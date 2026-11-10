#!/usr/bin/env python3
"""Opt-in idle/sleep inhibitor for a powered DoomsdayOS host.

Service never promises downloads while hardware is asleep. Runs only while on
AC or when user opted into unknown-power devices (VMs). No changes to firmware,
bootloader, systemd sleep configuration, or Windows host power management.
"""
from __future__ import annotations
import argparse,json,os,signal,subprocess,time
from pathlib import Path

CFG=Path(os.environ.get('DOOMSDAY_POWER_STATE','/etc/doomsday-vault/power.json'))

def ac_status(root=Path('/sys/class/power_supply')):
    status=[]
    if not root.exists():return 'unknown'
    for supply in root.iterdir():
        try:
            if (supply/'type').read_text().strip().lower() in ('mains','usb','usb-c','usb_pd'):
                status.append((supply/'online').read_text().strip()=='1')
        except (OSError,ValueError):continue
    return 'online' if any(status) else ('offline' if status else 'unknown')

def settings():
    try:return json.loads(CFG.read_text())
    except (OSError,ValueError):return {'enabled':False,'allow_unknown':False}

def should_inhibit(cfg,power):
    return bool(cfg.get('enabled') and (power=='online' or (power=='unknown' and cfg.get('allow_unknown'))))

def _save(cfg):
    import tempfile
    CFG.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=str(CFG.parent),prefix='.power-')
    try:
        with os.fdopen(fd,'w') as f:json.dump(cfg,f);f.write('\n');f.flush();os.fsync(f.fileno())
        os.chmod(tmp,0o600);os.replace(tmp,CFG)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def set_mode(enabled,allow_unknown=False):
    if os.geteuid()!=0:raise PermissionError('Run with sudo')
    _save({'enabled':bool(enabled),'allow_unknown':bool(allow_unknown),'notes':'Host sleep is unaffected if DoomsdayOS runs inside a VM'})
    subprocess.run(['systemctl','enable' if enabled else 'disable','--now' if enabled else '--now','doomsday-always-on.service'],check=False)

def loop(poll=45):
    child=None
    def close():
        nonlocal child
        if child is not None:
            child.terminate()
            try:child.wait(timeout=3)
            except subprocess.TimeoutExpired:child.kill();child.wait()
            child=None
    signal.signal(signal.SIGTERM,lambda *args:(_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        while True:
            eligible=should_inhibit(settings(),ac_status())
            if eligible and (child is None or child.poll() is not None):
                close()
                child=subprocess.Popen(['systemd-inhibit','--what=sleep:idle','--mode=block',
                    '--why=DoomsdayOS always-on offline library (user enabled)',
                    '/usr/bin/sleep','infinity'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if not eligible:close()
            time.sleep(poll)
    except KeyboardInterrupt:pass
    finally:close()

def main(argv=None):
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='cmd',required=True)
    sub.add_parser('status');en=sub.add_parser('enable');en.add_argument('--allow-unknown-power',action='store_true');sub.add_parser('disable');sub.add_parser('run')
    args=p.parse_args(argv)
    if args.cmd=='status':print(json.dumps({'power':ac_status(),'config':settings(),'inhibiting':should_inhibit(settings(),ac_status()),'host_sleep':'not controlled when running in a VM'},indent=2))
    elif args.cmd=='enable':set_mode(True,args.allow_unknown_power)
    elif args.cmd=='disable':set_mode(False)
    else:loop()
if __name__=='__main__':main()
