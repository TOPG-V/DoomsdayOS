#!/usr/bin/env python3
"""Fail-closed, fast local routing for text and image requests.

Rules are CPU-only and zero dependencies; an optional EmbeddingGemma 2 server
may supply classification similarities. This is a decision layer, not a claim
that any downloaded/model checkpoints are already installed.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Optional

CLASS_LABELS = {
    'search': 'Find and open source articles in my offline encyclopedia and archives',
    'chat': 'Talk with a local language model to answer a general question',
    'rag': 'Research using cited offline articles and summarize their evidence',
    'translate': 'Translate a sentence or paragraph between languages',
    'map': 'Find a city country map coordinates or navigation',
    'image_search': 'Find similar pictures or images in an offline collection',
    'vision': 'Describe or understand an uploaded photo, screenshot, or diagram',
}
IMAGE_LABELS = {
    'image_search':'Find visually similar images and their source articles',
    'vision':'Answer a question about the contents of the supplied image',
    'ocr':'Read or transcribe text visible in the image',
}

@dataclass(frozen=True)
class Decision:
    route: str
    method: str
    confidence: float
    needs_llm: bool
    reason: str
    def dict(self): return asdict(self)


def _score_route(scores: dict, labels: dict, margin=0.08, threshold=0.40):
    """Accept only unambiguous, normalized similarity scores, otherwise fallback."""
    candidates=sorted(((float(scores[k]),k) for k in labels if k in scores), reverse=True)
    if not candidates or candidates[0][0]<threshold:
        return None
    if len(candidates)>1 and candidates[0][0]-candidates[1][0]<margin:
        return None
    return candidates[0][1], min(1.0,max(0.0,candidates[0][0]))


def route_text(text: str, embedding_scores: Optional[dict]=None) -> Decision:
    q=(text or '').strip()[:4000]
    if not q: return Decision('search','rules',1.0,False,'Empty query')
    s=q.casefold()
    simple = [
        ('translate',r'\b(translat(?:e|ion)|przetłumacz|übersetz|traduire|traducir)\b',True),
        ('map',r'\b(map|maps|where is|coordinates|latitude|longitude|gps|navigation|mapa)\b',False),
        ('image_search',r'\b(similar image|similar picture|find (?:an? )?image|image search|reverse image|visual lookup|google lens)\b',False),
        ('search',r'\b(search|find article|open (?:an? )?article|wikipedia|zim|kiwix|look up|wiktionary|encyclopedia)\b',False),
        ('rag',r'\b(sources|citations|cite|based on (?:articles|sources)|research|compare sources|rag)\b',True),
    ]
    for name,regex,needs in simple:
        if re.search(regex,s): return Decision(name,'rules',0.99,needs,'Keyword intent')
    if embedding_scores:
        choice=_score_route(embedding_scores,CLASS_LABELS)
        if choice:
            name,score=choice
            return Decision(name,'embeddings',score,name in ('chat','rag','translate','vision'),'Multimodal embedding classification')
    return Decision('chat','rules',0.5,True,'Default local chat')


def route_image(prompt: str='', embedding_scores: Optional[dict]=None) -> Decision:
    s=(prompt or '').casefold()
    if re.search(r'\b(read|ocr|text in|transcribe|writing|screenshot text|letters)\b',s):
        return Decision('ocr','rules',0.99,True,'Requested image text recognition')
    if re.search(r'\b(similar|reverse|find (?:other )?pictures|find images|source image|lens|match)\b',s):
        return Decision('image_search','rules',0.99,False,'Requested visual similarity')
    if embedding_scores:
        choice=_score_route(embedding_scores,IMAGE_LABELS)
        if choice:
            name,score=choice
            return Decision(name,'embeddings',score,name!='image_search','Image embedding classification')
    return Decision('vision','rules',0.5,True,'Default image question; needs a vision-capable model')


def recognize_image(data: bytes) -> str:
    """Untrusted upload header check; decoding remains in the target model/Pillow."""
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):return 'image/jpeg'
    if data.startswith(b'RIFF') and data[8:12]==b'WEBP':return 'image/webp'
    raise ValueError('Only PNG, JPEG and WEBP images are accepted')


def usable_ram_mib():
    try:
        m=re.search(r'^MemAvailable:\s+(\d+) kB$',Path('/proc/meminfo').read_text(),re.M)
        if m:return int(m.group(1))//1024
    except OSError:pass
    pages=os.sysconf('SC_AVPHYS_PAGES');size=os.sysconf('SC_PAGE_SIZE')
    return int(pages*size//1048576)


def gpu_free_mib():
    try:
        p=subprocess.run(['nvidia-smi','--query-gpu=memory.free','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=3)
        if p.returncode==0:return max(int(x.strip()) for x in p.stdout.splitlines() if x.strip().isdigit())
    except (OSError,subprocess.TimeoutExpired,ValueError):pass
    return 0


def inference_profile(ram_mib=None,vram_mib=None) -> dict:
    """Conservative request-time profile; not a promise that any model fits."""
    ram = usable_ram_mib() if ram_mib is None else max(0,int(ram_mib))
    vram = gpu_free_mib() if vram_mib is None else max(0,int(vram_mib))
    if ram<4096:
        return {'tier':'very_low','ram_free_mib':ram,'vram_free_mib':vram,'context':1024,'kv':'q4_0','batch':128,'gpu_layers':0,'embedding_dims':128,'index_mode':'hash','max_chat_tokens':256}
    if ram<8192 or vram<2048:
        return {'tier':'low','ram_free_mib':ram,'vram_free_mib':vram,'context':2048,'kv':'q4_0','batch':256,'gpu_layers':0,'embedding_dims':128,'index_mode':'hash','max_chat_tokens':512}
    if vram<8192:
        return {'tier':'balanced','ram_free_mib':ram,'vram_free_mib':vram,'context':4096,'kv':'q8_0','batch':512,'gpu_layers':12,'embedding_dims':256,'index_mode':'embeddings','max_chat_tokens':768}
    return {'tier':'high','ram_free_mib':ram,'vram_free_mib':vram,'context':8192,'kv':'q8_0','batch':1024,'gpu_layers':99,'embedding_dims':512,'index_mode':'embeddings','max_chat_tokens':1024}


def llama_args(gguf_path: str, profile: dict, port:int=8081, executable='llama-server') -> list[str]:
    """Suggested launch args for an installed llama.cpp build; do not auto-execute."""
    if not Path(gguf_path).is_file() or not gguf_path.endswith('.gguf'):
        raise ValueError('The GGUF file must exist locally')
    if not 1024<=port<=65535:raise ValueError('Invalid llama.cpp server port')
    return [executable,'-m',gguf_path,'--host','127.0.0.1','--port',str(port),'-c',str(profile['context']),
            '-ngl',str(profile['gpu_layers']),'--batch-size',str(profile['batch']),
            '--flash-attn','on','--cache-type-k',profile['kv'],'--cache-type-v',profile['kv']]


def embed_cosine(a,b):
    import math
    dot=sum(x*y for x,y in zip(a,b))
    den=math.sqrt(sum(x*x for x in a))*math.sqrt(sum(x*x for x in b))
    return dot/den if den else 0


def embedding_route_scores(text: str, labels=CLASS_LABELS, endpoint='http://127.0.0.1:8082', timeout=4):
    """Optional installed local llama.cpp embedding server (no cloud calls)."""
    from urllib.parse import urlsplit
    p=urlsplit(endpoint)
    if p.hostname not in ('localhost','127.0.0.1','::1') or p.scheme!='http' or p.path.strip('/'):
        raise ValueError('Decision server must be on loopback')
    payload=json.dumps({'input':[text,*labels.values()],'model':'local'}).encode()
    req=urllib.request.Request(endpoint.rstrip('/')+'/v1/embeddings',data=payload,headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=timeout) as r:
        out=json.loads(r.read(512*1024))
    embs=[r['embedding'] for r in sorted(out['data'],key=lambda x:x['index'])]
    if len(embs)!=len(labels)+1:raise ValueError('Embedding server returned incomplete data')
    return {key:embed_cosine(embs[0],emb) for key,emb in zip(labels,embs[1:])}


def image_embedding_scores(data_uri: str, labels=IMAGE_LABELS, endpoint='http://127.0.0.1:8082', timeout=4):
    """Optional REAL image-to-text zero-shot routing with a multimodal embedder.

    Only use when that engine supports image embeddings. It is never sent to a
    public endpoint and errors safely fall back to text/rule routing.
    """
    from urllib.parse import urlsplit
    p=urlsplit(endpoint)
    if p.scheme!='http' or p.hostname not in ('localhost','127.0.0.1','::1') or p.path.strip('/'):
        raise ValueError('Decision engine must be bound to loopback')
    if not data_uri.startswith(('data:image/png;base64,','data:image/jpeg;base64,','data:image/webp;base64,')) or len(data_uri)>16*1024*1024:
        raise ValueError('Unsupported or oversize image for embedding')
    inputs=[{'content':[{'type':'image_url','image_url':{'url':data_uri}}]},*labels.values()]
    req=urllib.request.Request(endpoint+'/v1/embeddings',data=json.dumps({'model':'local','input':inputs}).encode(),headers={'Content-Type':'application/json'},method='POST')
    with urllib.request.urlopen(req,timeout=timeout) as r:
        out=json.loads(r.read(512*1024))
    embs=[item['embedding'] for item in sorted(out['data'],key=lambda x:x['index'])]
    if len(embs)!=len(labels)+1:raise ValueError('Incomplete multimodal embeddings')
    return {key:embed_cosine(embs[0],emb) for key,emb in zip(labels,embs[1:])}
