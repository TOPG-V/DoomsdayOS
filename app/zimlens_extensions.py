"""Local ZimLens projects, themes, MCP metadata, skills and attachments.

MCP registrations and generated skills are inert metadata by default. Neither
addition nor static validation executes untrusted programs. An external MCP
runtime or sandbox must be separately installed and explicitly approved.
"""
from __future__ import annotations
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get('ZIMLENS_DATA_DIR',str(Path.home()/'.local/share/zimlens')))
LOCK = threading.RLock()
NAME = re.compile(r'^[A-Za-z][A-Za-z0-9 _.-]{0,63}$')
HEX = re.compile(r'^#[0-9a-fA-F]{6}$')
KINDS = {'theme','mcp','skill','project'}
COLORS = ('bg','side','panel','panel2','line','text','muted','accent')
SAFE_DEFAULT = {'bg':'#191b1a','side':'#202321','panel':'#292c29','panel2':'#242724','line':'#414640','text':'#f0eee8','muted':'#b4b9b1','accent':'#c3c8a4'}
ATTACH_LIMIT = 24*1024*1024
ATTACH_ALLOWED = {'.txt','.md','.csv','.json','.yaml','.yml','.pdf','.png','.jpg','.jpeg','.webp','.gif','.mp4','.mkv','.webm','.mp3','.wav','.ogg','.flac','.zip','.html','.py','.js','.css','.xml','.log','.srt','.vtt','.svg','.docx','.odt','.epub','.zim'}

def _dir(kind):
    path = ROOT / {'theme':'themes','mcp':'mcp','skill':'skills','project':'projects'}[kind]
    path.mkdir(parents=True,exist_ok=True,mode=0o700)
    return path

def _id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{32}',value):
        raise ValueError('Invalid item ID')
    return value

def _read(path):
    try:
        data=json.loads(path.read_text('utf-8'))
        return data if isinstance(data,dict) else None
    except (ValueError, OSError,UnicodeError):return None

def _save(path,record):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.tmp-')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as f:
            json.dump(record,f,ensure_ascii=False,indent=2)
            f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def listing(kind):
    if kind not in KINDS:raise ValueError('Unsupported item category')
    with LOCK:
        items=[]
        for path in sorted(_dir(kind).glob('*.json'))[:250]:
            data=_read(path)
            if data and data.get('id')==path.stem:
                # Never expose hidden content or local filesystem secrets in listings.
                items.append({k:data[k] for k in ('id','name','description','type','enabled','created_at','approved','status','colors') if k in data})
        if kind=='theme':items.insert(0,{'id':'built-in-dark','name':'Original dark (built in)','type':'built-in','colors':SAFE_DEFAULT,'enabled':True})
        return {'items':items}

def _validate_name(data):
    name=data.get('name')
    if not isinstance(name,str) or not NAME.fullmatch(name):raise ValueError('Name must be 1–64 letters, numbers, spaces, dots, hyphens or underscores')
    return name.strip()

def _clean_description(data):
    desc=data.get('description','')
    if not isinstance(desc,str) or len(desc)>2000:raise ValueError('Description too long')
    return desc

def _validate_theme(data):
    colors=data.get('colors')
    if not isinstance(colors,dict) or any(k not in COLORS or not isinstance(v,str) or not HEX.fullmatch(v) for k,v in colors.items()):
        raise ValueError('Theme colors must be #RRGGBB')
    result={**SAFE_DEFAULT,**colors}
    # Ensure interface text remains readable; contrast testing is deterministic, no model required.
    def luminance(c):
        rgb=[int(c[j:j+2],16)/255 for j in (1,3,5)]
        rgb=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in rgb]
        return rgb[0]*.2126+rgb[1]*.7152+rgb[2]*.0722
    a,b=sorted((luminance(result['text']),luminance(result['bg'])),reverse=True)
    if (a+.05)/(b+.05)<4.5:raise ValueError('Theme text/background contrast must be at least 4.5:1')
    return {'colors':result,'type':'custom','enabled':False}

def _validate_mcp(data):
    # Metadata only; adding a server does not launch a process or connect remotely.
    mode=data.get('transport','stdio')
    if mode not in ('stdio','http'):raise ValueError('Transport must be stdio or http')
    out={'type':'mcp','transport':mode,'enabled':False,'approved':False,'status':'Not connected'}
    if mode=='stdio':
        command=data.get('command','')
        args=data.get('args',[])
        if not isinstance(command,str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,150}',command) or '..' in Path(command).parts:
            raise ValueError('A single executable path/name is required; shell syntax is not accepted')
        if not isinstance(args,list) or len(args)>20 or any(not isinstance(a,str) or len(a)>256 or '\x00' in a for a in args):
            raise ValueError('Invalid argument list')
        out.update(command=command,args=args)
    else:
        from urllib.parse import urlparse
        url=data.get('url','')
        p=urlparse(url)
        if p.scheme!='https' or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError('Remote server URL must use HTTPS, without credentials')
        out['url']=url
    return out

def _validate_skill(data):
    content=data.get('instructions','')
    if not isinstance(content,str) or not 4<=len(content)<=12000:
        raise ValueError('Skill must have 4–12,000 characters of instructions')
    return {'type':'skill','instructions':content,'enabled':False,'approved':False,'status':'Draft — static validation passed'}

def _validate_project(data):
    return {'type':'project','description':_clean_description(data),'status':'Local workspace'}

