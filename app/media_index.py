#!/usr/bin/env python3
"""Optional classic perceptual-hash image index for user-enrolled folders.

No AI models needed. Does NOT automatically extract media from ZIM archives.
Requires python3-pil, used only if the user enables indexing explicitly.
"""
from __future__ import annotations
import io,os,sqlite3,json,argparse,tempfile
from pathlib import Path

DEFAULT_DB=Path(os.environ.get('DOOMSDAY_IMAGE_INDEX','/var/lib/doomsday-vault/image-index.sqlite'))
FOLDER_CONFIG=Path(os.environ.get('DOOMSDAY_MEDIA_CONFIG','/etc/doomsday-vault/media-index.json'))
MAX_BYTES=12*1024*1024


def fingerprint(raw:bytes):
    if len(raw)>MAX_BYTES:raise ValueError('Image exceeds 12 MiB limit')
    try:
        from PIL import Image,ImageOps
    except ImportError as e:raise RuntimeError('Install python3-pil to use classic image matching') from e
    with Image.open(io.BytesIO(raw)) as i:
        if i.width*i.height>30_000_000:raise ValueError('Image too large')
        i=ImageOps.exif_transpose(i).convert('L').resize((8,8))
        pix=list(i.getdata());mean=sum(pix)/64
        result=sum((1<<n) for n,v in enumerate(pix) if v>=mean)
    return result


def _db(p):
    c=sqlite3.connect(str(p),timeout=5)
    c.execute('CREATE TABLE IF NOT EXISTS pictures (path TEXT PRIMARY KEY, hash TEXT NOT NULL, title TEXT NOT NULL, mtime_ns INTEGER, file_size INTEGER)')
    columns={x[1] for x in c.execute('PRAGMA table_info(pictures)').fetchall()}
    if 'mtime_ns' not in columns:c.execute('ALTER TABLE pictures ADD COLUMN mtime_ns INTEGER')
    if 'file_size' not in columns:c.execute('ALTER TABLE pictures ADD COLUMN file_size INTEGER')
    return c


def index_folder(directory,db_path=DEFAULT_DB,limit=5000):
    root=Path(directory).resolve(strict=True)
    if not root.is_dir():raise ValueError('Folder required')
    out=Path(db_path);out.parent.mkdir(parents=True,exist_ok=True)
    scanned=0;skipped=0;unchanged=0
    with _db(out) as c:
        for p in sorted(root.rglob('*')):
            if scanned>=limit:break
            if p.is_symlink() or not p.is_file() or p.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):continue
            stat=p.stat()
            if stat.st_size>MAX_BYTES:skipped+=1;continue
            old=c.execute('SELECT mtime_ns,file_size FROM pictures WHERE path=?',(str(p),)).fetchone()
            if old and old==(stat.st_mtime_ns,stat.st_size):unchanged+=1;continue
            try:h=fingerprint(p.read_bytes())
            except (OSError,ValueError):skipped+=1;continue
            c.execute('INSERT OR REPLACE INTO pictures (path,hash,title,mtime_ns,file_size) VALUES (?,?,?,?,?)',(str(p),f'{h:016x}',p.name,stat.st_mtime_ns,stat.st_size))
            scanned+=1
        c.commit()
    return {'indexed':scanned,'skipped':skipped,'unchanged':unchanged}


def similar(raw,db_path=DEFAULT_DB,limit=12):
    if not Path(db_path).exists():return []
    query=fingerprint(raw)
    with _db(db_path) as c:
        rows=c.execute('SELECT path,hash,title FROM pictures').fetchall()
    scored=[]
    for path,h,title in rows:
        distance=(query^int(h,16)).bit_count()
        scored.append({'name':title,'distance':distance,'match_percent':round((64-distance)/64*100,1),'source':path})
    return sorted(scored,key=lambda x:x['distance'])[:limit]


def enroll(directory):
    """Only with explicit user action; subsequent timer jobs run at idle priority."""
    root=Path(directory).resolve(strict=True)
    if not root.is_dir():raise ValueError('Directory required')
    FOLDER_CONFIG.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=FOLDER_CONFIG.parent,prefix='.media-index-')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump({'image_folder':str(root)},f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,FOLDER_CONFIG)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)
    return root


def run_enrolled():
    if not FOLDER_CONFIG.exists():return {'skipped':'No image folder explicitly enrolled'}
    cfg=json.loads(FOLDER_CONFIG.read_text())
    folder=Path(cfg['image_folder'])
    if not folder.is_dir():return {'skipped':'Enrolled image folder is not mounted'}
    return index_folder(folder)


def main():
    p=argparse.ArgumentParser(description='Optional idle classic image index (no model required)')
    p.add_argument('action',choices=['enroll','run','status'])
    p.add_argument('path',nargs='?')
    a=p.parse_args()
    if a.action=='enroll':
        if not a.path:p.error('Provide the folder to index')
        print('Enrolled:',enroll(a.path))
    elif a.action=='run':print(json.dumps(run_enrolled(),indent=2))
    else:print(FOLDER_CONFIG.read_text() if FOLDER_CONFIG.exists() else 'No image folder enrolled')

if __name__=='__main__':main()
