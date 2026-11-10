#!/usr/bin/env python3
"""Discover all guest Wi-Fi radios; never steal an active association without consent.

Windows Mobile Hotspot / Wi-Fi Direct does not prove a Linux AP driver exists.
On a VirtualBox guest, host Wi-Fi usually appears as virtual Ethernet, not Wi-Fi.
"""
from __future__ import annotations
import argparse, json, os, re, secrets, subprocess, sys
from pathlib import Path

CFG=Path(os.environ.get('DOOMSDAY_WIFI_CONFIG','/etc/doomsday-vault/wifi.json'))
PROFILE='Doomsday-Net'
INTERFACE=re.compile(r'^[A-Za-z0-9_.-]{1,32}$')


def run(*args):
    return subprocess.run(args,text=True,capture_output=True,timeout=15)


def settings():
    if not CFG.exists():return {'enabled':False}
    return json.loads(CFG.read_text())


def supports_ap(iw_output):
    """Check only 'Supported interface modes', not an unrelated 'AP' elsewhere."""
    section=False
    for line in (iw_output or '').splitlines():
        if re.match(r'^\s*Supported interface modes\s*:',line):
            section=True;continue
        if section:
            if re.match(r'^\s*\*\s*AP\s*$',line):return True
            if line and not line[0].isspace():section=False
    return False


def wifi_devices():
    result=run('nmcli','-t','-f','DEVICE,TYPE,STATE','device','status')
    if result.returncode:return []
    devices=[]
    for line in result.stdout.splitlines():
        parts=line.split(':',2)
        if len(parts)!=3:continue
        dev,kind,state=parts
        if kind!='wifi' or not INTERFACE.fullmatch(dev):continue
        phy_link=Path('/sys/class/net')/dev/'phy80211'
        phy=phy_link.resolve().name if phy_link.exists() else None
        reason='';supported=False
        if phy:
            try:
                info=run('iw','phy',phy,'info')
                supported=info.returncode==0 and supports_ap(info.stdout)
                if info.returncode:reason='iw cannot inspect the adapter; install iw or check Linux driver'
                elif not supported:reason='Linux driver does not advertise AP mode (Windows Wi-Fi Direct may differ)'
            except (OSError,subprocess.TimeoutExpired):reason='iw missing or timed out'
        else:reason='No Linux wireless PHY visible for this interface'
        devices.append({'interface':dev,'phy':phy,'state':state,'ap':supported,'reason':reason})
    return devices


def guest_hint(devices=None):
    devices=wifi_devices() if devices is None else devices
    if devices:return None
    try:
        product=Path('/sys/class/dmi/id/product_name').read_text().strip()
    except OSError:product=''
    if 'virtualbox' in product.casefold():
        return ('VirtualBox shows host Wi-Fi as virtual Ethernet by default. In VirtualBox Devices > USB, '
                'attach the actual USB Wi-Fi function to Ubuntu (the host loses access), or keep '
                'Windows Mobile hotspot and forward the Kiwix port from the VM to Windows.')
    return ('No Wi-Fi interface is visible to Ubuntu. Check USB pass-through, the wireless driver, '
            'rfkill and nmcli device status; a Bluetooth-only USB interface cannot broadcast Wi-Fi.')


def hotspot_status():
    devices=wifi_devices()
    for x in devices:
        extra=' ('+x['reason']+')' if x['reason'] else ''
        print(f"{x['interface']}: state={x['state']}, Linux AP={'yes' if x['ap'] else 'no'}"+extra)
    if not devices:print(guest_hint(devices))
    try:
        active=run('nmcli','-t','-f','NAME,TYPE,DEVICE','connection','show','--active')
        print('Active:',active.stdout.strip() or '(none)')
    except (OSError,subprocess.TimeoutExpired) as exc:print('Unable to inspect active connections:',exc)


def _active_profile():
    active=run('nmcli','-t','-f','NAME,TYPE,DEVICE','connection','show','--active')
    if active.returncode:return None
    for line in active.stdout.splitlines():
        bits=line.split(':',2)
        if len(bits)==3 and bits[0]==PROFILE:return bits[2]
    return None


def _saved_config(ssid,password,interface):
    if not INTERFACE.fullmatch(interface):raise ValueError('Invalid Wi-Fi interface')
    if not (1<=len(ssid.encode('utf-8'))<=32):raise ValueError('SSID must be 1–32 bytes')
    if not (12<=len(password)<=63):raise ValueError('WPA2 password must be 12–63 characters')
    CFG.parent.mkdir(parents=True,exist_ok=True)
    path=CFG.with_suffix('.json.new')
    try:
        with open(path,'w') as stream:
            os.fchmod(stream.fileno(),0o600)
            json.dump({'enabled':True,'ssid':ssid,'password':password,'interface':interface},stream)
            stream.write('\n');stream.flush();os.fsync(stream.fileno())
        os.replace(path,CFG)
    finally:
        path.unlink(missing_ok=True)


