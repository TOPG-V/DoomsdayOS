#!/usr/bin/env python3
"""Private local research library: cited PDFs, imported HTTPS pages, indexed ZIMs.

No cloud search service, scraping of arbitrary LAN endpoints, or arbitrary
local filesystem reads. Web ingestion is a single explicit HTTPS request with
pinned public DNS and no redirects. PDF pages and source links remain auditable.
"""
from __future__ import annotations
import argparse
import base64
import contextlib
import hashlib
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import tempfile
import time
import urllib.parse

ROOT = Path(os.environ.get('DOOMSDAY_RESEARCH_ROOT', '/var/lib/doomsday-vault/research'))
DB = ROOT/'research.sqlite'
PDF_MAX = 40 * 1024 * 1024
WEB_MAX = 8 * 1024 * 1024
MAX_PAGES = 200
MAX_WEB_TEXT = 800_000
MAX_CHUNKS = 1300
TOKEN = re.compile(r'[^\W_]+',re.UNICODE)


@contextlib.contextmanager
def _db(path=None):
    path = Path(path or DB)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15)
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    conn.execute('''CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
        url TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
        sha256 TEXT NOT NULL, added INTEGER NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
        bytes INTEGER NOT NULL DEFAULT 0)''')
    conn.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(
        title, text, source_id UNINDEXED, page UNINDEXED, tokenize='unicode61')''')
    try:
        with conn:yield conn
    finally:conn.close()


def _chunks(text,size=2100,overlap=230):
    text = re.sub(r'[\t ]+', ' ',text)
    text = re.sub(r'\n{4,}', '\n\n', text).strip()
    if not text:return []
    result=[];start=0
    while start<len(text) and len(result)<MAX_CHUNKS:
        end=min(start+size,len(text))
        if end<len(text):
            nearby=text.rfind(' ',start+size//2,end)
            if nearby>start:end=nearby
        result.append(text[start:end])
        if end==len(text):break
        start=max(start+1,end-overlap)
    return result


def _record(conn, identifier, kind, title, url, file_path, digest, byte_size, pages):
    # Atomic FTS replacement keeps previous content on extraction failure.
    conn.execute('DELETE FROM chunks WHERE source_id=?',(identifier,))
    conn.execute('''INSERT OR REPLACE INTO sources
        (id,kind,title,url,path,sha256,added,pages,bytes)
        VALUES (?,?,?,?,?,?,?,?,?)''',(identifier,kind,title[:300],url,file_path,digest,int(time.time()),pages,byte_size))


def _pdf_pages(path):
    """Text-first extraction with limited, local Tesseract OCR for scanned pages."""
    if shutil.which('pdftotext'):
        cmd=['pdftotext','-f','1','-l',str(MAX_PAGES),'-layout',str(path),'-']
        # A disk-backed bounded result avoids loading a large PDF's full text in RAM.
        with tempfile.TemporaryFile() as output:
            proc=subprocess.run(cmd,stdout=output,stderr=subprocess.PIPE,timeout=100,check=False)
            if proc.returncode:
                raise ValueError('PDF text extraction failed (encrypted, invalid, or unsupported PDF)')
            size=output.tell()
            if size>9_000_000:raise ValueError('Extracted PDF text exceeds 9 MB; split the document')
            output.seek(0)
            raw=output.read().decode('utf-8',errors='replace')
        pages=raw.split('\f')[:MAX_PAGES]
        while pages and not pages[-1].strip():pages.pop()
        # pdftotext can emit *no* form-feed for a completely scanned PDF.
        # pdfinfo provides the authoritative page count for bounded OCR.
        if shutil.which('pdfinfo'):
            try:
                meta=subprocess.run(['pdfinfo',str(path)],capture_output=True,text=True,timeout=10,check=False)
                match=re.search(r'^Pages:\s*(\d+)\s*$',meta.stdout,re.M)
                if meta.returncode==0 and match:
                    count=min(int(match.group(1)),MAX_PAGES)
                    pages=pages[:count]+['']*max(0,count-len(pages))
            except (OSError,subprocess.TimeoutExpired):pass
        # OCR only sparse pages, capped to 12 per import to protect RAM/CPU.
        # Never OCR pages which already yielded meaningful extractable text.
        if any(len(page.strip())<12 for page in pages):
            from native_ocr import OCR_PDF_PAGES, pdf_page_text, languages
            if languages() and shutil.which('pdftoppm'):
                used=0
                for i,page in enumerate(pages):
                    if len(page.strip())>=12:continue
                    if used>=OCR_PDF_PAGES:break
                    used+=1
                    try:
                        found=pdf_page_text(path,i+1)
                        if found.strip():pages[i]=found
                    except (OSError,RuntimeError,ValueError):
                        # Keep all successfully extracted pages; OCR failure
                        # cannot corrupt the existing PDF or its database.
                        continue
        return pages
    try:
        from pypdf import PdfReader
    except ImportError as e:
        raise RuntimeError('Install poppler-utils (pdftotext) or python3-pypdf to read PDFs') from e
    reader=PdfReader(path,strict=False)
    if reader.is_encrypted:raise ValueError('Encrypted PDFs must be unlocked before importing')
    return [(p.extract_text() or '')[:50_000] for p in list(reader.pages)[:MAX_PAGES]]


def import_pdf(data,filename='uploaded.pdf',root=None):
    root=Path(root or ROOT)
    if not isinstance(data,(bytes,bytearray)) or not 16<=len(data)<=PDF_MAX:
        raise ValueError('PDF must be between 16 bytes and 40 MiB')
    if not data.lstrip(b'\xef\xbb\xbf\x00\r\n\t ').startswith(b'%PDF-'):
        raise ValueError('Invalid PDF signature')
    safe_title=Path(str(filename).replace('\\','/')).name
    if not safe_title.lower().endswith('.pdf'):raise ValueError('Only PDF files can be uploaded')
    safe_title=re.sub(r'[^\w. ()-]','_',safe_title)[:150] or 'document.pdf'
    digest=hashlib.sha256(data).hexdigest();identifier='pdf:'+digest
    root.mkdir(parents=True,mode=0o700,exist_ok=True)
    folder=root/'pdf';folder.mkdir(mode=0o700,exist_ok=True)
    final=folder/(digest+'.pdf')
    # Write+parse temporary bytes, then publish only after successful extraction.
    fd,tmp=tempfile.mkstemp(dir=folder,prefix='.incoming-',suffix='.pdf')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
        pages=_pdf_pages(tmp)
        if not any(p.strip() for p in pages):
            raise ValueError('No searchable PDF text. Install tesseract-ocr, tesseract-ocr-eng and poppler-utils for scanned PDFs (first 12 scanned pages per import).')
        rows=[]
        for pg,content in enumerate(pages,1):
            for chunk in _chunks(content[:80_000]):
                rows.append((safe_title,chunk,identifier,pg))
                if len(rows)>=MAX_CHUNKS:break
            if len(rows)>=MAX_CHUNKS:break
        if not rows:raise ValueError('No indexable text in this PDF')
        with _db(root/'research.sqlite') as db:
            old=db.execute('SELECT id FROM sources WHERE id=?',(identifier,)).fetchone()
            if old and final.is_file():return {'id':identifier,'title':safe_title,'pages':len(pages),'chunks':0,'existing':True}
            os.replace(tmp,final)
            _record(db,identifier,'pdf',safe_title,'',str(final),digest,len(data),len(pages))
            db.executemany('INSERT INTO chunks(title,text,source_id,page) VALUES (?,?,?,?)',rows)
        return {'id':identifier,'title':safe_title,'pages':len(pages),'chunks':len(rows),'existing':False}
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def import_text(raw,filename='article.txt',root=None):
    """Index a dropped UTF-8 article, Markdown or HTML file, without executing it."""
    root=Path(root or ROOT)
    if not isinstance(raw,(bytes,bytearray)) or not 1<=len(raw)<=2*1024*1024:
        raise ValueError('Article file must be at most 2 MiB')
    safe_title=Path(str(filename).replace('\\','/')).name
    if len(safe_title)>160 or not re.fullmatch(r'[\w .()-]+\.(?:txt|md|html|htm)',safe_title,re.I):
        raise ValueError('Upload a .txt, .md, .html, or .htm file with a safe filename')
    try:text=bytes(raw).decode('utf-8','strict')
    except UnicodeDecodeError as e:raise ValueError('Article text must be UTF-8') from e
    if safe_title.lower().endswith(('.html','.htm')):
        parser=_VisibleHTML();parser.feed(text);parser.close()
        text=' '.join(parser.sections)
    if not text.strip():raise ValueError('No readable article text')
    digest=hashlib.sha256(raw).hexdigest();identifier='note:'+digest
    parts=_chunks(text[:MAX_WEB_TEXT])
    with _db(root/'research.sqlite') as db:
        current=db.execute('SELECT id FROM sources WHERE id=?',(identifier,)).fetchone()
        if current:return {'id':identifier,'title':safe_title,'chunks':0,'existing':True}
        _record(db,identifier,'note',safe_title,'','',digest,len(raw),1)
        db.executemany('INSERT INTO chunks(title,text,source_id,page) VALUES (?,?,?,?)',[(safe_title,part,identifier,1) for part in parts])
    return {'id':identifier,'title':safe_title,'chunks':len(parts),'existing':False}

class _VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden=0;self.tags=[];self.sections=[];self.title='';self.in_title=False
    def handle_starttag(self,tag,attrs):
        if tag=='title':self.in_title=True
        if tag in ('script','style','svg','iframe','noscript','form','nav','footer','header'):
            self.hidden+=1;self.tags.append(tag)
        elif tag in ('p','li','h1','h2','h3','h4','main','article','section','br') and not self.hidden:
            self.sections.append('\n')
    def handle_endtag(self,tag):
        if tag=='title':self.in_title=False
        if self.tags and tag==self.tags[-1]:
            self.tags.pop();self.hidden=max(0,self.hidden-1)
        if tag in ('p','li','h1','h2','h3','article') and not self.hidden:self.sections.append('\n')
    def handle_data(self,data):
        if self.in_title:self.title+=data[:300]
        if not self.hidden and data.strip() and not self.in_title:self.sections.append(data)


def _web_address(url):
    if not isinstance(url,str) or len(url)>1800:raise ValueError('Invalid HTTPS URL')
    p=urllib.parse.urlsplit(url)
    if p.scheme.lower()!='https' or not p.hostname or p.username or p.password or p.port not in (None,443):
        raise ValueError('Only public HTTPS URLs on port 443 (without credentials) are allowed')
    if not p.hostname.isascii():raise ValueError('Use a public ASCII/punycode hostname')
    host=p.hostname.lower().rstrip('.')
    if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)+',host) or host.endswith(('.local','.internal','.localhost','.test','.invalid')):
        raise ValueError('Blocked private or invalid web hostname')
    try:ipaddress.ip_address(host)
    except ValueError:pass
    else:raise ValueError('IP-literal web URLs are not supported')
    return p,host


class _PinnedTLS(http.client.HTTPSConnection):
    def __init__(self,host,ip,timeout):
        super().__init__(host,443,timeout=timeout,context=ssl.create_default_context())
        self._pin=ip
    def connect(self):
        # Hostname validation and SNI remain the public DNS name, NOT the address.
        sock=socket.create_connection((self._pin,443),timeout=self.timeout)
        try:self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except Exception:sock.close();raise


def fetch_https(url,timeout=18,fetcher=None):
    """Single, user-authorized public HTTPS GET; no redirects, proxies, LAN or loopback."""
    p,host=_web_address(url)
    addresses=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    ips=list(dict.fromkeys(row[4][0] for row in addresses))
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips):
        raise ValueError('Web address resolves to a private or reserved IP')
    if fetcher is not None:return fetcher(url)
    conn=_PinnedTLS(host,ips[0],timeout)
    try:
        target=urllib.parse.urlunsplit(('', '', p.path or '/',p.query,''))
        conn.request('GET',target,headers={'Host':host,'User-Agent':'DoomsdayVaultResearch/1.0','Accept':'text/html, text/plain, application/pdf','Accept-Encoding':'identity','Connection':'close'})
        res=conn.getresponse()
        if res.status!=200:raise ValueError('Web request failed (redirects are deliberately not followed): HTTP '+str(res.status))
        mime=res.getheader('Content-Type','').split(';')[0].strip().lower()
        if mime not in ('text/html','text/plain','application/pdf'):
            raise ValueError('Only HTML, plain text and PDF webpages are supported')
        cap=PDF_MAX if mime=='application/pdf' else WEB_MAX
        declared=res.getheader('Content-Length','')
        if declared.isdecimal() and int(declared)>cap:raise ValueError('Remote document exceeds size limit')
        body=res.read(cap+1)
        if len(body)>cap:raise ValueError('Remote document exceeds size limit')
        return mime,body
    finally:conn.close()


def import_web(url,allow_online=False,root=None,fetcher=None):
    if allow_online is not True:raise ValueError('External access is OFF. Explicitly enable HTTPS import for this URL')
    root=Path(root or ROOT);p,host=_web_address(url)
    mime,raw=fetch_https(url,fetcher=fetcher)
    if mime=='application/pdf':
        name=Path(urllib.parse.unquote(p.path)).name
        if not name.lower().endswith('.pdf'):name=(name or 'download')+'.pdf'
        result=import_pdf(raw,name,root=root)
        # A PDF URL remains part of the imported source record, not a replacement for local byte access.
        with _db(root/'research.sqlite') as db:db.execute('UPDATE sources SET url=? WHERE id=?',(url,result['id']))
        return {**result,'url':url,'kind':'pdf'}
    webpage_title=''
    if mime=='text/html':
        parser=_VisibleHTML()
        parser.feed(raw.decode('utf-8',errors='replace'))
        content=' '.join(parser.sections)
        webpage_title=parser.title.strip()[:180]
    else:content=raw.decode('utf-8',errors='replace')
    content=html.unescape(content)[:MAX_WEB_TEXT]
    if len(content.strip())<30:raise ValueError('No usable article text was extracted')
    digest=hashlib.sha256(raw).hexdigest();identifier='web:'+hashlib.sha256(url.encode('utf-8')).hexdigest()
    title=(webpage_title+' — '+host) if webpage_title else (host+' — '+(Path(p.path).name or 'homepage'))
    rows=[(title,chunk,identifier,0) for chunk in _chunks(content)]
    with _db(root/'research.sqlite') as db:
        _record(db,identifier,'web',title,url,'',digest,len(raw),0)
        db.executemany('INSERT INTO chunks(title,text,source_id,page) VALUES (?,?,?,?)',rows)
    return {'id':identifier,'kind':'web','title':title,'url':url,'chunks':len(rows)}


def _terms(query):
    tokens=TOKEN.findall(str(query)[:320])[:14]
    return ' OR '.join('"'+w.replace('"','')+'"' for w in tokens)


def _links(row):
    ident,kind,title,url,pg,excerpt=row
    if kind=='pdf':
        link='/api/research/file/'+ident[4:]+'#page='+str(pg or 1)
        ident=ident+'@'+str(pg or 1)
    elif kind=='note':link='/api/research/source?id='+urllib.parse.quote(ident,safe='')
    else:link=url
    return {'id':ident,'kind':kind,'title':title,'link':link,'page':pg or None,'snippet':excerpt,'source':kind.upper()}


def search(query,root=None,limit=10,include_zim=True):
    root=Path(root or ROOT);limit=max(1,min(int(limit),25))
    output=[];token=_terms(query)
    if token and (root/'research.sqlite').is_file():
        with _db(root/'research.sqlite') as db:
            rows=db.execute('''SELECT c.source_id,s.kind,s.title,s.url,CAST(c.page AS INTEGER),
                snippet(chunks,1,'[',']','…',32) AS excerpt
                FROM chunks AS c JOIN sources AS s ON c.source_id=s.id
                WHERE chunks MATCH ? ORDER BY bm25(chunks,5.0,1.0) LIMIT ?''', (token,limit*5)).fetchall()
        seen=set()
        for row in rows:
            key=(row[0],row[4])
            if key in seen:continue
            seen.add(key);output.append(_links(row))
            if len(output)>=limit:break
    if include_zim:
        import zim_index
        articles=zim_index.article_title_search(query,db_path=zim_index.DB,limit=max(1,min(8,limit)))
        for a in articles:
            output.append({'id':'zim:'+base64.urlsafe_b64encode(json.dumps([a['zim'],a['path']],separators=(',',':')).encode()).decode().rstrip('='),
                           'kind':'zim','title':a['title'],'snippet':a['snippet'][:1500],
                           'link':a['link'],'zim':a['zim'],'path':a['path'],'source':a['source']})
    return output[:limit]


def read(identifier,root=None,max_chars=14000):
    root=Path(root or ROOT)
    if not isinstance(identifier,str):return None
    if identifier.startswith('zim:'):
        # Decode identity and authorize it against the local ZIM FTS index.
        from zim_index import DB as zdb,connect as zim_connect,zim_url
        encoded=identifier[4:]
        if not zdb.is_file() or len(encoded)>3200 or not re.fullmatch(r'[A-Za-z0-9_-]+',encoded):return None
        try:
            pair=json.loads(base64.urlsafe_b64decode(encoded+'='*((-len(encoded))%4)))
            if not isinstance(pair,list) or len(pair)!=2 or not all(isinstance(x,str) for x in pair):return None
            zim,path=pair
        except (ValueError,UnicodeError):return None
        with contextlib.closing(zim_connect(zdb)) as db:
            first=db.execute('SELECT title FROM articles WHERE zim=? AND path=? LIMIT 1',(zim,path)).fetchone()
            if first is None:return None
            rows=db.execute('SELECT text FROM articles WHERE zim=? AND path=? ORDER BY rowid LIMIT 20',(zim,path)).fetchall()
        return {'id':identifier,'kind':'zim','title':first[0],'link':zim_url(zim,path),
                'text':'\n'.join(x[0] for x in rows)[:max_chars]}
    scoped=re.fullmatch(r'(pdf:[a-f0-9]{64})@([1-9][0-9]{0,3})',identifier)
    page=int(scoped.group(2)) if scoped else None
    actual_id=scoped.group(1) if scoped else identifier
    if not re.fullmatch(r'(pdf|web|note):[a-f0-9]{64}',actual_id):return None
    if not (root/'research.sqlite').exists():return None
    with _db(root/'research.sqlite') as db:
        row=db.execute('SELECT id,kind,title,url,pages FROM sources WHERE id=?',(actual_id,)).fetchone()
        if not row:return None
        if page is not None:
            chunks=db.execute('SELECT text,CAST(page AS INTEGER) FROM chunks WHERE source_id=? AND page=? ORDER BY rowid LIMIT 24',(actual_id,page)).fetchall()
        else:
            chunks=db.execute('SELECT text,CAST(page AS INTEGER) FROM chunks WHERE source_id=? ORDER BY rowid LIMIT 24',(actual_id,)).fetchall()
    if not chunks:return None
    link='/api/research/file/'+actual_id[4:]+'#page='+str(page or 1) if row[1]=='pdf' else row[3]
    return {'id':identifier,'kind':row[1],'title':row[2],'url':row[3], 'pages':row[4],
            'link':link,'text':'\n'.join(('[Page '+str(pg)+'] ' if row[1]=='pdf' else '')+s for s,pg in chunks)[:max_chars]}


def pdf_path(identifier,root=None):
    if not re.fullmatch(r'[a-f0-9]{64}',identifier):raise ValueError('Invalid PDF identifier')
    root=Path(root or ROOT)
    if not (root/'research.sqlite').exists():raise FileNotFoundError('PDF not found')
    with _db(root/'research.sqlite') as db:
        row=db.execute('SELECT path FROM sources WHERE id=? AND kind=?',('pdf:'+identifier,'pdf')).fetchone()
    file=(root/'pdf'/(identifier+'.pdf'))
    if not row or str(file)!=row[0] or file.is_symlink() or not file.is_file():raise FileNotFoundError('PDF not found')
    return file


def status(root=None):
    root=Path(root or ROOT)
    if not (root/'research.sqlite').exists():return {'sources':0,'pdfs':0,'webpages':0,'indexed_chunks':0,'online_default':False}
    with _db(root/'research.sqlite') as db:
        kinds=dict(db.execute('SELECT kind,COUNT(*) FROM sources GROUP BY kind').fetchall())
        n=db.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    return {'sources':sum(kinds.values()),'pdfs':kinds.get('pdf',0),'webpages':kinds.get('web',0),'indexed_chunks':n,'online_default':False}


def prepare_answer(question,root=None,source_ids=None,limit=6):
    if not str(question).strip():raise ValueError('Enter a research question')
    if source_ids is not None:
        if not isinstance(source_ids,list) or len(source_ids)>10:raise ValueError('Too many source IDs')
        sources=[read(i,root=root,max_chars=2600) for i in source_ids]
        sources=[s for s in sources if s]
    else:
        candidates=search(question,root=root,limit=limit,include_zim=True)
        sources=[read(x['id'],root=root,max_chars=2600) for x in candidates[:limit]]
        sources=[s for s in sources if s]
    citations=[{'number':i,'id':s['id'],'title':s['title'],'kind':s['kind'],'link':s['link'],
                'excerpt':s['text'][:1400]} for i,s in enumerate(sources,1)]
    return citations


def main(argv=None):
    p=argparse.ArgumentParser(description='DoomsdayOS source-grounded research library')
    p.add_argument('command',choices=['add-pdf','add-url','search','open','status'])
    p.add_argument('value',nargs='?');p.add_argument('--online',action='store_true')
    args=p.parse_args(argv)
    if args.command=='add-pdf':
        if not args.value: p.error('add-pdf requires a local PDF path')
        path=Path(args.value).resolve(strict=True)
        if not path.is_file() or path.is_symlink() or path.stat().st_size>PDF_MAX: p.error('PDF missing, symlink, or too large')
        result=import_pdf(path.read_bytes(),path.name)
    elif args.command=='add-url':
        if not args.value:p.error('add-url requires an HTTPS URL')
        result=import_web(args.value,allow_online=args.online)
    elif args.command=='search':result=search(args.value or '')
    elif args.command=='open':result=read(args.value or '')
    else:result=status()
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
