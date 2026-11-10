#!/usr/bin/env python3
"""Local, bounded llama.cpp model residency manager.

Owns llama-server children and a root-only Unix socket. The browser talks to
its restricted client through the same host; no public control endpoint.
Loads main + decision concurrently only when a conservative RAM budget fits,
and otherwise switches roles after outstanding inference leases finish.
"""
from __future__ import annotations
import argparse, contextlib, http.client, json, os, secrets, signal, socket, socketserver, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from llama_runtime import ROLES, load_config, command_for
from decision_router import usable_ram_mib

SOCKET = Path(os.environ.get('DOOMSDAY_MODEL_SOCKET','/run/doomsday-vault/model-manager.sock'))
MIB = 1024**2
ROLES_PORTS = ROLES


def mem_total_mib():
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            if line.startswith('MemTotal:'):return int(line.split()[1])//1024
    except (OSError,ValueError,IndexError):pass
    return 0


def estimate_mib(entry):
    """Conservative full-host RAM reservation per model incl. KV and mmap overhead."""
    path=Path(entry['file'])
    size=path.stat().st_size
    if entry.get('mmproj'):size+=Path(entry['mmproj']).stat().st_size
    return max(512,int((size/MIB)*1.30)+512)


def memory_budget(total,available,resident_mib=0):
    """Leave at least 2 GiB or 20% of RAM to OS and other services."""
    reserve=max(2048,int(total*0.20))
    return max(0,min(total-reserve,available+resident_mib-reserve))


def determine_policy(entries,total_mib,available_mib):
    """Pure conservative scheduler recommendation for offline unit testing."""
    sizes={role:estimate_mib(entry) for role,entry in entries.items() if role in ROLES}
    budget=memory_budget(total_mib,available_mib)
    wants=('text','decision')
    both=all(x in sizes for x in wants) and sum(sizes[x] for x in wants)<=budget
    return {'policy':'not-configured' if not sizes else 'keep-both' if both else 'switch-one-at-a-time',
            'usable_budget_mib':budget,'model_estimates_mib':sizes,
            'combined_text_decision_mib':sum(sizes.get(x,0) for x in wants)}


class Manager:
    def __init__(self,spawn=None,health=None,ram=None,total=None,clock=None):
        self.spawn=spawn or (lambda argv:subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=None,stderr=None,start_new_session=True))
        self.health=health or self._health
        self.ram=ram or usable_ram_mib
        self.total=total or mem_total_mib
        self.clock=clock or time.monotonic
        self.lock=threading.RLock()
        self.procs={};self.leases={};self.last_use={}

    @staticmethod
    def _health(role):
        with urllib.request.urlopen('http://127.0.0.1:'+str(ROLES[role])+'/health',timeout=1.3) as response:
            return response.status==200

    def _prune(self):
        now=self.clock()
        self.leases={key:rec for key,rec in self.leases.items() if rec['expires']>now}
        self.procs={role:proc for role,proc in self.procs.items() if proc.poll() is None}

    def _usage(self):
        entries=load_config().get('models',{})
        sizes={role:estimate_mib(entries[role]) for role in self.procs if role in entries}
        return entries,sizes

    def status(self):
        with self.lock:
            self._prune();entries,sizes=self._usage()
            recommendation=determine_policy(entries,self.total(),self.ram()+sum(sizes.values()))
            return {**recommendation,'loaded':sorted(self.procs),
                    'busy':sorted({rec['role'] for rec in self.leases.values()}),
                    'registered':sorted(r for r in entries if r in ROLES),
                    'model_ports':{r:ROLES[r] for r in self.procs}}

    def _stop_role(self,role):
        proc=self.procs.pop(role,None)
        if proc is None:return
        proc.terminate()
        try:proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=5)
        self.last_use.pop(role,None)

    def acquire(self,role,timeout=95):
        if role not in ROLES:raise ValueError('Unsupported model role')
        with self.lock:
            self._prune()
            entries,sizes=self._usage()
            if role not in entries:raise RuntimeError('No registered '+role+' GGUF. Use: sudo doomsday-ai register '+role+' /path/model.gguf')
            size=estimate_mib(entries[role])
            resident=sum(sizes.values())
            budget=memory_budget(self.total(),self.ram(),resident)
            if size>budget:
                raise RuntimeError(f'Model {role} needs approximately {size} MiB; only {budget} MiB safe capacity. Choose a smaller GGUF or free RAM.')
            busy={rec['role'] for rec in self.leases.values()}
            if role not in self.procs:
                # Keep both models if capacity allows; otherwise evict only IDLE roles.
                for candidate in sorted(self.procs,key=lambda r:self.last_use.get(r,0)):
                    if resident+size<=budget:break
                    if candidate in busy:continue
                    self._stop_role(candidate);resident-=sizes[candidate]
                if resident+size>budget:
                    raise RuntimeError('Not enough free RAM/VRAM while another model is answering. Retry after the active request finishes.')
                argv=command_for(role)
                proc=self.spawn(argv);self.procs[role]=proc
                try:
                    until=self.clock()+timeout
                    while self.clock()<until:
                        if proc.poll() is not None:raise RuntimeError(role+' llama.cpp server exited during startup')
                        try:
                            if self.health(role):break
                        except (OSError,TimeoutError):pass
                        time.sleep(0.35)
                    else:raise RuntimeError(role+' llama.cpp server did not become healthy')
                except Exception:
                    self._stop_role(role)
                    raise
            token=secrets.token_hex(16)
            self.leases[token]={'role':role,'expires':self.clock()+360}
            self.last_use[role]=self.clock()
            return {'token':token,'role':role,'port':ROLES[role],'policy':self.status()['policy']}

    def release(self,token):
        with self.lock:
            return self.leases.pop(token,None) is not None

    def preload(self):
        # First text; decision only if both can fit. Do not prewarm optional vision.
        entries=load_config().get('models',{})
        roles=['text'] if 'text' in entries else []
        policy=determine_policy(entries,self.total(),self.ram())
        if policy['policy']=='keep-both':roles.append('decision')
        for role in roles:
            try:
                lease=self.acquire(role);self.release(lease['token'])
            except (RuntimeError,OSError,ValueError) as exc:
                print('Model preload skipped:',str(exc),flush=True)

    def close(self):
        with self.lock:
            for role in list(self.procs):self._stop_role(role)
            self.leases.clear()


