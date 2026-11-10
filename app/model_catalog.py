#!/usr/bin/env python3
"""Human-selected Hugging Face GGUF catalog for the local llama.cpp engine.

Selecting never downloads or starts a model. Downloading is a separate explicit
operation and activation is a third explicit operation. No remote code execution.
"""
from __future__ import annotations
import json, os, re, shutil, subprocess
from pathlib import Path

CATALOG=Path(__file__).with_name('model_catalog.json')
STORE=Path(os.environ.get('DOOMSDAY_MODEL_STORE','/var/lib/doomsday-vault/models'))
REPO=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$')
FILENAME=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,190}\.gguf$',re.I)


def catalog():
    items=json.loads(CATALOG.read_text(encoding='utf-8'))['models']
    return {m['id']:m for m in items}


def display():
    print('DoomsdayOS selectable GGUF models (NO automatic download or startup):')
    for i,m in enumerate(catalog().values(),1):
        print(f'{i:>2}. {m["id"]:20} {m["name"]}  [{m["role"]}]')
        if m.get('hf_repo'):print('    https://huggingface.co/'+m['hf_repo'])
        else:print('    Exact Hugging Face source not verified: supply repo/filename manually.')
    print('Selected models remain OFF until you explicitly run doomsday-ai start ROLE.')


def validate_source(repo,file,mmproj=None):
    if not isinstance(repo,str) or not REPO.fullmatch(repo):
        raise ValueError('Provide a Hugging Face owner/repository identifier')
    if not isinstance(file,str) or not FILENAME.fullmatch(file) or '..' in file:
        raise ValueError('Provide the exact GGUF filename from the repository')
    if mmproj and (not FILENAME.fullmatch(mmproj) or '..' in mmproj):
        raise ValueError('Invalid multimedia projector filename')
    return repo,file,mmproj


def choose(role,model_id,repo=None,file=None,mmproj=None):
    from llama_runtime import ROLES,load_config,save_config
    if role not in ROLES:raise ValueError('Expected text, decision or vision')
    m=catalog().get(model_id)
    if not m:raise ValueError('Not in the built-in model catalog')
    if role=='text' and model_id=='embeddinggemma-2':
        raise ValueError('EmbeddingGemma 2 produces embeddings, not chat messages; choose decision')
    if (repo or file or mmproj) and not (repo and file):
        raise ValueError('Custom source requires both --repo and --file')
    entry={'id':model_id,'hf_repo':repo or m.get('hf_repo'),'file':file or m.get('file'),
           'mmproj':mmproj if repo else m.get('mmproj'),'verified_catalog':not bool(repo) and m.get('verified',False)}
    validate_source(entry['hf_repo'],entry['file'],entry.get('mmproj'))
    data=load_config();data.setdefault('selections',{})[role]=entry
    # Avoid silently running a previously configured model after a different selection.
    data.setdefault('models',{}).pop(role,None)
    save_config(data)
    return entry


def download(role,runner=subprocess.run,which=shutil.which):
    """Explicit HF CLI download into a dedicated location; no automatic start."""
    from llama_runtime import ROLES,load_config,register
    if role not in ROLES:raise ValueError('Expected a model role')
    selection=load_config().get('selections',{}).get(role)
    if not selection:raise RuntimeError('Select a Hugging Face model first: doomsday-ai choose ROLE MODEL_ID')
    repo,file,projector=validate_source(selection['hf_repo'],selection['file'],selection.get('mmproj'))
    hf=which('hf')
    if not hf:
        raise RuntimeError('Hugging Face CLI missing. Install the official "hf" CLI (huggingface_hub), then rerun download. No model was started.')
    target=STORE/selection['id']
    if STORE.is_symlink() or target.is_symlink():raise ValueError('Refusing symlink model directory')
    target.mkdir(parents=True,exist_ok=True,mode=0o750)
    if shutil.disk_usage(target).free<2*1024**3:
        raise RuntimeError('Less than 2 GiB free at model destination; refusing download')
    names=[file]+([projector] if projector else [])
    print('Explicit Hugging Face download:',repo,', '.join(names),flush=True)
    runner([hf,'download',repo,*names,'--local-dir',str(target)],check=True)
    results=[]
    for name in names:
        path=target/name
        if path.is_symlink() or not path.is_file() or path.stat().st_size<4096:
            raise RuntimeError('Downloaded model is missing, tiny or a symlink: '+name)
        with path.open('rb') as stream:
            if stream.read(4)!=b'GGUF':raise RuntimeError('File is not GGUF: '+name)
        results.append(path)
    model=register(role,results[0],results[1] if len(results)>1 else None)
    return model


def configure(input_fn=input):
    from llama_runtime import ROLES
    ms=list(catalog().values());display()
    choice=input_fn('Select model number (Enter cancels): ').strip()
    if not choice:return None
    if not choice.isdecimal() or not 1<=int(choice)<=len(ms):raise ValueError('Invalid model number')
    item=ms[int(choice)-1]
    role=input_fn(f'Role [text/decision/vision; default {item["role"]}]: ').strip() or item['role']
    if role not in ROLES:raise ValueError('Invalid role')
    repo=file=mmproj=None
    if not item.get('hf_repo') or not item.get('file'):
        print('Model ID not confirmed; enter the exact Hugging Face source yourself.')
        repo=input_fn('Hugging Face owner/repo: ').strip()
        file=input_fn('Exact filename (.gguf): ').strip()
        mmproj=input_fn('Projector filename (.gguf), optional: ').strip() or None
    sel=choose(role,item['id'],repo,file,mmproj)
    print('Selected for '+role+': '+sel['hf_repo']+'/'+sel['file'])
    print('Nothing downloaded, started or scheduled.')
    print(f'Next (optional): sudo doomsday-ai download {role}')
    print(f'Then, when you want it running: sudo doomsday-ai start {role}')
    return sel
