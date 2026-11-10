#!/usr/bin/env python3
"""Resumable, locally indexed ZIM articles and media (read-only ZIM inputs).

Uses python-libzim >=4.1 when installed. Never extracts entire ZIM files onto
another drive. Text is indexed in bounded chunks; compressed images are decoded
only when small enough for classic hashes. Audio and video are indexed by names,
MIME, and article context, not magically transcribed.
"""
from __future__ import annotations
import argparse,html,io,json,os,re,sqlite3,sys,time,urllib.parse
from html.parser import HTMLParser
from pathlib import Path

DB = Path(os.environ.get('DOOMSDAY_ZIM_INDEX','/var/lib/doomsday-vault/zim-fts.sqlite'))
MAX_CONTENT = 128 * 1024
MAX_IMAGE = 4 * 1024 * 1024
BATCH = 80


class CleanHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[];self.media=[];self.block=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style','svg'):self.block+=1
        if tag in ('img','audio','video','source'):
            src=attrs.get('src') or attrs.get('data-src')
            if src and not src.startswith(('http:','https:','data:','javascript:','#','//')):
                self.media.append((src,attrs.get('alt','')[:300]))
    def handle_endtag(self,tag):
        if tag in ('script','style','svg'):self.block=max(0,self.block-1)
        if tag in ('p','div','li','h1','h2','h3','section'):self.parts.append(' ')
    def handle_data(self,data):
        if not self.block:self.parts.append(data)


def plain_html(raw):
    parser=CleanHTML();parser.feed(raw.decode('utf-8','replace'));parser.close()
    return ' '.join(''.join(parser.parts).split()),parser.media


def connect(path=DB):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(path),timeout=30)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS progress (zim TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, cursor INTEGER DEFAULT 0, complete INTEGER DEFAULT 0, updated INTEGER)')
    c.execute('CREATE VIRTUAL TABLE IF NOT EXISTS articles USING fts5(title,text, zim UNINDEXED,path UNINDEXED, tokenize="unicode61")')
    c.execute('CREATE TABLE IF NOT EXISTS media (zim TEXT, path TEXT, mime TEXT, title TEXT, article TEXT, alt TEXT, hash TEXT, PRIMARY KEY(zim,path))')
    c.execute('CREATE VIRTUAL TABLE IF NOT EXISTS media_notes USING fts5(note, zim UNINDEXED, path UNINDEXED, tool UNINDEXED)')
    c.execute('CREATE INDEX IF NOT EXISTS media_zim ON media(zim)')
    return c


def zim_url(zim,path):
    base=Path(zim).stem
    # URL paths use the archive's filename and an encoded article resource path.
    return 'http://localhost:8080/'+urllib.parse.quote(base,safe='')+'/'+urllib.parse.quote(path.lstrip('/'),safe='/')


def safe_tokens(q):
    terms=re.findall(r'\w+',str(q)[:300],re.U)[:12]
    return ' OR '.join('"'+t.replace('"','')+'"' for t in terms)


def _entry_data(entry):
    if entry.is_redirect:return None
    item=entry.get_item()
    mime=str(item.mimetype).split(';',1)[0].lower()
    if not (mime.startswith(('text/','image/','audio/','video/')) or mime=='application/xhtml+xml'):
        return None
    size=int(item.size)
    if size>MAX_CONTENT and (mime.startswith('text/') or mime=='application/xhtml+xml'):
        # Avoid decompression of pathological pages in a low-RAM vault.
        return None
    if mime.startswith('image/') and size>MAX_IMAGE:return (mime,None)
    if mime.startswith(('audio/','video/')):return (mime,None)
    return (mime,bytes(item.content))


def _chunks(text,maxlen=2800):
    for i in range(0,min(len(text),MAX_CONTENT),maxlen):
        yield text[i:i+maxlen]


def _image_hash(blob):
    if not blob:return None
    try:
        from media_index import fingerprint
        return f'{fingerprint(blob):016x}'
    except (ImportError,RuntimeError,ValueError,OSError):return None


def _media_path(article_path,ref):
    path=urllib.parse.urlsplit(ref).path
    joined=urllib.parse.urljoin('https://zim.invalid/'+article_path,path)
    final=urllib.parse.unquote(urllib.parse.urlsplit(joined).path).lstrip('/')
    return final if final and '\x00' not in final else None