def add(kind,data,existing_id=None):
    if kind not in KINDS:raise ValueError('Unsupported item category')
    if not isinstance(data,dict):raise ValueError('Expected an object')
    name=_validate_name(data)
    desc=_clean_description(data)
    fields={'theme':_validate_theme,'mcp':_validate_mcp,'skill':_validate_skill,'project':_validate_project}[kind](data)
    with LOCK:
        if existing_id:
            ident=_id(existing_id)
            previous=_read(_dir(kind)/(ident+'.json'))
            if not previous:raise ValueError('Item not found')
            # Editing an MCP server or skill always removes earlier approval.
        else:ident=uuid.uuid4().hex
        now=int(time.time())
        record={'id':ident,'name':name,'description':desc,'created_at':now,**fields}
        _save(_dir(kind)/(ident+'.json'),record)
        return {k:record[k] for k in ('id','name','description','type','created_at','status','colors','enabled','approved') if k in record}

def get(kind,ident):
    if kind not in KINDS:raise ValueError('Unsupported category')
    with LOCK:
        val=_read(_dir(kind)/(_id(ident)+'.json'))
        if not val:raise ValueError('Item not found')
        return val

def remove(kind,ident):
    if kind not in KINDS:raise ValueError('Unsupported category')
    with LOCK:
        path=_dir(kind)/(_id(ident)+'.json')
        if not path.is_file():raise ValueError('Item not found')
        path.unlink()
    return {'deleted':True}

def approve(kind,ident,enabled):
    if kind not in ('skill','mcp'):raise ValueError('Only MCP registrations and skills require approval')
    if type(enabled) is not bool:raise ValueError('Expected explicit approval choice')
    with LOCK:
        item=get(kind,ident)
        item['approved']=enabled
        item['enabled']=enabled if kind=='skill' else False
        item['status']='Approved for prompt use' if (enabled and kind=='skill') else ('Registration approved — connection requires an external MCP broker' if enabled else 'Disabled')
        _save(_dir(kind)/(_id(ident)+'.json'),item)
        return {'id':ident,'approved':enabled,'enabled':item['enabled'],'status':item['status']}


def approved_skill_instructions(ids):
    if ids is None:return ''
    if not isinstance(ids,list) or len(ids)>4:raise ValueError('Select at most four skills')
    output=[]
    for ident in ids:
        item=get('skill',ident)
        if not item.get('approved') or not item.get('enabled'):raise ValueError('Skill must be explicitly approved before use')
        output.append(item['name']+': '+item['instructions'][:3000])
    return '\n'.join(output)[:6500]


def static_test(kind,data):
    # No dynamic code execution, imports, shell, MCP connections or tests that run user code.
    if kind not in ('theme','mcp','skill'):raise ValueError('Only themes, MCP definitions and skills support static tests')
    _validate_name(data)
    result={'theme':_validate_theme,'mcp':_validate_mcp,'skill':_validate_skill}[kind](data)
    return {'passed':True,'checks':['schema','limits','safe data validation'], 'execution':'No external programs were executed','detail':result['type']}

def save_attachment(data,filename,project_id=None):
    if not isinstance(data,(bytes,bytearray)) or not 0<len(data)<=ATTACH_LIMIT:raise ValueError('Attachment must be 1–24 MiB')
    base=Path(filename).name
    if not 1<=len(base)<=180 or base in ('.','..'):
        raise ValueError('Unsupported attachment filename or extension')
    if project_id is not None:get('project',project_id)
    attachment_dir=ROOT/'attachments'
    attachment_dir.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Strict aggregate storage budget; arbitrary files remain inert data.
    existing=[p for p in attachment_dir.iterdir() if p.is_file() and not p.name.endswith('.json')]
    if len(existing)>=200 or sum(p.stat().st_size for p in existing)>256*1024*1024-len(data):
        raise ValueError('Attachment storage quota reached (256 MiB or 200 files)')
    ident=uuid.uuid4().hex
    target=ROOT/'attachments'/ident
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    # Untrusted bytes are stored but never executed or opened by the server.
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as f:f.write(data)
    metadata={'id':ident,'name':base,'size':len(data),'project_id':project_id,'created_at':int(time.time()),'analysis':'Attachment only; text and image formats can be processed by supported local tools; video requires an installed media analyzer'}
    _save(ROOT/'attachments'/(ident+'.json'),metadata)
    return metadata

def sandbox_status():
    exe=shutil.which('bwrap')
    return {'available':bool(exe),'backend':'bubblewrap' if exe else None,
            'execution_enabled':False,'policy':'No execution by default. Install an isolated sandbox broker and approve each action before use.',
            'host_access':False}


def sandbox_self_test():
    """Explicit allowlisted test only; no shell, paths, arguments or host writes."""
    exe=shutil.which('bwrap')
    if not exe:raise RuntimeError('Install bubblewrap to enable a sandbox self-test')
    paths=['/usr','/bin','/lib','/lib64']
    cmd=[exe,'--die-with-parent','--new-session','--unshare-all','--proc','/proc','--dev','/dev',
         '--tmpfs','/tmp','--dir','/home','--chdir','/tmp']
    for path in paths:
        if Path(path).exists():cmd+=['--ro-bind',path,path]
    cmd+=['--','/bin/sh','-c','printf "sandbox-ready\n"']
    try:
        p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=4,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:raise RuntimeError('Sandbox test unavailable: '+str(exc)[:120])
    if p.returncode or p.stdout.strip()!='sandbox-ready':raise RuntimeError('Sandbox launch failed: '+p.stderr[:180])
    return {'ok':True,'isolation':'unshared namespaces, network disabled, read-only system binaries','host_write':False,
            'note':'Self-test passed. Arbitrary shell execution is not enabled.'}