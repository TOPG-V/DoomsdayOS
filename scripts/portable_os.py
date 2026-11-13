#!/usr/bin/env python3
"""Read-only boot portability audit and verified preparation report.

This program never rewrites GRUB, EFI, initramfs, pool labels or /etc/fstab.
Portable boot still requires testing the target device in a recovery environment.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

STATE=Path(os.environ.get('DOOMSDAY_STATE_DIR','/var/lib/doomsday-vault'))

def _command(*args,timeout=10):
    try:
        p=subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=False)
        return p.stdout.strip() if p.returncode==0 else ''
    except (OSError,subprocess.TimeoutExpired):return ''

def _fstab_audit(text):
    risks=[];portable=[]
    for index,line in enumerate(text.splitlines(),1):
        fields=line.strip().split()
        if len(fields)<4 or fields[0].startswith('#'):continue
        spec,mount,fstype=fields[:3]
        if spec.startswith('/dev/sd') or spec.startswith('/dev/mmcblk') or spec.startswith('/dev/nvme') or spec.startswith('/dev/disk/by-path/'):
            risks.append({'line':index,'mount':mount,'device':spec,'recommendation':'Use verified UUID/PARTUUID or dataset identity after testing.'})
        if spec.startswith(('UUID=','PARTUUID=','LABEL=','/dev/disk/by-id/')) or fstype=='zfs':
            portable.append({'line':index,'mount':mount,'source':spec})
    return {'device_path_risks':risks,'stable_mounts':portable}

def _sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):h.update(part)
    return h.hexdigest()

def report():
    root=_command('findmnt','-n','-o','SOURCE,FSTYPE','/')
    boot=_command('findmnt','-n','-o','SOURCE,FSTYPE','/boot')
    efi=_command('findmnt','-M','/boot/efi','-n','-o','SOURCE,FSTYPE')
    root_pool=root.split()[0].split('/')[0] if ' zfs' in root else None
    fstab=Path('/etc/fstab')
    mount_report=_fstab_audit(fstab.read_text(encoding='utf-8')) if fstab.is_file() else {'device_path_risks':[], 'stable_mounts':[]}
    kernel=_command('uname','-r')
    initrd=Path('/boot')/('initrd.img-'+kernel)
    initrd_ok=initrd.is_file() and initrd.stat().st_size>0
    module_report={}
    for module in ('zfs','usb_storage','uas','nvme','ahci','xhci_pci'):
        module_report[module]=bool(_command('modinfo','-n',module))
    removable_efi=Path('/boot/efi/EFI/BOOT/BOOTX64.EFI')
    issues=[]
    if not root:issues.append('Root filesystem unavailable')
    if mount_report['device_path_risks']:issues.append('fstab uses non-portable device path(s)')
    if not efi:issues.append('EFI system partition not mounted (BIOS systems may not use EFI)')
    elif not removable_efi.is_file():issues.append('Removable-media UEFI fallback bootloader not detected')
    if not initrd_ok:issues.append('Current kernel initramfs missing or empty')
    if root_pool and not module_report['zfs']:issues.append('ZFS module not visible via modinfo; check built-in module and initramfs')
    return {'os_root':root,'boot':boot or None,'efi':efi or None,'kernel':kernel,
            'initramfs':str(initrd),'initramfs_exists':initrd_ok,'modules_installed':module_report,
            'fstab':mount_report,'pools':_command('zpool','list','-H','-o','name,guid,health').splitlines(),
            'machine_id_backup_needed':True,
            'issues':issues,
            'suggestions':[
                'Keep ZFS pool GUIDs and filesystem UUIDs; avoid hard-coded /dev/sda mappings.',
                'Check both UEFI and BIOS target compatibility; preserve EFI before moving a system disk.',
                'Install storage-controller / USB / NVMe drivers in initramfs as required by the target computer.',
                'Use CPU-only llama.cpp as a fallback if GPU drivers on the destination PC are incompatible.',
                'Verify encrypted disks, TPM bindings, Secure Boot and firmware settings before moving hardware.',
                'Test a boot and ZFS import on spare media; a checksum-perfect file copy alone is not bootable.'
            ],
            'will_edit_boot_configuration':False}

def prepare(output=None):
    """Save a portable recovery inventory without touching live boot settings."""
    if os.geteuid()!=0:raise PermissionError('Run with sudo to save protected recovery metadata')
    destination=Path(output) if output else STATE/'portability'
    if not destination.is_absolute() or destination.is_symlink():raise ValueError('Absolute, nonsymlink directory required')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    if destination.is_symlink():raise ValueError('Refusing symlink directory')
    files=['/etc/fstab','/etc/default/grub','/etc/zfs/zpool.cache',
           '/etc/crypttab','/etc/machine-id']
    resources=[]
    for path in files:
        f=Path(path)
        if f.is_symlink() or not f.is_file():continue
        resources.append({'path':path,'size_bytes':f.stat().st_size,'sha256':_sha(f)})
    record={'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
            'audit':report(),'boot_files':resources,
            'notice':'Inventory only. No boot files were modified, imported or restored.'}
    fd,temp=tempfile.mkstemp(prefix='.portability-',dir=destination)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as out:
            json.dump(record,out,indent=2);out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(temp,destination/'portability-report.json')
    finally:
        if os.path.exists(temp):os.unlink(temp)
    return {'saved':str(destination/'portability-report.json'),'issues':record['audit']['issues']}

def main(argv=None):
    p=argparse.ArgumentParser(description='PC portability and boot-device migration audit')
    p.add_argument('action',choices=['report','prepare']);p.add_argument('--output')
    args=p.parse_args(argv)
    data=report() if args.action=='report' else prepare(args.output)
    print(json.dumps(data,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())