#!/usr/bin/env python3
"""Optional source-grounded semantic retrieval with EmbeddingGemma 2.

Uses an already-running LOCAL OpenAI-compatible embeddings endpoint. No model
weights downloaded, no network lookups, bounded batches. Lexical ZIM title search
remains the safe fallback when embeddings are unavailable.
"""
from __future__ import annotations
import argparse,array,contextlib,json,math,os,sqlite3,struct,urllib.request,urllib.error
from pathlib import Path

DB=Path(os.environ.get('DOOMSDAY_SEMANTIC_DB','/var/lib/doomsday-vault/research/semantic-rag.sqlite'))
ENDPOINT='http://127.0.0.1:8082/v1/embeddings'
MAX_DIMENSIONS=768
DEFAULT_DIMENSIONS=256

def connect(db_path=DB):
    path=Path(db_path);path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(path),timeout=15)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS documents (zim TEXT, path TEXT, title TEXT, excerpt TEXT, vector BLOB, model TEXT, dimension INTEGER, PRIMARY KEY(zim,path))')
    c.execute('CREATE TABLE IF NOT EXISTS progress (id INTEGER PRIMARY KEY CHECK(id=1), cursor INTEGER DEFAULT 0)')
    return c

def normalize(values,dimensions=DEFAULT_DIMENSIONS):
    if not isinstance(values,list) or not 1<=len(values)<=MAX_DIMENSIONS:raise ValueError('Invalid embedding dimensions')
    if not 32<=int(dimensions)<=MAX_DIMENSIONS:raise ValueError('Dimensions out of bounds')
    vals=[float(x) for x in values[:int(dimensions)]]
    if not all(math.isfinite(v) for v in vals):raise ValueError('Embedding contains NaN/Inf')
    length=math.sqrt(sum(x*x for x in vals))
    if length<=1e-12:raise ValueError('Empty embedding')
    return [x/length for x in vals]

def embed(text,endpoint=ENDPOINT,timeout=20,dimensions=DEFAULT_DIMENSIONS,opener=None):
    if endpoint!=ENDPOINT:raise ValueError('Embedding endpoint must remain localhost')
    if not isinstance(text,str) or not text.strip():raise ValueError('Empty text')
    payload=json.dumps({'model':'local','input':text[:4000]}).encode()
    req=urllib.request.Request(endpoint,data=payload,headers={'Content-Type':'application/json'},method='POST')
    response=(opener or urllib.request.urlopen)(req,timeout=timeout)
    with response as handle:
        data=json.loads(handle.read(1024*1024))
    if not isinstance(data,dict) or not data.get('data'):raise ValueError('Embedding server returned no vectors')
    return normalize(data['data'][0]['embedding'],dimensions)

def _blob(vec):return struct.pack('<'+str(len(vec))+'f',*vec)
def _vector(blob):
    if not blob or len(blob)%4 or len(blob)>MAX_DIMENSIONS*4:raise ValueError('Invalid stored embedding')
    return struct.unpack('<'+str(len(blob)//4)+'f',blob)

def _score(x,y):
    if len(x)!=len(y):return -1.0
    return sum(a*b for a,b in zip(x,y))

def index_new(db_path=DB,zim_db=None,max_docs=150,embedder=None,dimensions=DEFAULT_DIMENSIONS):
    import zim_index
    if not (zim_db or zim_index.DB).is_file():return {'indexed':0,'reason':'No ZIM title index'}
    dimensions=max(32,min(MAX_DIMENSIONS,int(dimensions)))
    if max_docs<1 or max_docs>2000:raise ValueError('Index at most 2000 entries at a time')
    dst=connect(db_path)
    try:
        progress=dst.execute('SELECT cursor FROM progress WHERE id=1').fetchone()
        cursor=progress[0] if progress else 0
        with contextlib.closing(zim_index.connect(zim_db or zim_index.DB)) as src:
            rows=src.execute('''SELECT a.rowid, a.zim,a.path,a.title,a.text FROM articles a
                 JOIN (SELECT MIN(rowid) AS rid FROM articles WHERE rowid>? GROUP BY zim,path) x ON a.rowid=x.rid
                 ORDER BY a.rowid LIMIT ?''',(cursor,max_docs)).fetchall()
        count=0
        for rowid,zim,path,title,text in rows:
            # Do not ever regenerate embeddings for identical article IDs during normal resume.
            exists=dst.execute('SELECT 1 FROM documents WHERE zim=? AND path=?',(zim,path)).fetchone()
            if not exists:
                vec=(embedder or embed)(title+'\n'+text[:2300],dimensions=dimensions)
                dst.execute('INSERT INTO documents VALUES (?,?,?,?,?,?,?)',(zim,path,title,text[:1700],_blob(vec),'embeddinggemma2-local',len(vec)))
                count+=1
            dst.execute('INSERT OR REPLACE INTO progress(id,cursor) VALUES (1,?)',(rowid,))
            dst.commit()
        return {'indexed':count,'cursor':rows[-1][0] if rows else cursor,'source':'verified ZIM article index',
                'note':'Only when local compatible embedding endpoint is running. No external API.'}
    finally:dst.close()

def search(query,db_path=DB,limit=7,embedder=None,dimensions=DEFAULT_DIMENSIONS):
    import zim_index
    lexical=zim_index.article_title_search(query,limit=limit)
    if not Path(db_path).is_file():return {'mode':'title-fallback','results':lexical}
    try:vec=(embedder or embed)(query,dimensions=dimensions)
    except (ValueError,KeyError,OSError,TimeoutError,urllib.error.URLError):
        return {'mode':'title-fallback','results':lexical}
    with contextlib.closing(connect(db_path)) as db:
        rows=db.execute('SELECT zim,path,title,excerpt,vector FROM documents WHERE dimension=? LIMIT 75000',(len(vec),)).fetchall()
    scores=[]
    for zim,path,title,excerpt,blob in rows:
        try:
            cosine=_score(vec,_vector(blob))
            if cosine>=0.05:scores.append((cosine,{'title':title,'snippet':excerpt,'zim':zim,'path':path,'link':zim_index.zim_url(zim,path),'score':round(cosine,3)}))
        except (ValueError,struct.error):continue
    scores.sort(key=lambda x:-x[0])
    return {'mode':'embeddinggemma2-local','results':[item for _,item in scores[:max(1,min(int(limit),20))]],'indexed_candidates':len(rows),
            'note':'Semantic ranks only indexed verified-archive articles. Confirm quotes and source links.'}

def status(db_path=DB):
    if not Path(db_path).is_file():return {'indexed':0,'available':False,'hint':'Run doomsday-semantic index after starting a compatible local embedding server'}
    with contextlib.closing(connect(db_path)) as db:
        count=db.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
    return {'indexed':count,'available':True,'dimension':DEFAULT_DIMENSIONS,'endpoint':'localhost only'}

def main(argv=None):
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action',required=True)
    sub.add_parser('status');sub.add_parser('index');q=sub.add_parser('search');q.add_argument('query');q.add_argument('--limit',type=int,default=8)
    a=p.parse_args(argv)
    if a.action=='status':print(json.dumps(status(),indent=2))
    elif a.action=='index':print(json.dumps(index_new(),indent=2))
    else:print(json.dumps(search(a.query,limit=a.limit),indent=2))
if __name__=='__main__':main()
