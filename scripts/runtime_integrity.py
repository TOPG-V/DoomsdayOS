#!/usr/bin/env python3
"""Detect changed/missing installed DoomsdayOS program files with SHA-256.

Only program files in /opt are measured; never hashes user archives for a
runtime snapshot and never repairs or deletes unexpected files. Verified ZIMs
use archive_guard.py and their own publisher-trusted SHA-256 manifest.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

ROOT = Path('/opt/doomsday-firstboot')
MANIFEST = Path('/var/lib/doomsday-vault/installation-manifest.json')
DIRS = ('app', 'scripts', 'systemd', 'vendor', 'docs', 'iso', 'tests')
TOP = ('install.sh', 'build-iso.sh', 'README.md', 'README_ZIMLENS_VNEXT.md',
       'LICENSE', 'apply_integration.py')
SKIP = {'__pycache__', '.git', '.venv', '.pytest_cache', '.mypy_cache', 'node_modules'}


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as fd:
        for block in iter(lambda: fd.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def enumerate_programs(root: Path) -> dict[str, str]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Installed source directory missing or symlinked')
    files = {}
    for name in (*DIRS, *TOP):
        path = root / name
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink():
            raise ValueError('Unexpected symlink: ' + str(path))
        paths = path.rglob('*') if path.is_dir() else [path]
        for candidate in paths:
            if any(part in SKIP for part in candidate.relative_to(root).parts):
                continue
            if candidate.is_symlink():
                raise ValueError('Unexpected symlink: ' + str(candidate))
            if candidate.is_file() and not candidate.name.endswith(('.pyc', '.pyo')):
                files[candidate.relative_to(root).as_posix()] = hash_file(candidate)
    if 'app/archive_guard.py' not in files or 'install.sh' not in files:
        raise ValueError('Incomplete DoomsdayOS release')
    return files


def create_snapshot(root: Path, manifest: Path) -> dict:
    files = enumerate_programs(root)
    doc = {'schema': 1, 'root': str(root.resolve()), 'created_at_unix': int(time.time()),
           'algorithm': 'sha256', 'files': files}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.installation-manifest-', dir=manifest.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(doc, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, manifest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return {'tracked_files': len(files), 'manifest': str(manifest)}


def verify(root: Path, manifest: Path) -> dict:
    try:
        data = json.loads(manifest.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'ok': False, 'error': 'No installation baseline. Reinstall or run the initial setup.'}
    if data.get('schema') != 1 or data.get('algorithm') != 'sha256' or data.get('root') != str(root.resolve()):
        return {'ok': False, 'error': 'Manifest schema or installation root mismatch'}
    expected = data.get('files')
    if not isinstance(expected, dict) or not expected:
        return {'ok': False, 'error': 'Missing expected file digests'}
    actual = enumerate_programs(root)
    missing = sorted(set(expected) - set(actual))
    changed = sorted(k for k in set(expected) & set(actual) if expected[k] != actual[k])
    extra = sorted(set(actual) - set(expected))
    return {'ok': not missing and not changed and not extra,
            'checked': len(expected), 'missing': missing, 'changed': changed,
            'untracked_programs': extra,
            'note': 'No automatic repair. Use a known-good installer or verified backup if needed.'}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=('snapshot', 'verify'))
    p.add_argument('--root', type=Path, default=ROOT)
    p.add_argument('--manifest', type=Path, default=MANIFEST)
    a = p.parse_args()
    result = create_snapshot(a.root, a.manifest) if a.command == 'snapshot' else verify(a.root, a.manifest)
    print(json.dumps(result, indent=2))
    if not result.get('ok', True):
        raise SystemExit(2)

if __name__ == '__main__':
    main()