class UnixHTTPServer(socketserver.ThreadingMixIn,socketserver.UnixStreamServer):
    daemon_threads=True
    allow_reuse_address=True


class Handler(BaseHTTPRequestHandler):
    server_version='DoomsdayModelManager/1.0'
    def log_message(self,*args):pass
    def respond(self,data,code=200):
        blob=json.dumps(data).encode();self.send_response(code)
        self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(blob)))
        self.end_headers();self.wfile.write(blob)
    def do_GET(self):
        if self.path=='/status':return self.respond(self.server.manager.status())
        self.respond({'error':'No such endpoint'},404)
    def do_POST(self):
        try:
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=4096:raise ValueError('Invalid command size')
            obj=json.loads(self.rfile.read(size));path=self.path
            if path=='/acquire':result=self.server.manager.acquire(obj.get('role'))
            elif path=='/release':result={'released':self.server.manager.release(str(obj.get('token','')))}
            else:return self.respond({'error':'No such endpoint'},404)
            self.respond(result)
        except (ValueError,RuntimeError,OSError) as exc:self.respond({'error':str(exc)[:350]},409)


class UnixConnection(http.client.HTTPConnection):
    def __init__(self,socket_path,timeout=120):
        super().__init__('localhost',timeout=timeout);self.socket_path=str(socket_path)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout);self.sock.connect(self.socket_path)


def call(path,obj=None,timeout=120):
    conn=UnixConnection(SOCKET,timeout=timeout)
    try:
        body=json.dumps(obj).encode() if obj is not None else None
        conn.request('POST' if body else 'GET',path,body,{'Content-Type':'application/json'} if body else {})
        response=conn.getresponse();data=json.loads(response.read(65536))
        if response.status!=200:raise RuntimeError(data.get('error','Local model manager unavailable'))
        return data
    finally:conn.close()


@contextlib.contextmanager
def use_model(role):
    if not SOCKET.exists():
        # Legacy installations can still run manually launched model services.
        yield None;return
    lease=call('/acquire',{'role':role})
    try:yield lease
    finally:
        try:call('/release',{'token':lease['token']},timeout=8)
        except (RuntimeError,OSError):pass


def serve():
    SOCKET.parent.mkdir(parents=True,exist_ok=True)
    if SOCKET.exists():SOCKET.unlink()
    manager=Manager()
    server=UnixHTTPServer(str(SOCKET),Handler);server.manager=manager
    os.chmod(SOCKET,0o600)
    def terminate(*_):
        # Thread-safe shutdown on signal.
        threading.Thread(target=server.shutdown,daemon=True).start()
    signal.signal(signal.SIGTERM,terminate);signal.signal(signal.SIGINT,terminate)
    try:
        manager.preload();server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close();manager.close();SOCKET.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description='Local llama.cpp model residency manager')
    p.add_argument('command',choices=('serve','status','preload'))
    a=p.parse_args()
    if a.command=='serve':serve()
    elif a.command=='status':print(json.dumps(call('/status'),indent=2))
    else:
        lease=call('/acquire',{'role':'text'})
        call('/release',{'token':lease['token']})
        print('Text model loaded with adaptive policy; check: sudo doomsday-ai status')

if __name__=='__main__':main()
