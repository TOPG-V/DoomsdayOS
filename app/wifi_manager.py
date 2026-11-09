#!/usr/bin/env python3
"""Conservative AP-capable Wi-Fi discovery and NetworkManager hotspot manager.
Never replaces an active Wi-Fi association or enables a WEP hotspot.
"""
import json, os, re, secrets, subprocess, sys
from pathlib import Path

CFG=Path(os.environ.get('DOOMSDAY_WIFI_CONFIG','/etc/doomsday-vault/wifi.json'))
PROFILE='Doomsday-Net'

def run(*args):
    return subprocess.run(args,text=True,capture_output=True)

def settings():
    if not CFG.exists():return {'enabled':False}
    return json.loads(CFG.read_text())

def wifi_devices():
    rows=[]
    r=run('nmcli','-t','-f','DEVICE,TYPE,STATE','device','status')
    if r.returncode:return rows
    for line in r.stdout.splitlines():
        parts=line.split(':',2)
        if len(parts)<3:continue
        dev,typ,state=parts
        if typ!='wifi' or not re.fullmatch(r'[A-Za-z0-9_.-]+',dev):continue
        # Avoid guessing a USB Wi-Fi adapter is AP capable from the model/brand.
        phy_dir=Path('/sys/class/net')/dev/'phy80211'
        if not phy_dir.exists():continue
        phy=phy_dir.resolve().name
        info=run('iw','phy',phy,'info')
        modes=info.stdout.split('Supported interface modes:',1)[-1].split('Band 1:',1)[0]
        supports_ap=info.returncode==0 and bool(re.search(r'^\s*\*\s+AP\s*$',modes,re.MULTILINE))
        rows.append({'interface':dev,'phy':phy,'state':state,'ap':supports_ap})
    return rows

def hotspot_status():
    for x in wifi_devices():print(f"{x['interface']}: state={x['state']} AP={'yes' if x['ap'] else 'no'}")
    r=run('nmcli','-t','-f','NAME,TYPE,DEVICE','connection','show','--active')
    print('Active:',r.stdout.strip() or '(none)')

def ensure_hotspot():
    cfg=settings()
    if not cfg.get('enabled'):
        print('Hotspot not enrolled (safe default).');return
    candidates=[x for x in wifi_devices() if x['ap'] and x['state']=='disconnected']
    active=run('nmcli','-t','-f','NAME,TYPE,DEVICE','connection','show','--active')
    if PROFILE+':' in active.stdout:
        print('Hotspot already running.');return
    if not candidates:
        print('No idle AP-capable Wi-Fi device. Will retry on next hardware scan, without disconnecting an existing Wi-Fi connection.');return
    dev=candidates[0]['interface']
    check=run('nmcli','-g','connection.id','con','show',PROFILE)
    password=cfg.get('password','')
    if not (12<=len(password)<=63):
        print('No secure password configured; refusing to start hotspot.');return
    if check.returncode:
        # Add a secured WPA2 profile rather than nmcli's WEP fallback hotspot shortcut.
        cmd=['nmcli','con','add','type','wifi','ifname',dev,'con-name',PROFILE,'ssid',cfg.get('ssid',PROFILE),'802-11-wireless.mode','ap','wifi-sec.key-mgmt','wpa-psk','wifi-sec.proto','rsn','wifi-sec.psk',password,'ipv4.method','shared','ipv6.method','disabled','connection.autoconnect','yes']
        res=run(*cmd)
        if res.returncode:
            print('Unable to create WPA2 AP profile:',res.stderr.strip());return
    else:
        # Existing profile must still be a Wi-Fi AP; don't hijack unrelated profile.
        mode=run('nmcli','-g','802-11-wireless.mode','con','show',PROFILE)
        if mode.stdout.strip()!='ap':print('Name collision: existing connection is not an AP; refusing.');return
        # Rebind only the profile we own if interface names change on a different host.
        run('nmcli','con','mod',PROFILE,'connection.interface-name',dev)
    res=run('nmcli','con','up',PROFILE,'ifname',dev)
    print('Hotspot active on '+dev if res.returncode==0 else 'Hotspot activation failed: '+res.stderr.strip())

def configure():
    print('Available wireless adapters:');hotspot_status()
    eligible=[x for x in wifi_devices() if x['ap']]
    if not eligible:
        print('No AP-capable dongle currently detected. Can configure later.');return
    reply=input('Enable automatic offline hotspot on an otherwise unused AP-capable adapter? [y/N]: ').strip().lower()
    if reply!='y':print('Hotspot remains disabled.');return
    secret=secrets.token_urlsafe(18)
    ssid=input('SSID [Doomsday-Net]: ').strip() or 'Doomsday-Net'
    if not (1<=len(ssid.encode('utf-8'))<=32):print('Invalid SSID');return
    CFG.parent.mkdir(parents=True,exist_ok=True)
    data={'enabled':True,'ssid':ssid,'password':secret}
    tmp=CFG.with_suffix('.json.new')
    with open(tmp,'w') as out:json.dump(data,out);out.write('\n')
    os.chmod(tmp,0o600);os.replace(tmp,CFG)
    print('New hotspot password (save it securely): '+secret)
    print('NetworkManager will manage DHCP on the AP. Existing Wi-Fi connections are never displaced.')
    ensure_hotspot()

if __name__=='__main__':
    modes={'scan':hotspot_status,'ensure':ensure_hotspot,'setup':configure}
    mode=sys.argv[1] if len(sys.argv)>1 else 'scan'
    if mode not in modes:sys.exit('Usage: wifi_manager.py scan|ensure|setup')
    modes[mode]()
