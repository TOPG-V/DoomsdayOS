#!/usr/bin/env python3
"""Verified self-copy installer for Doomsday Vault.

Copy into a private sibling directory, SHA-256 compare every staged file,
atomically promote the tree, and keep exactly one rollback tree until the
installer confirms the services and integrity baseline were installed.

This tool never deletes the folder from which the user invoked install.sh.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile

ESSENTIAL = ('install.sh', 'app/ai_gateway.py', 'app/archive_guard.py',
             'scripts/deploy_atomic.py', 'systemd/doomsday-ai-web.service')
ROOT_ITEMS = ('app', 'scripts', 'systemd', 'vendor', 'docs', 'iso', 'tests',
              'install.sh', 'build-iso.sh', 'README.md', 'README_ZIMLENS_VNEXT.md',
              'README_PORTABLE_ZIM_PROTECTOR.md', 'LICENSE', '.gitignore',
              'apply_integration.py')
OMIT_DIRS = {'.git', '.venv', 'node_modules', '__pycache__', '.pytest_cache', '.mypy_cache'}
OMIT_SUFFIXES = ('.pyc', '.pyo', '.zim', '.zim.part', '.gguf', '.part')
MARKER = '.doomsday-install-transaction.json'


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as inp:
        for part in iter(lambda: inp.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def file_map(root: Path) -> dict[str, str]:
    result = {}
    for item in ROOT_ITEMS:
        src = root / item
        if not src.exists() and not src.is_symlink():
            continue
        if src.is_symlink():
            raise ValueError('Refusing symlink in install source: ' + item)
        paths = [src] if src.is_file() else sorted(src.rglob('*'))
        for path in paths:
            if any(p in OMIT_DIRS for p in path.relative_to(root).parts):
                continue
            if path.is_symlink():
                raise ValueError('Refusing symlink in install source: ' + str(path))
            if path.is_dir():
                continue
            if not path.is_file():
                raise ValueError('Unexpected non-file in release: ' + str(path))
            if path.name.endswith(OMIT_SUFFIXES):
                continue
            result[path.relative_to(root).as_posix()] = file_hash(path)
    return result


def check_source(source: Path) -> dict[str, str]:
    expected = file_map(source)
    missing = [name for name in ESSENTIAL if name not in expected]
    if missing:
        raise ValueError('Incomplete source package: ' + ', '.join(missing))
    return expected


def copy_release(source: Path, dest: Path) -> None:
    for name in ROOT_ITEMS:
        src = source / name
        if not src.exists():
            continue
        target = dest / name
        if src.is_dir():
            def ignore(directory: str, names: list[str]) -> set[str]:
                return {n for n in names if n in OMIT_DIRS or n.endswith(OMIT_SUFFIXES)}
            shutil.copytree(src, target, symlinks=False, ignore=ignore)
        else:
            shutil.copy2(src, target)


def _safe_locations(source: Path, dest: Path) -> tuple[Path, Path]:
    src = source.resolve(strict=True)
    target = dest.absolute()
    if not (src / 'install.sh').is_file() or src == target:
        raise ValueError('Source must be a separate Doomsday Vault release folder')
    if dest.is_symlink() or target.parent.is_symlink():
        raise ValueError('Refusing symlink installation paths')
    if target in src.parents or src in target.parents:
        raise ValueError('Nested source and destination are forbidden')
    if target == Path('/') or str(target) in ('/opt', '/usr', '/home'):
        raise ValueError('Invalid installation destination')
    target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    return src, target


def stage(source: Path, destination: Path) -> str:
    source, destination = _safe_locations(source, destination)
    expected = check_source(source)
    token = secrets.token_hex(12)
    staging = Path(tempfile.mkdtemp(prefix='.doomsday-stage-', dir=destination.parent))
    backup = destination.parent / ('.doomsday-rollback-' + token)
    had_old = destination.exists()
    moved_old = False
    promoted = False
    try:
        copy_release(source, staging)
        if file_map(staging) != expected:
            raise RuntimeError('SHA-256 verification of self-copied release failed')
        # MapLibre assets can be explicitly installed offline after setup. They
        # live in the old program tree, not state/, so preserve that narrow
        # known optional directory without importing arbitrary executable code.
        if had_old:
            optional = destination / 'app' / 'web' / 'vendor'
            keep = staging / 'app' / 'web' / 'vendor'
            if optional.is_dir() and not optional.is_symlink() and not keep.exists():
                if any(p.is_symlink() for p in optional.rglob('*')):
                    raise ValueError('Refusing symlinked optional MapLibre assets')
                shutil.copytree(optional, keep)
        if had_old:
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError('Existing installation is not a regular directory')
            os.replace(destination, backup)
            moved_old = True
        os.replace(staging, destination)
        promoted = True
        info = {'token': token, 'backup': backup.name if had_old else None,
                'had_old': had_old, 'sha256_files': len(expected)}
        marker = destination / MARKER
        with marker.open('x', encoding='utf-8') as output:
            json.dump(info, output)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(marker, 0o600)
        return token
    except Exception:
        if promoted and destination.exists():
            shutil.rmtree(destination)
        if moved_old and backup.exists():
            os.replace(backup, destination)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _transaction(dest: Path, token: str) -> tuple[dict, Path | None]:
    if not token or len(token) != 24 or any(c not in '0123456789abcdef' for c in token):
        raise ValueError('Invalid transaction token')
    if dest.is_symlink() or not dest.is_dir():
        raise ValueError('Installed tree missing or symlinked')
    marker = dest / MARKER
    if marker.is_symlink() or not marker.is_file():
        raise ValueError('No managed installation transaction')
    info = json.loads(marker.read_text(encoding='utf-8'))
    if info.get('token') != token:
        raise ValueError('Installation transaction mismatch')
    expected_backup = '.doomsday-rollback-' + token
    backup_name = info.get('backup')
    if backup_name is not None and backup_name != expected_backup:
        raise ValueError('Unsafe rollback target')
    backup = dest.parent / backup_name if backup_name else None
    if backup and (backup.is_symlink() or not backup.is_dir()):
        raise ValueError('Rollback directory missing or symlinked')
    return info, backup


def complete(dest: Path, token: str) -> None:
    info, backup = _transaction(dest, token)
    # Finalize only after the called installer has generated its integrity baseline.
    # The caller can supply an independent manifest location, so avoid embedding it
    # here; this transaction protects code, not users' archive/data directories.
    (dest / MARKER).unlink()
    if backup:
        shutil.rmtree(backup)


def rollback(dest: Path, token: str) -> None:
    info, backup = _transaction(dest, token)
    failed = dest.parent / ('.doomsday-failed-' + token)
    if failed.exists() or failed.is_symlink():
        raise ValueError('Refusing unexpected failed-install directory')
    os.replace(dest, failed)
    try:
        if backup:
            os.replace(backup, dest)
    except Exception:
        os.replace(failed, dest)
        raise
    shutil.rmtree(failed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('stage')
    p.add_argument('source', type=Path)
    p.add_argument('destination', type=Path)
    for name in ('complete', 'rollback'):
        p = sub.add_parser(name)
        p.add_argument('destination', type=Path)
        p.add_argument('token')
    args = parser.parse_args()
    if args.command == 'stage':
        print(stage(args.source, args.destination))
    elif args.command == 'complete':
        complete(args.destination, args.token)
    else:
        rollback(args.destination, args.token)


if __name__ == '__main__':
    main()