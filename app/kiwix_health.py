#!/usr/bin/env python3
"""Read-only Kiwix diagnosis and a safe systemd restart helper."""
from __future__ import annotations
import json, shutil, subprocess, sys
from pathlib import Path


def cmd(*args):
    try:return subprocess.run(args,capture_output=True,text=True,timeout=15,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:
        return type('Result',(),{'returncode':1,'stdout':'','stderr':str(exc)})()


def doctor():
    from zim_manager import config
    from serve_zims import ready_directories
    cfg=config()
    exe=shutil.which('kiwix-serve')
    print('Kiwix executable:',exe or 'MISSING (sudo apt install kiwix-tools)')
    if exe:
        version=cmd(exe,'--version')
        print('Installed Kiwix version:',(version.stdout or version.stderr).strip()[:240])
        from serve_zims import supported_options
        help_text=supported_options(exe)
        print('Compatible options: skipInvalid='+str('--skipInvalid' in help_text)+', blockexternal='+str('--blockexternal' in help_text))
    print('Configured archive:',cfg.get('zim_dir','NOT CONFIGURED'))
    try:
        from dual_storage import locations
        items=locations(cfg)
        for row in items:
            print('Enrolled',row['key'],':',row['archive'],'dataset:',row['archive_ds'])
            found=[x for x in row['archive'].glob('*.zim') if x.is_file() and not x.is_symlink()] if row['archive'].exists() else []
            print('  Published .zim files:',len(found))
            for f in found[:12]:print('   ',f.name,'(',f.stat().st_size,'bytes)')
        directories=ready_directories(cfg)
    except (KeyError,ValueError,RuntimeError,OSError) as exc:
        print('Archive configuration problem:',exc)
        directories=[]
    print('Ready ZIM directories:',directories or 'NONE')
    # Don't trust an unverified partial, or blindly copy to the archive here.
    r=cmd('systemctl','is-active','doomsday-kiwix.service')
    print('Service:',r.stdout.strip() or r.stderr.strip() or 'unknown')
    port=cmd('ss','-ltnp')
    listeners=[line for line in port.stdout.splitlines() if ':8080 ' in line]
    print('Port 8080 listener:', '\n'.join(listeners) if listeners else 'NONE')
    journal=cmd('journalctl','-u','doomsday-kiwix.service','-n','15','--no-pager')
    print('Recent service log:\n',journal.stdout[-4000:] or journal.stderr)
    if not directories:
        print('ACTION: Check sudo doomsday-zims status; downloads must be verified and published as .zim files.')
    elif not listeners:
        print('ACTION: sudo doomsday-kiwix restart; inspect service log if still unavailable.')
    else:print('ACTION: Open http://127.0.0.1:8080 in Ubuntu; Windows needs VM port forwarding.')


def restart():
    for args in [('systemctl','reset-failed','doomsday-kiwix.service'),
                 ('systemctl','restart','doomsday-kiwix.service')]:
        result=cmd(*args)
        if result.returncode:
            print('Error:',result.stderr.strip() or result.stdout.strip(),file=sys.stderr)
            return 1
    print('Kiwix restart requested. Run sudo doomsday-kiwix doctor for readiness and logs.')
    return 0


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    if argv==['doctor'] or not argv:return doctor()
    if argv==['restart']:return restart()
    raise SystemExit('Usage: sudo doomsday-kiwix doctor|restart')

if __name__=='__main__':sys.exit(main())
