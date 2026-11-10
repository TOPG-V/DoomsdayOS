#!/usr/bin/env python3
"""Version-aware Kiwix launch and a safe empty-library landing page.

Only reads finished .zim files on enrolled ZFS archive mounts; never serves
staging paths or alters an archive. The downloader restarts this service after
publishing each new, independently SHA-verified ZIM.
"""
from __future__ import annotations
import os,shutil,subprocess,sys,tempfile,threading,time
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from zim_manager import config,guard_path
from dual_storage import locations,archive_config
from archive_guard import checked_archive

PORT=8080
LIBRARY=Path('/var/lib/doomsday-vault/kiwix/library.xml')

def ready_files(cfg):
    out=[]
    for loc in locations(cfg):
        c=archive_config(cfg,loc['key'])
        try:
            path,ds=checked_archive(c)
            guard_path(path,ds)
        except (RuntimeError,ValueError,OSError) as exc:
            if loc['key']=='primary':
                raise RuntimeError('Primary ZFS archive unavailable: '+str(exc)) from exc
            print('Secondary ZIM location unavailable:',exc,file=sys.stderr,flush=True)
            continue
        out.extend(f for f in sorted(path.glob('*.zim')) if f.is_file() and not f.is_symlink())
    return out

def archive_directories(cfg):
    return list(dict.fromkeys(str(f.parent) for f in ready_files(cfg)))

ready_directories=archive_directories

def diagnostics(print_fn=print):
    exe=shutil.which('kiwix-serve')
    if not exe:
        print_fn('KIWIX BINARY MISSING: sudo apt install kiwix-tools');return 3
    try:dirs=archive_directories(config())
    except (RuntimeError,KeyError,ValueError,OSError) as exc:
        print_fn('ARCHIVE UNAVAILABLE: '+str(exc));return 2
    if not dirs:
        print_fn('NO PUBLISHED ZIM yet. Port 8080 can serve the empty library page.');return 1
    print_fn('Kiwix is ready to serve: '+', '.join(dirs));return 0

def supported_options(exe):
    try:
        r=subprocess.run([exe,'--help'],capture_output=True,text=True,timeout=7,check=False)
        return r.stdout+r.stderr
    except (OSError,subprocess.TimeoutExpired):return ''

def serve_command(paths,exe,options=None):
    """Ubuntu Kiwix 3.5 lacks --skipInvalid; only use advertised flags."""
    options=supported_options(exe) if options is None else options
    cmd=[exe,'--port=8080']
    if '--address' in options:cmd.append('--address=0.0.0.0')
    if '--blockexternal' in options:cmd.append('--blockexternal')
    if '--skipInvalid' in options:cmd.append('--skipInvalid')
    return cmd+list(map(str,paths))

def create_library(paths,manage=None,location=LIBRARY):
    """Rebuild a separate Kiwix XML index; no ZIM bytes are copied or edited."""
    manage=shutil.which('kiwix-manage') if manage is None else manage
    if not manage:raise RuntimeError('kiwix-manage not installed')
    location.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.kiwix-build-',dir=location.parent) as d:
        staged=Path(d)/'library.xml'
        r=subprocess.run([manage,str(staged),'add',*map(str,paths)],capture_output=True,text=True,timeout=180,check=False)
        if r.returncode or not staged.is_file() or not staged.stat().st_size:
            raise RuntimeError('kiwix-manage failed: '+(r.stderr or r.stdout).strip()[:400])
        os.chmod(staged,0o644)
        os.replace(staged,location)
    return location

class ReusableServer(ThreadingHTTPServer):
    allow_reuse_address=True

class EmptyLibrary(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ('/','/health','/favicon.ico'):
            self.send_error(404);return
        if self.path=='/favicon.ico':self.send_response(204);self.end_headers();return
        if self.path=='/health':body=b'Kiwix waiting for a verified ZIM\n';mime='text/plain'
        else:
            body=("""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="15"><title>DoomsdayOS · Library</title><style>body{background:#0b1320;color:#eef7f5;font:16px system-ui;min-height:80vh;display:grid;place-items:center;margin:0}main{max-width:560px;padding:40px;background:#162433;border:1px solid #34566b;border-radius:20px}small{color:#9db2c4}a{color:#a6ecd5}</style><main><small>DOOMSDAY OS · OFFLINE LIBRARY</small><h1>Library is getting ready.</h1><p>Port 8080 is working. When the first ZIM finishes downloading and passes verification, its articles will be shown here.</p><p><a href="http://localhost:8765">Go to the DoomsdayOS workspace</a></p><small>This page refreshes every 15 seconds.</small></main></html>""").encode();mime='text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type',mime)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers();self.wfile.write(body)
    def log_message(self,*args):pass

