#!/usr/bin/env python3
"""Explicit llama.cpp model registration and opt-in Hugging Face catalog.

Model selection does not download anything. Model download and activation are
separate manual commands. All inference endpoints bind to localhost.
"""
from __future__ import annotations
import argparse, json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
from decision_router import inference_profile,llama_args

CONFIG=Path(os.environ.get('DOOMSDAY_AI_MODELS','/etc/doomsday-vault/ai-models.json'))
ROLES={'text':8081,'decision':8082,'vision':8083}
SUGGESTIONS=['Spark X2.6 1.7B','Spark X2.6 4B','Bonsai 27B','Orith A3B','Qwopus 3.5 9B','LFM 2.3 Omni 2B','EmbeddingGemma 2 (multimodal decision/retrieval)']


def load_config():
    try:return json.loads(CONFIG.read_text())
    except FileNotFoundError:return {'models':{}}


def save_config(data):
    CONFIG.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=CONFIG.parent,prefix='.ai-models.')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,CONFIG)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def register(role,path,mmproj=None):
    if role not in ROLES:raise ValueError('Unknown role')
    model=Path(path).expanduser().resolve()
    if not model.is_file() or model.suffix.lower()!='.gguf':raise ValueError('Expected an existing local GGUF file')
    if mmproj:
        projector=Path(mmproj).expanduser().resolve()
        if not projector.is_file() or projector.suffix.lower()!='.gguf':raise ValueError('Expected a local .gguf multimedia projector')
    else:projector=None
    d=load_config()
    d.setdefault('models',{})[role]={'file':str(model),**({'mmproj':str(projector)} if projector else {})}
    save_config(d)
    return d['models'][role]


def command_for(role):
    if role not in ROLES:raise ValueError('Unknown role')
    entry=load_config().get('models',{}).get(role)
    if not entry:raise RuntimeError('No model registered for '+role+'. Register a local GGUF first.')
    exe=shutil.which('llama-server')
    if not exe:raise RuntimeError('llama-server is not installed; build and install ggml-org/llama.cpp first')
    profile=inference_profile()
    # Conservative GPU memory guard based on actual GGUF file size.
    try:model_bytes=Path(entry['file']).stat().st_size
    except OSError as e:raise RuntimeError('GGUF file missing: '+entry['file']) from e
    from llama_backend import choose
    runtime=choose(exe)
    if runtime['backend']=='cpu':
        profile['gpu_layers']=0
    elif runtime['backend']=='cuda':
        # NVIDIA can report free VRAM. Keep conservative guards.
        vram=profile['vram_free_mib']*1048576
        if vram<model_bytes*1.5:profile['gpu_layers']=0
        elif vram<model_bytes*2:profile['gpu_layers']=min(12,max(1,profile['gpu_layers']))
        else:profile['gpu_layers']=99
    else:
        # Vulkan/ROCm VRAM measurements are not portable. Start with partial
        # offload, allow llama.cpp to reject unsupported memory configurations.
        profile['gpu_layers']=12
    argv=llama_args(entry['file'],profile,ROLES[role],exe)
    if role=='text':argv+=['--jinja']
    if role=='decision':
        argv+=['--embedding','--pooling','mean']
    if role in ('vision','decision') and entry.get('mmproj'):
        argv+=['--mmproj',entry['mmproj']]
    return argv


def start_role(role,systemctl=subprocess.run):
    """Manually start the manager, then request one role. NEVER enable at boot."""
    if role not in ROLES:raise ValueError('Invalid model role')
    if os.geteuid()!=0:raise RuntimeError('Run sudo doomsday-ai start '+role)
    if role not in load_config().get('models',{}):
        raise RuntimeError('Download/register the selected '+role+' GGUF before starting')
    systemctl(['systemctl','start','doomsday-model-manager.service'],check=True)
    from model_scheduler import call,SOCKET
    for _ in range(60):
        if SOCKET.exists():break
        time.sleep(0.2)
    else:raise RuntimeError('Local model manager did not start; check systemctl status doomsday-model-manager')
    lease=call('/acquire',{'role':role})
    try:print('Started '+role+' model on local port '+str(lease['port'])+'. Not enabled at boot.')
    finally:call('/release',{'token':lease['token']})


def stop_role(role,systemctl=subprocess.run):
    if role!='all' and role not in ROLES:raise ValueError('Invalid model role')
    if os.geteuid()!=0:raise RuntimeError('Run sudo doomsday-ai stop '+role)
    if role=='all':
        systemctl(['systemctl','stop','doomsday-model-manager.service'],check=False)
        for name in ROLES:
            systemctl(['systemctl','stop','doomsday-llama-'+name+'.service'],check=False)
    else:
        from model_scheduler import call,SOCKET
        if SOCKET.exists():call('/unload',{'role':role})
        systemctl(['systemctl','stop','doomsday-llama-'+role+'.service'],check=False)
    print('Stopped '+role+'. Registered GGUF files are preserved.')


def main():
    p=argparse.ArgumentParser(description='DoomsdayOS locally selected llama.cpp models')
    sub=p.add_subparsers(dest='cmd',required=True)
    x=sub.add_parser('register');x.add_argument('role',choices=ROLES);x.add_argument('file');x.add_argument('--mmproj')
    x=sub.add_parser('serve');x.add_argument('role',choices=ROLES)
    sub.add_parser('list');sub.add_parser('recommend');sub.add_parser('status')
    sub.add_parser('catalog');sub.add_parser('configure')
    x=sub.add_parser('choose');x.add_argument('role',choices=ROLES);x.add_argument('model_id');x.add_argument('--repo');x.add_argument('--file');x.add_argument('--mmproj')
    x=sub.add_parser('download');x.add_argument('role',choices=ROLES)
    x=sub.add_parser('start');x.add_argument('role',choices=ROLES)
    x=sub.add_parser('stop');x.add_argument('role',choices=(*ROLES,'all'))
    # Retain the historical command as a harmless alias that starts only on explicit invocation.
    sub.add_parser('auto-start')
    a=p.parse_args()
    if a.cmd=='register':
        print('Registered local file (not started):',register(a.role,a.file,a.mmproj))
    elif a.cmd=='catalog':
        from model_catalog import display
        display()
    elif a.cmd=='configure':
        from model_catalog import configure
        configure()
    elif a.cmd=='choose':
        from model_catalog import choose
        print('Selected, not downloaded or started:',choose(a.role,a.model_id,a.repo,a.file,a.mmproj))
    elif a.cmd=='download':
        from model_catalog import download
        print('Downloaded and registered; not started:',download(a.role))
    elif a.cmd=='start':start_role(a.role)
    elif a.cmd=='stop':stop_role(a.role)
    elif a.cmd=='list':print(json.dumps(load_config(),indent=2))
    elif a.cmd=='status':
        from model_scheduler import call,SOCKET
        if not SOCKET.exists():
            print(json.dumps({'policy':'stopped (manual start only)','loaded':[],
                  'registered':sorted(load_config().get('models',{})),
                  'selected':load_config().get('selections',{})},indent=2))
        else:print(json.dumps(call('/status'),indent=2))
    elif a.cmd=='auto-start':
        print('Automatic startup is intentionally disabled. Use: sudo doomsday-ai start text')
    elif a.cmd=='recommend':
        from model_catalog import display
        print('Hardware profile:',json.dumps(inference_profile(),indent=2));display()
    elif a.cmd=='serve':
        argv=command_for(a.role)
        print('Starting local '+a.role+' llama.cpp engine:',argv,flush=True)
        os.execv(argv[0],argv)

if __name__=='__main__':main()
