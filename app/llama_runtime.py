#!/usr/bin/env python3
"""Explicit, local-only llama.cpp model registration and hardware-aware launch.

The user must provide installed compatible .gguf models. No auto-download,
network access, or silently selected large weights. Role isolation keeps the
small decision/embedding model separate from heavier text/vision inference.
"""
from __future__ import annotations
import argparse, json,os,shutil,subprocess,sys,tempfile
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
    vram=profile['vram_free_mib']*1048576
    if vram<model_bytes*1.5:profile['gpu_layers']=0
    elif vram<model_bytes*2:profile['gpu_layers']=min(profile['gpu_layers'],12)
    argv=llama_args(entry['file'],profile,ROLES[role],exe)
    if role=='text':argv+=['--jinja']
    if role=='decision':
        argv+=['--embedding','--pooling','mean']
    if role=='vision' and entry.get('mmproj'):
        argv+=['--mmproj',entry['mmproj']]
    return argv


def main():
    p=argparse.ArgumentParser()
    sub=p.add_subparsers(dest='cmd',required=True)
    x=sub.add_parser('register');x.add_argument('role',choices=ROLES);x.add_argument('file');x.add_argument('--mmproj')
    x=sub.add_parser('serve');x.add_argument('role',choices=ROLES)
    sub.add_parser('list');sub.add_parser('recommend');sub.add_parser('status');sub.add_parser('auto-start')
    a=p.parse_args()
    if a.cmd=='register':
        print('Registered:',register(a.role,a.file,a.mmproj))
        # Refresh the local residency manager so it considers newly registered models.
        if os.geteuid()==0 and shutil.which('systemctl'):
            subprocess.run(['systemctl','try-restart','doomsday-model-manager.service'],check=False,timeout=8)
    elif a.cmd=='list':print(json.dumps(load_config(),indent=2))
    elif a.cmd=='status':
        from model_scheduler import call
        print(json.dumps(call('/status'),indent=2))
    elif a.cmd=='auto-start':
        if os.geteuid()!=0:raise RuntimeError('Run auto-start with sudo')
        subprocess.run(['systemctl','enable','--now','doomsday-model-manager.service'],check=True)
        print('Adaptive model manager started. Register GGUF models then check: sudo doomsday-ai status')
    elif a.cmd=='recommend':
        print('Hardware profile:',json.dumps(inference_profile(),indent=2))
        print('Suggested user-requested model labels (verify compatible GGUF builds before choosing):')
        for s in SUGGESTIONS:print(' -',s)
        print('Weights are NOT automatically downloaded.')
    elif a.cmd=='serve':
        argv=command_for(a.role)
        print('Starting local '+a.role+' llama.cpp engine:',argv,flush=True)
        os.execv(argv[0],argv)

if __name__=='__main__':main()