def index_archive(filename,db_path=DB,max_entries=1000, reader_factory=None):
    if Path(filename).is_symlink():raise ValueError('Refusing symlinked ZIM input')
    zim=Path(filename).resolve(strict=True)
    if not zim.is_file() or zim.suffix.lower()!='.zim':
        raise ValueError('Select a mounted, existing .zim archive')
    if not 1<=max_entries<=50000:raise ValueError('max_entries must be 1–50000')
    if reader_factory is None:
        try:
            from libzim.reader import Archive
        except ImportError as e:raise RuntimeError('Install python-libzim >=4.1 into the indexer Python environment first') from e
        reader_factory=Archive
    st=zim.stat();archive_id=zim.name;processed=0;media=0;articles=0
    with connect(db_path) as db:
        row=db.execute('SELECT size,mtime_ns,cursor,complete FROM progress WHERE zim=?',(archive_id,)).fetchone()
        if row and row[:2]!=(st.st_size,st.st_mtime_ns):
            db.execute('DELETE FROM articles WHERE zim=?',(archive_id,))
            db.execute('DELETE FROM media WHERE zim=?',(archive_id,))
            db.execute('DELETE FROM media_notes WHERE zim=?',(archive_id,))
            row=None
        if row and row[3]:return {'zim':archive_id,'already_indexed':True,'complete':True,'scanned':0}
        cursor=row[2] if row else 0
        db.execute('INSERT OR REPLACE INTO progress(zim,size,mtime_ns,cursor,complete,updated) VALUES (?,?,?,?,0,?)',(archive_id,st.st_size,st.st_mtime_ns,cursor,int(time.time())))
        db.commit()
        with reader_factory(str(zim)) as archive:
            if hasattr(archive,'iter_by_path'):
                iterator=archive.iter_by_path()
            elif hasattr(archive,'get_entry_by_id') and hasattr(archive,'all_entry_count'):
                # Ubuntu's python3-libzim often ships pre-4.1 bindings.
                iterator=(archive.get_entry_by_id(i) for i in range(archive.all_entry_count))
            else:
                raise RuntimeError('python-libzim API does not provide iterable archive entries')
            done=True
            for idx,entry in enumerate(iterator):
                if idx<cursor:continue
                if processed>=max_entries:
                    done=False;break
                processed+=1
                title=str(entry.title)[:512];path=str(entry.path)
                if not path or len(path)>2000:continue
                try:detail=_entry_data(entry)
                except (OSError,ValueError,KeyError,RuntimeError):continue
                if detail is None:continue
                mime,blob=detail
                if mime.startswith('text/') or mime=='application/xhtml+xml':
                    if blob is None:continue
                    if 'html' in mime:clean,links=plain_html(blob)
                    else:clean,links=blob.decode('utf-8','replace'),[]
                    for piece in _chunks(clean):
                        db.execute('INSERT INTO articles(title,text,zim,path) VALUES (?,?,?,?)',(title,piece,archive_id,path))
                        articles+=1
                    for ref,alt in links:
                        media_path=_media_path(path,ref)
                        if media_path:
                            db.execute('INSERT INTO media(zim,path,mime,title,article,alt,hash) VALUES (?,?,?,?,?,?,NULL) '
                                       'ON CONFLICT(zim,path) DO UPDATE SET article=excluded.article,alt=excluded.alt',
                                       (archive_id,media_path,'linked',title,path,alt))
                else:
                    h=_image_hash(blob) if mime.startswith('image/') else None
                    # Preserve a matching article reference populated by HTML parsing.
                    db.execute('INSERT INTO media(zim,path,mime,title,article,alt,hash) VALUES (?,?,?,?,?,?,?) '
                               'ON CONFLICT(zim,path) DO UPDATE SET mime=excluded.mime, title=excluded.title, hash=excluded.hash',
                               (archive_id,path,mime,title,'','',h))
                    media+=1
                if processed%BATCH==0:
                    db.execute('UPDATE progress SET cursor=?,updated=? WHERE zim=?',(idx+1,int(time.time()),archive_id))
                    db.commit()
            next_idx=cursor+processed
            db.execute('UPDATE progress SET cursor=?,complete=?,updated=? WHERE zim=?',(next_idx,int(done),int(time.time()),archive_id))
            db.commit()
    return {'zim':archive_id,'scanned':processed,'articles':articles,'media':media,'cursor':next_idx,'complete':done}


def search(q, db_path=DB, limit=7):
    if not Path(db_path).is_file() or not safe_tokens(q):return []
    c=connect(db_path)
    try:
        rows=c.execute('SELECT title,snippet(articles,1,"[","]","…",28),zim,path FROM articles WHERE articles MATCH ? ORDER BY rank LIMIT ?',
                       (safe_tokens(q),max(1,min(limit,40)))).fetchall()
        extra=c.execute('SELECT snippet(media_notes,0,"[","]","…",28),zim,path FROM media_notes WHERE media_notes MATCH ? ORDER BY rank LIMIT ?',
                       (safe_tokens(q),max(1,min(limit,40)))).fetchall()
    finally:c.close()
    output=[{'title':title,'snippet':snippet,'source':'ZIM: '+zim,'link':zim_url(zim,path)} for title,snippet,zim,path in rows]
    output.extend({'title':'Indexed media: '+path.rsplit('/',1)[-1],'snippet':text,'source':'ZIM media: '+zim,'link':zim_url(zim,path)} for text,zim,path in extra)
    return output[:limit]


