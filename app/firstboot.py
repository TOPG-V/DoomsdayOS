#!/usr/bin/env python3
"""Interactive setup; never formats disks and does not require an AI model."""
import json, os, subprocess, sys
from pathlib import Path
from zim_manager import BASE, CONF, atomic_json, interactive_wizard, dataset_at
import wifi_manager

def run(*args):
    return subprocess.run(args,check=False)

def main():
    if os.geteuid()!=0:sys.exit('Run via sudo: sudo doomsday-setup')
    marker=Path('/usr/share/doomsday-vault/setup-complete')
    print('=== Doomsday Vault first-boot setup ===')
    print('No disk will be formatted. You can rerun this wizard to add more ZIM archives.')
    root=subprocess.run(['findmnt','-n','-o','FSTYPE','/'],capture_output=True,text=True).stdout.strip()
    if root!='zfs':
        print('WARNING: Running root filesystem is not ZFS. Recovery installer will skip ZFS-specific operations.')
    cfg={'zim_dir':'/srv/doomsday-zims','backup_dir':'','require_sha256':True,'immutable_zims':True,'min_free_gib':3}
    if CONF.exists():
        cfg.update(json.loads(CONF.read_text()))
    print('Immutable ZIM destination:',cfg['zim_dir'])
    print('Staging destination:',cfg.get('staging_dir','(not configured)'))
    cfg['zim_dataset']=dataset_at(Path(cfg['zim_dir']))
    print('You can choose an OS-drive folder or another disk for optional ZIM backups in the download wizard.')
    atomic_json(CONF,cfg)
    print('\nConfigure Wi-Fi hotspot (optional):')
    wifi_manager.configure()
    print('\nZIM library selection (requires Internet only during download):')
    interactive_wizard()
    BASE.mkdir(parents=True,exist_ok=True)
    marker.parent.mkdir(parents=True,exist_ok=True)
    marker.write_text('complete\n')
    os.chmod(marker,0o644)
    print('\nSetup saved. Safe automatic services will continue across reboots.')
    print('Download status: sudo doomsday-zims status')
    print('Hotspot status: sudo doomsday-wifi scan')

if __name__=='__main__':main()
