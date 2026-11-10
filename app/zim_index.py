#!/usr/bin/env python3
"""Resumable, locally indexed ZIM articles and media (read-only ZIM inputs).

Uses python-libzim >=4.1 when installed. Never extracts entire ZIM files onto
another drive. Text is indexed in bounded chunks; compressed images are decoded
only when small enough for classic hashes. Audio and video are indexed by names,
MIME, and article context, not magically transcribed.
"""
from __future__ import annotations
import argparse,contextlib,html,io,itertools,json,os,re,sqlite3,sys,time,urllib.parse
from html.parser import HTMLParser
from pathlib import Path

DB = Path(os.environ.get('DOOMSDAY_ZIM_INDEX','/var/lib/doomsday-vault/zim-fts.sqlite'))
MAX_CONTENT = 128 * 1024
MAX_IMAGE = 4 * 1024 * 1024
BATCH = 80
MAX_MATCH_CANDIDATES = 200_000
TITLE_STOPWORDS = frozenset(('a an the of in on at with for from and or if how what where when who why '
    'do does did is are was were be been can could would should tell me about explain '
    'find search look show article articles image images picture pictures similar like to using use '
    'which please information facts give my this that these those by as it its').split())
IMAGE_SUFFIXES = ('.png','.jpg','.jpeg','.webp','.gif','.bmp','.avif')


class CleanHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[];self.media=[];self.media_kinds=[];self.block=0
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag in ('script','style','svg'):self.block+=1
        if tag in ('img','audio','video','source') and not self.block:
            src=attrs.get('src') or attrs.get('data-src')
            if src and not src.startswith(('http:','https:','data:','javascript:','#','//')):
                self.media.append((src,attrs.get('alt','')[:300]))
                self.media_kinds.append('image' if tag=='img' else 'other')
    def handle_endtag(self,tag):
        if tag in ('script','style','svg'):self.block=max(0,self.block-1)
        if tag in ('p','div','li','h1','h2','h3','section'):self.parts.append(' ')
    def handle_data(self,data):
        if not self.block:self.parts.append(data)


def plain_html(raw):
    parser=CleanHTML();parser.feed(raw.decode('utf-8','replace'));parser.close()
    return ' '.join(''.join(parser.parts).split()),parser.media


def html_details(raw):
    parser=CleanHTML();parser.feed(raw.decode('utf-8','replace'));parser.close()
    return ' '.join(''.join(parser.parts).split()),list(zip(parser.media,parser.media_kinds))


def connect(path=DB):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    c=sqlite3.connect(str(path),timeout=30)
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('CREATE TABLE IF NOT EXISTS progress (zim TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, cursor INTEGER DEFAULT 0, complete INTEGER DEFAULT 0, updated INTEGER)')
    c.execute('CREATE VIRTUAL TABLE IF NOT EXISTS articles USING fts5(title,text, zim UNINDEXED,path UNINDEXED, tokenize="unicode61")')
    c.execute('CREATE TABLE IF NOT EXISTS media (zim TEXT, path TEXT, mime TEXT, title TEXT, article TEXT, alt TEXT, hash TEXT, PRIMARY KEY(zim,path))')
    c.execute('CREATE VIRTUAL TABLE IF NOT EXISTS media_notes USING fts5(note, zim UNINDEXED, path UNINDEXED, tool UNINDEXED)')
    # Multiple articles can legitimately share a ZIM image. The old media.article
    # field stored one path only; this scoped link table preserves all associations.
    c.execute("CREATE TABLE IF NOT EXISTS article_media (zim TEXT NOT NULL, article TEXT NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL, alt TEXT DEFAULT '', PRIMARY KEY(zim,article,path))")
    c.execute('CREATE INDEX IF NOT EXISTS article_media_article ON article_media(zim,article)')
    c.execute('CREATE INDEX IF NOT EXISTS article_media_image ON article_media(zim,path)')
    cols={row[1] for row in c.execute('PRAGMA table_info(media)')}
    if 'dhash' not in cols:c.execute('ALTER TABLE media ADD COLUMN dhash TEXT')
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


