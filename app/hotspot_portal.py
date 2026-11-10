#!/usr/bin/env python3
"""Read-only local hotspot landing page (no model, diagnostics or file access)."""
from __future__ import annotations
import html,ipaddress,json,subprocess
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

PORT=8780

def hotspot_address(device):
    if not device or any(ch not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-' for ch in device):
        raise ValueError('Invalid Wi-Fi device')
    p=subprocess.run(['ip','-j','-4','addr','show','dev',device],capture_output=True,text=True,timeout=5)
    if p.returncode:raise RuntimeError('Hotspot interface unavailable')
    rows=json.loads(p.stdout)
    for row in rows:
        for addr in row.get('addr_info',[]):
            if addr.get('scope')=='global':
                ip=ipaddress.ip_address(addr['local'])
                if ip.version==4 and ip.is_private and not ip.is_loopback:return str(ip)
    raise RuntimeError('Wi-Fi AP has no private IPv4 address')

def active_ap():
    import wifi_manager
    cfg=wifi_manager.settings()
    if not cfg.get('enabled'):raise RuntimeError('Hotspot not enabled')
    result=wifi_manager.run('nmcli','-t','-f','NAME,TYPE,DEVICE','con','show','--active')
    for line in result.stdout.splitlines():
        parts=line.split(':')
        if len(parts)>=3 and parts[0]==wifi_manager.PROFILE and parts[2]:
            return hotspot_address(parts[2])
    raise RuntimeError('No active Doomsday-Net hotspot')

def landing(address):
    address=html.escape(address)
    return (f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>DoomsdayOS Local Library</title>
    <style>body{{background:#0e1720;color:#ecf4f0;font:18px system-ui;max-width:650px;margin:12vh auto;padding:24px}}a{{display:block;padding:24px;border:1px solid #47564e;border-radius:12px;color:#d1fcba;margin:16px 0}}small{{color:#adbdba}}</style>
    <h1>DoomsdayOS · Offline Vault</h1><p>You are connected to the local Wi-Fi library. No Internet is required.</p>
    <a href="http://{address}:8080/">Open Kiwix archives and offline Wikipedia</a>
    <small>The AI workspace and privileged maintenance stay private on the vault computer. Files are served from verified archives only.</small></html>''').encode()

class Portal(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/','/health'):self.send_error(404);return
        content=landing(self.server.server_address[0]) if self.path=='/' else b'OK'
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8' if self.path=='/' else 'text/plain');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(content)));self.end_headers();self.wfile.write(content)

def main():
    address=active_ap()
    print('Hotspot page at http://'+address+':8780',flush=True)
    ThreadingHTTPServer((address,PORT),Portal).serve_forever()
if __name__=='__main__':main()