def ensure_hotspot(interface=None,replace=False, input_fn=input):
    """Auto mode uses idle adapters; manual replacement needs typed confirmation."""
    cfg=settings()
    if not cfg.get('enabled'):
        print('Hotspot not configured. Run: sudo doomsday-wifi setup');return False
    running=_active_profile()
    if running:
        print('Doomsday hotspot already active on '+running);return True
    available=[d for d in wifi_devices() if d['ap']]
    desired=interface or cfg.get('interface')
    candidates=[d for d in available if d['interface']==desired] if desired else available
    if not candidates:
        print('No suitable Linux AP adapter is visible.')
        print(guest_hint(wifi_devices()) or 'The Wi-Fi adapter is present, but Linux does not advertise AP mode. Run sudo doomsday-wifi scan.')
        return False
    idle=[d for d in candidates if d['state']=='disconnected']
    if idle:device=idle[0]
    else:
        device=candidates[0]
        print(f"{device['interface']} is currently {device['state']}, not idle. Linux must take it over for hotspot use.")
        if not replace:
            print('Automatic startup will not disconnect Wi-Fi. For a manual switch run:')
            print(f'  sudo doomsday-wifi activate --interface {device["interface"]} --replace')
            return False
        confirmation=input_fn(f'This will disconnect {device["interface"]} from its current network. Type SWITCH {device["interface"]}: ').strip()
        if confirmation!='SWITCH '+device['interface']:
            print('Cancelled. Existing connection unchanged.');return False
    dev=device['interface']
    secret=cfg.get('password','')
    if not (12<=len(secret)<=63):
        print('Secure hotspot password missing; run setup again.');return False
    exists=run('nmcli','-g','connection.id','con','show',PROFILE)
    if exists.returncode:
        args=('nmcli','con','add','type','wifi','ifname',dev,'con-name',PROFILE,
              'ssid',cfg.get('ssid',PROFILE),'802-11-wireless.mode','ap','wifi-sec.key-mgmt','wpa-psk',
              'wifi-sec.proto','rsn','wifi-sec.psk',secret,'ipv4.method','shared','ipv6.method','disabled',
              'connection.autoconnect','yes')
    else:
        mode=run('nmcli','-g','802-11-wireless.mode','con','show',PROFILE)
        if mode.returncode or mode.stdout.strip()!='ap':
            print('An unrelated NetworkManager profile already uses the hotspot name; refusing.');return False
        args=('nmcli','con','mod',PROFILE,'connection.interface-name',dev,
              '802-11-wireless.ssid',cfg.get('ssid',PROFILE),'wifi-sec.key-mgmt','wpa-psk',
              'wifi-sec.proto','rsn','wifi-sec.psk',secret,'ipv4.method','shared','ipv6.method','disabled')
    result=run(*args)
    if result.returncode:
        print('Cannot configure WPA2 profile:',result.stderr.strip());return False
    result=run('nmcli','con','up',PROFILE,'ifname',dev)
    if result.returncode:
        print('Linux driver/NetworkManager rejected hotspot activation:',result.stderr.strip())
        print('Windows hotspot capability does not necessarily mean Linux AP support.');return False
    print('Doomsday Wi-Fi hotspot active on '+dev)
    subprocess.run(['systemctl','enable','--now','doomsday-hotspot-portal.service'],capture_output=True,text=True,check=False)
    subprocess.run(['systemctl','try-restart','doomsday-hotspot-portal.service'],capture_output=True,text=True,check=False)
    try:
        from hotspot_portal import hotspot_address
        ip=hotspot_address(dev)
        print(f'Connected clients: http://{ip}:8780  ·  Kiwix: http://{ip}:8080')
    except (RuntimeError,OSError,ValueError):pass
    return True


def configure(input_fn=input):
    print('Available wireless adapters:');hotspot_status()
    devices=[x for x in wifi_devices() if x['ap']]
    if not devices:
        print('No Linux AP capable radio detected. Check USB pass-through or use Windows host hotspot.')
        return False
    for i,x in enumerate(devices,1):
        print(f'{i}. {x["interface"]} ({x["state"]})'+(' — will disconnect Wi-Fi when manually activated' if x['state']!='disconnected' else ''))
    answer=input_fn('Which Wi-Fi interface? [1, Enter cancels]: ').strip()
    if not answer:return False
    if not answer.isdecimal() or not 1<=int(answer)<=len(devices):
        print('Invalid interface choice');return False
    dev=devices[int(answer)-1]['interface']
    ssid=input_fn('Hotspot name [Doomsday-Net]: ').strip() or PROFILE
    password=secrets.token_urlsafe(18)
    _saved_config(ssid,password,dev)
    print('New hotspot password (store securely): '+password)
    print('Automatic activation never disconnects active Wi-Fi connections.')
    return ensure_hotspot(interface=dev,input_fn=input_fn)


def main(argv=None):
    parser=argparse.ArgumentParser(description='DoomsdayOS Wi-Fi discovery and local hotspot')
    parser.add_argument('command',nargs='?',choices=('scan','setup','ensure','activate'),default='scan')
    parser.add_argument('--interface')
    parser.add_argument('--replace',action='store_true',help='Manually disconnect the selected adapter after typed confirmation')
    args=parser.parse_args(argv)
    if args.interface and not INTERFACE.fullmatch(args.interface):parser.error('Invalid interface')
    if args.command=='scan':hotspot_status()
    elif args.command=='setup':configure()
    elif args.command=='ensure':ensure_hotspot()
    elif args.command=='activate':ensure_hotspot(interface=args.interface,replace=args.replace)

if __name__=='__main__':main()
