#!/usr/bin/env python3
"""Small, conservative fortnightly backup gate.

Daily timers make the interval persist across reboots. A failed backup is never
marked successful; another eligibility check happens the following day.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, re, subprocess, tempfile, time
from pathlib import Path

STATE = Path(os.environ.get('DOOMSDAY_STATE_DIR', '/var/lib/doomsday-vault'))
CONFIG = Path(os.environ.get('DOOMSDAY_BACKUP_POLICY','/etc/doomsday-vault/backup-policy.json'))
KINDS = ('local','external','zim-verify','zim-copy')
DEFAULTS = {'interval_days': 14, 'max_load_per_core': 1.5, 'stream_mib_per_second': 16,
            'min_system_free_gib': 20, 'min_backup_free_gib': 5, 'keep_local_editions': 2,
            'zim_backups': 'selected', 'selected_zims': []}


def settings():
    data=dict(DEFAULTS)
    if CONFIG.exists():data.update(json.loads(CONFIG.read_text()))
    days=int(data['interval_days'])
    if days<1 or days>365:raise ValueError('interval_days must be 1..365')
    speed=float(data['stream_mib_per_second'])
    if speed<0 or speed>1024:raise ValueError('invalid stream bandwidth limit')
    return data


def external_ready():
    """Only mark an external backup successful when an enrolled destination is present.

    Read the root-owned config as data, not shell code. No force-import or device paths.
    """
    path=Path('/etc/doomsday-vault/recovery.env')
    if not path.is_file():return False
    values={}
    for line in path.read_text().splitlines():
        m=re.fullmatch(r'([A-Z_]+)=(.*)',line.strip())
        if m:values[m[1]]=m[2].strip('"\'')
    guid=values.get('EXTERNAL_GUID','')
    ds=values.get('EXTERNAL_DATASET','')
    if not guid or guid.lower() in ('none','-') or not ds or '/' not in ds:return False
    r=subprocess.run(['zpool','list','-H','-o','name,guid'],capture_output=True,text=True)
    if r.returncode:return False
    pools={tokens[1]:tokens[0] for row in r.stdout.splitlines() if len(tokens:=row.split())==2}
    name=pools.get(guid)
    if not name or ds.split('/')[0]!=name:return False
    r=subprocess.run(['zfs','list','-H','-o','mounted',ds],capture_output=True,text=True)
    return r.returncode==0 and r.stdout.strip()=='yes'


def due(kind, now=None, load=None, cpus=None):
    if kind not in KINDS:raise ValueError('Unknown job')
    if kind=='external' and not external_ready():return False,'Enrolled external ZFS dataset not mounted'
    cfg=settings()
    now=time.time() if now is None else now
    stamp=STATE/('last-success-'+kind)
    if stamp.exists():
        try:last=float(stamp.read_text().strip())
        except ValueError:raise ValueError('Invalid previous backup timestamp')
        if now-last < int(cfg['interval_days'])*86400 and now>=last:return False,'Backup interval not yet reached'
    if os.environ.get('DOOMSDAY_IGNORE_LOAD') != '1':
        load = os.getloadavg()[0] if load is None else load
        cpus = os.cpu_count() or 1 if cpus is None else cpus
        if load>max(1,cpus)*float(cfg['max_load_per_core']):return False,'System busy; reschedule for next day'
    return True,'Eligible to run'


def mark(kind, now=None):
    if kind not in KINDS:raise ValueError('Unknown job')
    STATE.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,temp=tempfile.mkstemp(prefix='.last-success-',dir=STATE)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            out.write(str(int(time.time() if now is None else now))+'\n');out.flush();os.fsync(out.fileno())
        os.replace(temp,STATE/('last-success-'+kind))
    finally:
        if os.path.exists(temp):os.unlink(temp)


def main():
    p=argparse.ArgumentParser();p.add_argument('command', choices=['check','mark','status']);p.add_argument('kind',choices=KINDS);a=p.parse_args()
    if a.command=='mark':mark(a.kind);print('Success marker saved:',a.kind);return 0
    eligible, reason=due(a.kind)
    print(f'{a.kind}: {reason}')
    return 0 if eligible or a.command=='status' else 1

if __name__=='__main__':raise SystemExit(main())