def _image_hashes(blob):
    """Classical 64-bit average hash and difference hash; no AI inference."""
    if not blob:return None,None
    try:
        from PIL import Image,ImageOps
        with Image.open(io.BytesIO(blob)) as im:
            if im.width*im.height>30_000_000:return None,None
            gray=ImageOps.exif_transpose(im).convert('L')
            pixels=list(gray.resize((8,8)).getdata());avg=sum(pixels)/len(pixels)
            ah=sum((1<<i) for i,v in enumerate(pixels) if v>=avg)
            rows=list(gray.resize((9,8)).getdata())
            dh=sum((1<<(y*8+x)) for y in range(8) for x in range(8)
                   if rows[y*9+x]>rows[y*9+x+1])
        return f'{ah:016x}',f'{dh:016x}'
    except (ImportError,RuntimeError,ValueError,OSError):return None,None


def _image_hash(blob):
    return _image_hashes(blob)[0]


def _media_path(article_path,ref):
    path=urllib.parse.urlsplit(ref).path
    joined=urllib.parse.urljoin('https://zim.invalid/'+article_path,path)
    final=urllib.parse.unquote(urllib.parse.urlsplit(joined).path).lstrip('/')
    return final if final and '\x00' not in final else None


def index_archive(filename,db_path=DB,max_entries=1000, reader_factory=None, rebuild=False):
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
        if row and (rebuild or row[:2]!=(st.st_size,st.st_mtime_ns)):
            db.execute('DELETE FROM articles WHERE zim=?',(archive_id,))
            db.execute('DELETE FROM media WHERE zim=?',(archive_id,))
            db.execute('DELETE FROM article_media WHERE zim=?',(archive_id,))
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
                    if 'html' in mime:clean,links=html_details(blob)
                    else:clean,links=blob.decode('utf-8','replace'),[]
                    for piece in _chunks(clean):
                        db.execute('INSERT INTO articles(title,text,zim,path) VALUES (?,?,?,?)',(title,piece,archive_id,path))
                        articles+=1
                    for (ref,alt),kind in links:
                        media_path=_media_path(path,ref)
                        if media_path:
                            db.execute('INSERT OR REPLACE INTO article_media(zim,article,path,kind,alt) VALUES (?,?,?,?,?)',
                                       (archive_id,path,media_path,kind,alt))
                            db.execute('INSERT INTO media(zim,path,mime,title,article,alt,hash) VALUES (?,?,?,?,?,?,NULL) '
                                       'ON CONFLICT(zim,path) DO UPDATE SET article=excluded.article,alt=excluded.alt',
                                       (archive_id,media_path,'linked',title,path,alt))
                else:
                    h,dh=_image_hashes(blob) if mime.startswith('image/') else (None,None)
                    # Preserve the article association populated by HTML parsing.
                    db.execute('INSERT INTO media(zim,path,mime,title,article,alt,hash,dhash) VALUES (?,?,?,?,?,?,?,?) '
                               'ON CONFLICT(zim,path) DO UPDATE SET mime=excluded.mime,title=excluded.title,hash=excluded.hash,dhash=excluded.dhash',
                               (archive_id,path,mime,title,'','',h,dh))
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


def _title_terms(query):
    terms=[t.casefold() for t in re.findall(r'\w+',str(query)[:300],re.U)]
    distinct=list(dict.fromkeys(x for x in terms if len(x)>1 and x not in TITLE_STOPWORDS))
    return distinct[:10]


