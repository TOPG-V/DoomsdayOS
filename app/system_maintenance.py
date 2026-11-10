#!/usr/bin/env python3
"""Bounded OS diagnostics and explicitly approved, allowlisted repair operations.

The browser may call *only* analyze()/script_report(). Privileged operations require
root in a terminal, action-specific confirmation, and never execute model text.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

STATE=Path(os.environ.get('DOOMSDAY_MAINTENANCE_STATE','/var/lib/doomsday-vault/maintenance'))
SERVICES=('doomsday-kiwix.service','doomsday-ai-web.service')
SCRIPTS={
    'storage':['df','zpool','mounts'],
    'boot':['failed','mounts','dpkg'],
    'integrity':['zpool','dpkg','readonly'],
    'packages':['dpkg','verify'],
    'all':['df','zpool','mounts','failed','dpkg','readonly']
}
MAX_OUT=5500


def _run(args,timeout=12):
    try:
        p=subprocess.run(args,text=True,capture_output=True,timeout=timeout,check=False,
                         env={'PATH':'/usr/sbin:/usr/bin:/sbin:/bin','LC_ALL':'C'})
        return {'code':p.returncode,'text':((p.stdout or '')+'\n'+(p.stderr or '')).strip()[:MAX_OUT]}
    except (OSError,subprocess.TimeoutExpired) as exc:
        return {'code':127,'text':str(exc)[:220]}


def _readonly_report():
    try:
        import archive_guard
        cfg=archive_guard.current_config()
        if not cfg.get('immutable_zims'):return {'code':0,'text':'Immutable ZIM protection not configured'}
        lines=[]
        for key in ('zim_dir','zim_dir_2'):
            path=cfg.get(key)
            if not path:continue
            dataset=archive_guard.exact_dataset(Path(path))
            if not dataset:lines.append('UNMOUNTED: '+key);continue
            prop=_run(['zfs','get','-H','-o','value','readonly',dataset])
            lines.append(key+' dataset '+dataset+' readonly='+prop['text'].splitlines()[0][:25])
        return {'code':0,'text':'\n'.join(lines) or 'No enrolled ZIM datasets'}
    except (OSError,ValueError,RuntimeError) as exc:return {'code':1,'text':str(exc)[:250]}


def script_report(name):
    """Predefined terminal scripts only; never accept a script body from an LLM."""
    if name not in SCRIPTS:raise ValueError('Choose storage, boot, integrity, or all')
    commands={
        'df':['df','-P','-h','/'],
        'zpool':['zpool','status','-x'],
        'mounts':['findmnt','-n','-o','SOURCE,FSTYPE','/'],
        'failed':['systemctl','--failed','--no-pager','--plain'],
        'dpkg':['dpkg','--audit'],
        'verify':['dpkg','--verify']}
    result={}
    for key in SCRIPTS[name]:result[key]=_readonly_report() if key=='readonly' else _run(commands[key],timeout=35 if key=='verify' else 12)
    return {'script':name,'results':result,'execution':'read-only allowlisted diagnostics',
            'note':'No shell execution, arbitrary commands, or OS writes.'}


def analyze():
    report=script_report('all');findings=[];actions=[]
    data=report['results']
    # Preserve OS headroom for journaling, package work, and snapshot metadata.
    try:
        free=shutil.disk_usage('/').free
        if free<2*1024**3:
            findings.append({'severity':'critical','issue':'Less than 2 GiB free on OS filesystem',
                             'action':'Stop nonessential writes and inspect disk usage; never delete archives or snapshots automatically.'})
        elif free<8*1024**3:
            findings.append({'severity':'warning','issue':'Less than 8 GiB free on OS filesystem',
                             'action':'Reserve OS space before downloads, cache growth or upgrades.'})
    except OSError:pass
    if 'DEGRADED' in data['zpool']['text'] or 'UNAVAIL' in data['zpool']['text'] or 'FAULTED' in data['zpool']['text']:
        findings.append({'severity':'critical','issue':'ZFS pool reports degraded or unavailable devices','action':'Inspect zpool status -v and preserve the existing backup. Never clear errors as a repair.'})
    if 'UNMOUNTED:' in data['readonly']['text']:
        findings.append({'severity':'critical','issue':'An enrolled ZIM archive is not mounted','action':'Check the original pool and its mount. Do not write to the mountpoint.'})
    if 'readonly=off' in data['readonly']['text']:
        findings.append({'severity':'warning','issue':'An enrolled ZIM dataset is currently writable','action':'Protect archives when no ZIM publication is running.'})
        actions.append({'id':'protect-zims','description':'Reassert ZFS readonly=on on exact enrolled archive datasets','risk':'May pause a ZIM publication; only run after transfers stop.'})
    if data['dpkg']['text'].strip():
        findings.append({'severity':'warning','issue':'dpkg reports incomplete package configuration','action':'Review dpkg --audit before running apt or dpkg modifications.'})
    if data['failed']['code']==0:
        for service in SERVICES:
            if service in data['failed']['text']:
                findings.append({'severity':'warning','issue':'Service failed: '+service,'action':'Review journalctl -u '+service+' before restarting it.'})
                actions.append({'id':'restart-'+service.split('.')[0],'description':'Restart '+service,'risk':'Brief service interruption; no archive data altered.'})
    for key,entry in data.items():
        if entry['code']==127:findings.append({'severity':'info','issue':'Diagnostic unavailable: '+key,'action':entry['text'][:120]})
    if not findings:findings.append({'severity':'ok','issue':'No obvious problems detected by the limited checks','action':'This does not prove data integrity; verify hashes and monitor scrub results.'})
    return {'findings':findings,'actions':actions,'reports':data,'read_only':True,
            'note':'Review before any repair. No filesystem rewrite, package reinstall, fsck, zpool clear or rollback is automatic.'}


def _write_log(action,result):
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    dest=STATE/'actions.jsonl'
    # Open append with root-only permissions; no untrusted shell output execution.
    fd=os.open(str(dest),os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,'a',encoding='utf-8') as f:
            f.write(json.dumps({'at':dt.datetime.now(dt.timezone.utc).isoformat(), 'action':action,
                                'result':result},ensure_ascii=False)+'\n')
            f.flush();os.fsync(f.fileno())
    except Exception:raise


def apply(action,confirmation,runner=_run):
    if os.geteuid()!=0:raise PermissionError('Run as root in your own Ubuntu terminal')
    if confirmation!='APPLY-'+action:raise ValueError('Type the exact action confirmation; no repair was performed')
    if action=='protect-zims':
        # Prefer existing guarded archive manager, never arbitrary paths.
        import archive_guard
        output=archive_guard.protect();out={'code':0,'text':output}
    elif action in ('restart-doomsday-kiwix','restart-doomsday-ai-web'):
        service=action.removeprefix('restart-')+'.service'
        out=runner(['systemctl','restart',service],timeout=28)
    elif action=='scrub-enrolled':
        import archive_guard
        cfg=archive_guard.current_config()
        ds=cfg.get('zim_dataset')
        if not ds or '/' not in ds or not re.fullmatch(r'[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)+',ds):
            raise ValueError('Enrolled ZIM dataset not configured')
        pool=ds.split('/')[0]
        if archive_guard.exact_dataset(Path(cfg.get('zim_dir','/nonexistent')))!=ds:
            raise RuntimeError('Original ZIM dataset is not mounted at the enrolled location')
        check=runner(['zpool','status',pool],timeout=10)
        if check['code']:raise RuntimeError('Cannot confirm existing enrolled pool')
        # A ZFS scrub uses substantial I/O; defer while the download worker
        # could be publishing ZIMs. No automatic scrub via this helper.
        activity=runner(['systemctl','is-active','doomsday-zim-download.service'],timeout=8)
        if activity['text'].splitlines()[:1]==['active']:
            raise RuntimeError('ZIM download service is active; schedule scrub when it is idle')
        out=runner(['zpool','scrub',pool],timeout=15)
    else:raise ValueError('Only approved maintenance actions are supported')
    _write_log(action,out)
    return out


def auto_heal(runner=_run):
    """Very narrow self-heal: restart failed local *reader* services at most twice/day.

    Never run apt/dpkg/fsck, change ZFS properties, delete files, reboot, or
    accept any LLM-generated commands. Avoid repeated starts on broken systems.
    """
    if os.geteuid()!=0:raise PermissionError('Auto-heal requires root systemd unit')
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    marker=STATE/'auto-restarts.json'
    try:previous=json.loads(marker.read_text())
    except (OSError,ValueError):previous={}
    now=int(time.time());results=[]
    for name in SERVICES:
        status=runner(['systemctl','is-failed',name],timeout=8)
        if status['text'].splitlines()[:1]!=['failed']:continue
        stamps=[int(t) for t in previous.get(name,[]) if isinstance(t,int) and now-int(t)<86400]
        if len(stamps)>=2:
            results.append({'service':name,'result':'restart limit reached'});continue
        attempt=runner(['systemctl','restart',name],timeout=25)
        previous[name]=stamps+[now]
        results.append({'service':name,'code':attempt['code']})
    fd,tmp=tempfile.mkstemp(prefix='.maintenance-',dir=STATE)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:json.dump(previous,f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,marker)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return {'restarts':results,'scope':'only failed Kiwix/AI web readers; no data mutation'}


def main(argv=None):
    p=argparse.ArgumentParser(description='Safe DoomsdayOS diagnostics and approved repairs')
    s=p.add_subparsers(dest='command',required=True)
    s.add_parser('analyze')
    script=s.add_parser('run-script',help='Run a known read-only terminal diagnostic script')
    script.add_argument('name',choices=sorted(SCRIPTS))
    a=s.add_parser('apply',help='Run a reviewed, explicitly confirmed repair')
    a.add_argument('action',choices=['restart-doomsday-kiwix','restart-doomsday-ai-web','protect-zims','scrub-enrolled'])
    a.add_argument('--confirm',default='')
    s.add_parser('auto')
    args=p.parse_args(argv)
    if args.command=='analyze':output=analyze()
    elif args.command=='run-script':output=script_report(args.name)
    elif args.command=='apply':output=apply(args.action,args.confirm)
    else:output=auto_heal()
    print(json.dumps(output,indent=2,ensure_ascii=False))
    if args.command=='apply' and output['code']:return 2
    return 0
if __name__=='__main__':sys.exit(main())
