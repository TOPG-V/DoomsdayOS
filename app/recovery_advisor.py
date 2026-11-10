#!/usr/bin/env python3
"""Read-only last-known-good recovery advisor and new-computer detection.

Never automatically rolls back the running ZFS root, overwrites EFI, or force
imports any pool. Actual bare-metal recovery needs separate boot media and a
verified restore test. Marks are user-approved advisory metadata, not proof.
"""
from __future__ import annotations
import argparse,datetime as dt,hashlib,json,os,platform,subprocess,tempfile
from pathlib import Path

STATE=Path(os.environ.get('DOOMSDAY_STATE_DIR','/var/lib/doomsday-vault'))
MARK=STATE/'last-known-good.json'
HARDWARE=STATE/'portable-hardware.json'


def command(*a):
    return subprocess.run(list(a),capture_output=True,text=True,check=False,timeout=12)


def pool_guid(dataset):
    pool=dataset.split('/')[0]
    x=command('zpool','get','-H','-o','value','guid',pool)
    if x.returncode:raise RuntimeError('Cannot inspect system pool GUID')
    return x.stdout.strip()


def mounted_root():
    x=command('findmnt','-n','-o','SOURCE','/')
    if x.returncode or '/' not in x.stdout.strip():raise RuntimeError('Root is not a detectable ZFS child dataset')
    return x.stdout.strip()


def snapshots(root):
    x=command('zfs','list','-H','-o','name','-t','snapshot','-s','creation','-r',root)
    if x.returncode:raise RuntimeError('Cannot list snapshots; ZFS unavailable')
    return [s.strip() for s in x.stdout.splitlines() if s.startswith(root+'@')]


def rank(candidates,marked=None):
    """Recent snapshots first; user-marked last-good gets priority only if present."""
    names=list(dict.fromkeys(candidates))
    return sorted(names,key=lambda x:(x==marked,x.split('@')[-1]),reverse=True)


def _atomic_write(path,data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(prefix='.safe-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(data,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def mark_good(snapshot):
    root=mounted_root()
    if not snapshot.startswith(root+'@') or snapshot not in snapshots(root):
        raise ValueError('Select an existing snapshot from the active root filesystem')
    result={'snapshot':snapshot,'pool_guid':pool_guid(root),'user_marked_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'caution':'Marked as last-known-good by operator, NOT verified by a full restore test.'}
    _atomic_write(MARK,result)
    return result


def recovery_report():
    root=mounted_root()
    pool=pool_guid(root)
    allshots=snapshots(root)
    mark=json.loads(MARK.read_text()) if MARK.is_file() else {}
    chosen=mark.get('snapshot') if mark.get('pool_guid')==pool else None
    recommended=rank(allshots,chosen)[:8]
    health=command('zpool','status','-x')
    return {
        'root':root,'pool_guid':pool,'known_good_snapshot':chosen if chosen in allshots else None,
        'ranked_restore_candidates':recommended,'zpool_status':health.stdout.strip()[:1200],
        'safe_to_auto_rollback':False,
        'restore_notice':'No automatic rollback of the active OS is attempted. Select a snapshot only from a separate recovery environment and restore to a spare disk first.',
        'efi_notice':'ZFS snapshots do not include the FAT/EFI system partition; validate independent EFI backups.'}


def hardware_digest():
    # Hash machine-specific IDs rather than storing raw serials in the archive.
    ids=[platform.machine(),platform.processor()]
    for p in ('/sys/class/dmi/id/product_uuid','/sys/class/dmi/id/board_name'):
        try:ids.append(Path(p).read_text().strip())
        except (OSError,PermissionError):ids.append('unknown')
    return hashlib.sha256('|'.join(ids).encode()).hexdigest()


def hardware_report():
    now=hardware_digest()
    try:previous=json.loads(HARDWARE.read_text()).get('hardware_hash')
    except (FileNotFoundError,ValueError):previous=None
    return {'architecture':platform.machine(),'new_hardware_detected':bool(previous and previous!=now),
            'enrolled':bool(previous),'current_machine_hash_prefix':now[:12],
            'advice':'On new hardware, inspect storage controllers, CPU instructions, UEFI/Secure Boot, Wi-Fi, drivers and pool GUIDs. No automatic destructive repair.'}


def enroll_hardware():
    x={'hardware_hash':hardware_digest(),'enrolled_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    _atomic_write(HARDWARE,x)
    return x


def main():
    p=argparse.ArgumentParser(description='Read-only ZFS recovery and portable hardware advisor')
    p.add_argument('action',choices=['report','hardware','enroll-hardware','mark-good'])
    p.add_argument('--snapshot')
    a=p.parse_args()
    if a.action=='mark-good':
        if not a.snapshot:p.error('Please select an existing snapshot with --snapshot')
        value=mark_good(a.snapshot)
    elif a.action=='hardware':value=hardware_report()
    elif a.action=='enroll-hardware':value=enroll_hardware()
    else:value=recovery_report()
    print(json.dumps(value,indent=2))
if __name__=='__main__':main()