def article_title_search(query,db_path=DB,limit=6):
    """Select only FTS article NAMES, then retrieve text from those articles.

    Content matches or media captions CANNOT expand the set of RAG sources.
    Works on existing v6.7 FTS data; no destructive rebuild required.
    """
    if not Path(db_path).is_file():return []
    terms=_title_terms(query)
    if not terms:return []
    match='title : ('+' OR '.join('"'+term.replace('"','')+'"' for term in terms)+')'
    with contextlib.closing(connect(db_path)) as db:
        rows=db.execute('SELECT title,zim,path,bm25(articles,9.0,0.0) FROM articles '
                        'WHERE articles MATCH ? ORDER BY bm25(articles,9.0,0.0) LIMIT 900',(match,)).fetchall()
        scored={}
        for title,zim,path,bm in rows:
            key=(zim,path)
            if key in scored:continue
            normalized=title.casefold()
            matches=sum(bool(re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',normalized)) for term in terms)
            if not matches:continue
            # Favor title coverage and concise, specific article names.
            score=matches/len(terms)+matches/(len(re.findall(r'\w+',normalized))+2)*0.5
            scored[key]=(score,bm,title,zim,path)
        best=sorted(scored.values(),key=lambda t:(-t[0],t[1],len(t[2])))[:max(1,min(int(limit),15))]
        chosen=[]
        for _,_,title,zim,path in best:
            pieces=[row[0] for row in db.execute('SELECT text FROM articles WHERE zim=? AND path=? ORDER BY rowid LIMIT 48',(zim,path))]
            if not pieces:continue
            # Read ONLY the chosen article's chunks and prefer relevant paragraphs.
            ranked=sorted(enumerate(pieces),key=lambda x:-sum(x[1].casefold().count(t) for t in terms))
            excerpt=' '.join(piece for idx,piece in ranked[:2])[:3400]
            chosen.append({'title':title,'snippet':excerpt,'source':'ZIM: '+zim,
                           'link':zim_url(zim,path),'zim':zim,'path':path})
    return chosen


def _raster(path,mime):
    # No SVG or remote content; linked media without an indexed MIME needs
    # a recognized raster extension, not arbitrary video/audio resources.
    return mime.startswith('image/') and mime!='image/svg+xml' or (mime=='linked' and path.lower().endswith(IMAGE_SUFFIXES))


def article_images(articles,db_path=DB,limit=9,per_article=3):
    """Return images referenced by precisely these articles, never global media."""
    if not articles or not Path(db_path).is_file():return []
    results=[];seen=set()
    with contextlib.closing(connect(db_path)) as db:
        for source in articles[:15]:
            zim,path=source.get('zim'),source.get('path')
            if not isinstance(zim,str) or not isinstance(path,str):continue
            # Many-to-many link table supports images shared by different pages.
            rows=db.execute("SELECT am.path,am.alt,coalesce(m.mime,'linked'),am.kind FROM article_media am "
                            'LEFT JOIN media m ON m.zim=am.zim AND m.path=am.path '
                            'WHERE am.zim=? AND am.article=? ORDER BY am.path LIMIT 150',(zim,path)).fetchall()
            # Compatibility with the previously indexed v6.7 single-article link.
            if not rows:
                rows=[(p,alt,mime,'image') for p,alt,mime in db.execute(
                    'SELECT path,alt,mime FROM media WHERE zim=? AND article=? LIMIT 150',(zim,path))]
            taken=0
            for image_path,alt,mime,kind in rows:
                if taken>=per_article or len(results)>=limit:break
                if kind!='image' or not _raster(image_path,mime):continue
                key=(zim,path,image_path)
                if key in seen:continue
                seen.add(key);taken+=1
                results.append({'title':source['title'],'alt':alt or Path(image_path).name,
                                'article_link':zim_url(zim,path),'image_url':zim_url(zim,image_path),
                                'source':'ZIM: '+zim})
            if len(results)>=limit:break
    return results


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


def similar_image(raw,db_path=DB,limit=8,title_query=''):
    """Old-fashioned aHash+dHash comparison against article-linked ZIM images.

    Optional article name scopes the candidate set BEFORE matching. With no
    title, scan at most MAX_MATCH_CANDIDATES rows in small batches, keeping
    only a bounded set of closest hashes. No vision model or embeddings.
    """
    if not Path(db_path).is_file():return []
    ah,dh=_image_hashes(raw)
    if ah is None:return []
    scope=article_title_search(title_query,db_path=db_path,limit=12) if _title_terms(title_query) else None
    if scope is not None and not scope:return []
    value=int(ah,16);difference=int(dh,16) if dh else None
    max_results=max(1,min(int(limit),30));best=[];scanned=0
    with contextlib.closing(connect(db_path)) as db:
        if scope is None:
            # The join covers shared images without inventing article links.
            cursor=db.execute('SELECT m.zim,m.path,m.title,coalesce(am.article,m.article),m.mime,m.hash,m.dhash '
                              'FROM media m LEFT JOIN article_media am ON am.zim=m.zim AND am.path=m.path AND am.kind=? '
                              'WHERE m.hash IS NOT NULL AND (am.article IS NOT NULL OR m.article<>?)',('image',''))
            cursors=[cursor]
        else:
            cursors=[]
            for article in scope:
                zim,path=article['zim'],article['path']
                # Image reference takes precedence over legacy media.article.
                cursor=db.execute('SELECT m.zim,m.path,m.title,am.article,m.mime,m.hash,m.dhash '
                                  'FROM article_media am JOIN media m ON m.zim=am.zim AND m.path=am.path '
                                  'WHERE am.zim=? AND am.article=? AND am.kind=? AND m.hash IS NOT NULL',
                                  (zim,path,'image'))
                records=cursor.fetchall()
                if not records:
                    records=db.execute('SELECT zim,path,title,article,mime,hash,dhash FROM media '
                                       'WHERE zim=? AND article=? AND hash IS NOT NULL',(zim,path)).fetchall()
                cursors.append(iter(records))
        for cursor in cursors:
            while scanned<MAX_MATCH_CANDIDATES:
                batch=cursor.fetchmany(min(1024,MAX_MATCH_CANDIDATES-scanned)) if hasattr(cursor,'fetchmany') else list(itertools.islice(cursor,min(1024,MAX_MATCH_CANDIDATES-scanned)))
                if not batch:break
                scanned+=len(batch)
                for zim,path,title,article,mime,hashv,dhashv in batch:
                    if not _raster(path,mime):continue
                    try:d1=(int(hashv,16)^value).bit_count()
                    except (ValueError,TypeError):continue
                    try:d2=(int(dhashv,16)^difference).bit_count() if dhashv and difference is not None else d1
                    except (ValueError,TypeError):d2=d1
                    distance=round((d1+d2)/2,2)
                    if distance>24:continue
                    best.append({'name':title or Path(path).name,'distance':distance,
                                 'match_percent':round((64-distance)/64*100,1),
                                 'method':'aHash + dHash' if dhashv else 'aHash (legacy index)',
                                 'link':zim_url(zim,article),'media_link':zim_url(zim,path)})
                    if len(best)>max(40,max_results*6):best=sorted(best,key=lambda x:x['distance'])[:max_results]
            if scanned>=MAX_MATCH_CANDIDATES:break
    # Keep distinct image/article pairs and avoid redundant gallery results.
    output=[];seen=set()
    for r in sorted(best,key=lambda x:x['distance']):
        key=(r['link'],r['media_link'])
        if key in seen:continue
        seen.add(key);output.append(r)
        if len(output)>=max_results:break
    return output


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
    p.add_argument('--rebuild',action='store_true',help='Rebuild article/media links in the database without modifying the original ZIM')
    a=p.parse_args()
    if a.action=='add':
        if not a.value:p.error('Provide a path to a verified .zim file')
        print(json.dumps(index_archive(a.value,max_entries=a.limit,rebuild=a.rebuild),indent=2))
    elif a.action=='run':
        if a.rebuild:p.error('--rebuild is supported only with add PATH, to avoid repeatedly resetting timer-driven scans')
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
