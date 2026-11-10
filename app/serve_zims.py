#!/usr/bin/env python3
"""Serve SHA-published ZIMs from mounted, enrolled read-only ZFS datasets.

During a fresh installation no archive may be ready yet. Wait for the first
published .zim instead of entering a systemd failed state. Never serve a
staging .part, symlink, or unmounted archive path.
"""
import os, pathlib, shutil, sys, time
from zim_manager import config, guard_path
from dual_storage import locations, archive_config
from archive_guard import checked_archive


def archive_directories(cfg):
    paths=[]
    for loc in locations(cfg):
        c=archive_config(cfg,loc['key'])
        try:
            path,ds=checked_archive(c)
            guard_path(path,ds)
        except (RuntimeError,ValueError,OSError) as exc:
            if loc['key']=='primary':
                raise RuntimeError('Primary archive is not mounted/verified: '+str(exc)) from exc
            print(f'Kiwix archive unavailable ({loc["key"]}): {exc}',file=sys.stderr,flush=True)
            continue
        if any(f.is_file() and not f.is_symlink() for f in path.glob('*.zim')):
            paths.append(str(path))
    return paths


# Compatibility for the read-only health command.
ready_directories=archive_directories

def diagnostics(print_fn=print):
    exe=shutil.which('kiwix-serve')
    if not exe:
        print_fn('KIWIX BINARY MISSING: sudo apt install kiwix-tools')
        return 3
    try:
        paths=archive_directories(config())
    except (RuntimeError,KeyError,ValueError,OSError) as exc:
        print_fn('ARCHIVE UNAVAILABLE: '+str(exc))
        return 2
    if not paths:
        print_fn('NO PUBLISHED ZIM on enrolled read-only datasets. Verify download status and mounts.')
        return 1
    print_fn('Kiwix is ready to serve: '+', '.join(paths))
    return 0

def serve_command(paths,exe):
    # Multiple directory arguments are supported by kiwix-serve. ZIMs still
    # remain on their enrolled datasets; no RAM copies or filesystem changes.
    return [exe,'--address=0.0.0.0','--port=8080','--blockexternal','--skipInvalid',*paths]


def main(argv=None):
    argv=[] if argv is None else argv
    if argv in (['--check'],['--diagnose']):return diagnostics()
    if argv:raise SystemExit('Usage: serve_zims.py [--check]')
    # The service remains alive while downloads are still staged, so it can
    # serve the first verified archive without a reboot or systemctl reset.
    while True:
        try:
            paths=archive_directories(config())
        except (KeyError,ValueError,RuntimeError,OSError) as exc:
            print(f'Kiwix waiting for configuration / ZFS mounts: {exc}',file=sys.stderr,flush=True)
            paths=[]
        exe=shutil.which('kiwix-serve')
        if exe and paths:
            print('Kiwix starting on port 8080 with:',', '.join(paths),flush=True)
            os.execv(exe,serve_command(paths,exe))
            return 0  # Mocked exec in unit tests; actual execv never returns.
        if not exe:
            print('Kiwix waiting: kiwix-serve not installed. sudo apt install kiwix-tools',file=sys.stderr,flush=True)
        else:
            print('Kiwix waiting for first published .zim in mounted archive datasets; checking again.',file=sys.stderr,flush=True)
        time.sleep(20)

if __name__=='__main__':sys.exit(main(sys.argv[1:]))