def wait_for_archive(scan=None,exe_available=None):
    scan=scan or (lambda:ready_files(config()))
    exe_available=exe_available or (lambda:shutil.which('kiwix-serve'))
    class Server(ThreadingHTTPServer):allow_reuse_address=True
    with Server(('0.0.0.0',PORT),EmptyLibrary) as server:
        server.timeout=4
        print('Empty Kiwix library available on port 8080',flush=True)
        while True:
            try:files=scan()
            except (KeyError,ValueError,RuntimeError,OSError) as exc:
                print('Waiting for safe archive mount:',exc,file=sys.stderr,flush=True);files=[]
            if files and exe_available():return files
            server.handle_request()

def current_files(cfg):
    """A small, read-only fingerprint of published archive names and lengths."""
    return tuple(sorted((str(p),p.stat().st_size) for p in ready_files(cfg)))

def start_standby():
    server=ReusableServer(('0.0.0.0',PORT),EmptyLibrary)
    server.worker=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.5},daemon=True)
    server.worker.start()
    print('Kiwix empty library page available on port 8080',flush=True)
    return server

def stop_standby(server):
    if server is not None:
        server.shutdown()
        server.server_close()
        server.worker.join(timeout=5)

def _stop_reader(process):
    if process is None or process.poll() is not None:return
    process.terminate()
    try:process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill();process.wait(timeout=5)

def main(argv=None):
    argv=[] if argv is None else argv
    if argv in (['--check'],['--diagnose']):return diagnostics()
    if argv:raise SystemExit('Usage: serve_zims.py [--check]')
    # A long-lived supervisor makes 8080 useful before the first ZIM and
    # refreshes Kiwix as files arrive, without touching a .part or re-downloading.
    process=None
    standby=None
    signature=None
    try:
        while True:
            try:
                cfg=config()
                dirs=archive_directories(cfg)
                fingerprint=current_files(cfg) if dirs else ()
            except (KeyError,ValueError,RuntimeError,OSError) as exc:
                print('Kiwix waiting for verified archive mount:',exc,file=sys.stderr,flush=True)
                dirs=[];fingerprint=()
            exe=shutil.which('kiwix-serve')
            if not exe and fingerprint:
                print('Cannot serve ZIM: install kiwix-tools',file=sys.stderr,flush=True)
            should_run=bool(exe and fingerprint)
            failed=process is not None and process.poll() is not None
            changed=fingerprint!=signature
            if not should_run or failed or changed:
                if process is not None:
                    code=process.poll()
                    if code is not None:print(f'kiwix-serve exited status={code}; rechecking configuration',file=sys.stderr,flush=True)
                    _stop_reader(process);process=None
            if should_run and process is None:
                if standby is not None:stop_standby(standby);standby=None
                files=[Path(name) for name,_ in fingerprint]
                paths=files
                if len(files)>1:
                    try:paths=[create_library(files)]
                    except (RuntimeError,OSError,subprocess.TimeoutExpired) as exc:
                        print('XML library unavailable, trying direct file paths:',exc,file=sys.stderr,flush=True)
                cmd=serve_command(paths,exe)
                if len(paths)==1 and str(paths[0])==str(LIBRARY):cmd.insert(1,'--library')
                print('Starting Kiwix:', ' '.join(map(str,cmd)),flush=True)
                try:process=subprocess.Popen(cmd)
                except OSError as exc:
                    print('Kiwix launch failed:',exc,file=sys.stderr,flush=True)
                signature=fingerprint
            if not should_run or process is None or failed:
                if standby is None:
                    try:standby=start_standby()
                    except OSError as exc:
                        print('Unable to bind Kiwix empty page on port 8080:',exc,file=sys.stderr,flush=True)
            # A small polling interval allows newly published files to appear
            # even if older download workers do not signal a service restart.
            time.sleep(10 if process is not None else 20)
    finally:
        _stop_reader(process)
        stop_standby(standby)

if __name__=='__main__':sys.exit(main(sys.argv[1:]))