def media_search(q,kind='all',db_path=DB,limit=20):
    if not Path(db_path).is_file():return []
    if kind not in ('all','image','audio','video'):raise ValueError('Invalid media type')
    words=re.findall(r'\w+',q[:100],re.U)[:8]
    if not words:return []
    # Escape LIKE wildcards; use parameters exclusively.
    terms=['%'+w.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%' for w in words]
    filters=['(title LIKE ? ESCAPE "\\" OR alt LIKE ? ESCAPE "\\" OR path LIKE ? ESCAPE "\\")' for _ in terms]
    args=[v for x in terms for v in (x,x,x)]
    clause=' AND '.join(filters)
    if kind!='all':clause+=' AND mime LIKE ?';args.append(kind+'/%')
    c=connect(db_path)
    try:rows=c.execute('SELECT zim,path,mime,title,article,alt FROM media WHERE '+clause+' LIMIT ?',(*args,min(100,limit))).fetchall()
    finally:c.close()
    return [{'name':title or Path(path).name,'type':mime,'snippet':alt[:180],
             'link':zim_url(zim,article or path),'media_link':zim_url(zim,path), 'zim':zim} for zim,path,mime,title,article,alt in rows]


def similar_image(raw,db_path=DB,limit=8):
    if not Path(db_path).is_file():return []
    h=_image_hash(raw)
    if h is None:return []
    value=int(h,16);c=connect(db_path)
    try:rows=c.execute('SELECT zim,path,title,article,hash FROM media WHERE hash IS NOT NULL').fetchall()
    finally:c.close()
    scored=[]
    for zim,path,title,article,hashv in rows:
        try:distance=(int(hashv,16)^value).bit_count()
        except ValueError:continue
        scored.append({'name':title or Path(path).name,'distance':distance,'match_percent':round(100*(64-distance)/64,1),
                       'link':zim_url(zim,article or path),'media_link':zim_url(zim,path)})
    return sorted(scored,key=lambda d:d['distance'])[:limit]


def status(db_path=DB):
    if not Path(db_path).is_file():return {'available':False,'message':'No ZIM index created yet'}
    c=connect(db_path)
    try:
        rows=c.execute('SELECT zim,cursor,complete,updated FROM progress ORDER BY zim').fetchall()
        count=c.execute('SELECT count(*) FROM articles').fetchone()[0]
        enriched=c.execute('SELECT count(*) FROM media_notes').fetchone()[0]
        media=c.execute('SELECT count(*) FROM media').fetchone()[0]
    finally:c.close()
    return {'available':True,'articles':count,'media':media,'enriched_media':enriched,'archives':[{'name':r[0],'cursor':r[1],'complete':bool(r[2]),'updated':r[3]} for r in rows]}


def main():
    p=argparse.ArgumentParser(description='Index read-only ZIM articles and media for offline RAG')
    p.add_argument('action',choices=['add','run','search','media','status'])
    p.add_argument('value',nargs='?')
    p.add_argument('--limit',type=int,default=1000)
    a=p.parse_args()
    if a.action=='add':
        if not a.value:p.error('Provide a path to a verified .zim file')
        print(json.dumps(index_archive(a.value,max_entries=a.limit),indent=2))
    elif a.action=='run':
        # Only index archives present in the immutable verified manifest.
        from archive_guard import current_config
        cfg=current_config();roots=[Path(cfg.get('zim_dir','/nonexistent'))]
        if cfg.get('zim_dir_2'):roots.append(Path(cfg['zim_dir_2']))
        try:manifest=json.loads((Path(os.environ.get('DOOMSDAY_STATE_DIR','/var/lib/doomsday-vault'))/'zim-manifest.json').read_text())
        except (OSError,ValueError):manifest={}
        result=[]
        for name in sorted(manifest):
            candidates=[root/name for root in roots if (root/name).is_file()]
            if len(candidates)>1:
                result.append({'zim':name,'error':'Duplicate ZIM on two archive locations; inspect before indexing'})
                continue
            if candidates:
                try:result.append(index_archive(candidates[0],max_entries=a.limit))
                except (OSError,RuntimeError,ValueError) as e:result.append({'zim':name,'error':str(e)})
        print(json.dumps(result,indent=2))
    elif a.action=='search':print(json.dumps(search(a.value or ''),indent=2))
    elif a.action=='media':print(json.dumps(media_search(a.value or ''),indent=2))
    else:print(json.dumps(status(),indent=2))
if __name__=='__main__':main()
