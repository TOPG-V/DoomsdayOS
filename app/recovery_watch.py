#!/usr/bin/env python3
"""Conservative boot-failure watchdog and verified off-line recovery staging.

The boot watcher never rewrites UEFI, changes bootfs, rolls back the running
system, or overwrites the only snapshot. On repeated failed boots it records
an offline recovery plan. A separate recovery environment can receive a
verified snapshot to a NEW, unused dataset on an INDEPENDENT pool only.
"""
from __future__ import annotations
import argparse,datetime as dt,json,os,re,subprocess,sys
from pathlib import Path
from recovery_advisor import STATE,MARK,pool_guid,mounted_root,snapshots,rank,_atomic_write

WATCH=STATE/'boot-watch.json'
PLAN=STATE/'recovery-plan.json'


def _read(path):
    try:return json.loads(path.read_text())
    except (FileNotFoundError,ValueError):return {}


def _boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def _root_health():
    root=mounted_root()
    pool=root.split('/')[0]
    x=subprocess.run(['zpool','status','-x',pool],text=True,capture_output=True,timeout=18,check=False)
    if x.returncode or ('DEGRADED' in x.stdout or 'FAULTED' in x.stdout):
        raise RuntimeError('Root ZFS pool is not healthy')
    return {'root':root,'pool_guid':pool_guid(root),'status':x.stdout.strip()[:500]}


def watch_boot(boot_id=None):
    """Called ONCE per boot by early service, idempotent across restarts."""
    now=boot_id or _boot_id()
    current=_read(WATCH)
    if current.get('boot_id')==now:return current
    failures=int(current.get('consecutive_incomplete_boots',0))+(1 if current.get('pending') else 0)
    failures=min(100,failures)
    value={'boot_id':now,'pending':True,'consecutive_incomplete_boots':failures,
           'observed_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    _atomic_write(WATCH,value)
    if failures>=3:
        plan=make_plan(reason=f'{failures} consecutive boots without a healthy completion')
        _atomic_write(PLAN,plan)
        value['recovery_plan_ready']=True
        _atomic_write(WATCH,value)
    return value


def mark_healthy(boot_id=None,health_check=_root_health):
    """Called after boot stabilization. Never mark a corrupt pool healthy."""
    now=boot_id or _boot_id()
    current=_read(WATCH)
    if current.get('boot_id')!=now or not current.get('pending'):
        return {'ok':False,'reason':'No pending boot marker for this boot'}
    info=health_check()
    current.update({'pending':False,'consecutive_incomplete_boots':0,
                    'healthy_at':dt.datetime.now(dt.timezone.utc).isoformat(),'health':info})
    _atomic_write(WATCH,current)
    return {'ok':True,**current}


def make_plan(reason='operator-requested recovery'):
    try:
        root=mounted_root();guid=pool_guid(root);shots=snapshots(root)
        mark=_read(MARK)
        selected=mark.get('snapshot') if mark.get('pool_guid')==guid and mark.get('snapshot') in shots else None
        candidates=rank(shots,selected)
    except (OSError,RuntimeError) as e:
        return {'reason':reason,'safe_candidate':None,'error':str(e),'automatic_live_root_rollback':False}
    return {'reason':reason,'system_root':root,'source_pool_guid':guid,
            'safe_candidate':selected,'alternatives':candidates[:6],
            'automatically_selected':bool(selected),
            'automatic_live_root_rollback':False,
            'requires_rescue_boot':True,'verified_restore_test':False,
            'instructions':'Boot separate trusted recovery media; import source read-only and receive to an unused spare dataset. Restore EFI/bootloader separately and test before switching.'}


def _source_pool_guid(snapshot):
    name=snapshot.split('@',1)[0]
    return pool_guid(name)


def check_receive(snapshot,target,confirm=False,live_root=None):
    if '@' not in snapshot or snapshot.count('@')!=1 or not re.fullmatch(r'[A-Za-z0-9_./:@-]+',snapshot):
        raise ValueError('Invalid snapshot name')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+',target) or '@' in target:
        raise ValueError('Destination must be a NEW, unused ZFS child dataset')
    src= snapshot.split('@',1)[0]
    if not snapshot in snapshots(src):raise ValueError('Source snapshot does not exist')
    source_guid=_source_pool_guid(snapshot)
    destination_pool=target.split('/')[0]
    # Check dst pool is independently imported and not the source pool.
    imported=subprocess.run(['zpool','get','-H','-o','value','guid',destination_pool],text=True,capture_output=True,timeout=20)
    if imported.returncode:raise ValueError('Destination pool not imported')
    if source_guid==imported.stdout.strip():raise ValueError('Destination is on the same ZFS pool as source')
    exists=subprocess.run(['zfs','list','-H',target],text=True,capture_output=True,timeout=20)
    if exists.returncode==0:raise ValueError('Target dataset already exists — refusing overwrite')
    root=live_root if live_root is not None else subprocess.run(['findmnt','-n','-o','SOURCE','/'],text=True,capture_output=True).stdout.strip()
    if root.startswith(src.split('/')[0]+'/'):
        raise RuntimeError('Running from source pool. Must boot independent recovery media.')
    marker=_read(MARK)
    if marker.get('snapshot')!=snapshot or marker.get('pool_guid')!=source_guid:
        raise ValueError('Only an explicitly marked known-good snapshot is eligible for automatic selection')
    return {'snapshot':snapshot,'target':target,'source_guid':source_guid,'destination_guid':imported.stdout.strip(),
            'read_only_target':True,'will_execute':bool(confirm),
            'warning':'This creates a new, read-only ZFS dataset; no bootloader or EFI configuration is changed.'}


def receive_spare(snapshot,target,confirmation):
    if confirmation!='RECEIVE TO SPARE':raise ValueError('Type exactly RECEIVE TO SPARE')
    verified=check_receive(snapshot,target,confirm=True)
    # Explicit opt-in, no shell and no zfs receive -F, preserving all existing datasets.
    sender=subprocess.Popen(['zfs','send','-p',snapshot],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        receiver=subprocess.Popen(['zfs','receive','-u','-o','mountpoint=none','-o','readonly=on',target],
                                  stdin=sender.stdout,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        sender.stdout.close()
        rout,rerr=receiver.communicate()
        serr=sender.stderr.read();src_rc=sender.wait()
    except BaseException:
        sender.kill();sender.wait();raise
    if receiver.returncode or src_rc:
        raise RuntimeError('ZFS send/receive failed; inspect incomplete target manually: '+(rerr+serr).decode('utf-8','replace')[:500])
    verified['received']=True
    return verified


def main():
    p=argparse.ArgumentParser(description='Boot failure watch / safe external ZFS snapshot restore')
    p.add_argument('action',choices=['boot-start','boot-healthy','status','plan','check-receive','receive-spare'])
    p.add_argument('--snapshot');p.add_argument('--target');p.add_argument('--confirm',default='')
    a=p.parse_args()
    if a.action=='boot-start':data=watch_boot()
    elif a.action=='boot-healthy':data=mark_healthy()
    elif a.action=='status':data={'watch':_read(WATCH),'plan':_read(PLAN)}
    elif a.action=='plan':data=make_plan()
    else:
        if not a.snapshot or not a.target:p.error('Supply --snapshot and --target')
        data=check_receive(a.snapshot,a.target) if a.action=='check-receive' else receive_spare(a.snapshot,a.target,a.confirm)
    print(json.dumps(data,indent=2))
if __name__=='__main__':main()
