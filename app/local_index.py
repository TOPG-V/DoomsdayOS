#!/usr/bin/env python3
"""Small offline SQLite FTS index for explicitly imported local text documents.

No assertion that full ZIM archives are indexed. Native Kiwix search remains
available separately. This index is for documents a user imports and cites.
"""
import re, sqlite3, os
from pathlib import Path

DEFAULT_DB = Path(os.environ.get('DOOMSDAY_TEXT_INDEX','/var/lib/doomsday-vault/ai-documents.sqlite'))


def connect(path=DEFAULT_DB):
    db=sqlite3.connect(str(path),timeout=5)
    db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(title, text, source UNINDEXED, tokenize="unicode61")')
    return db


def safe_query(q):
    words=re.findall(r'\w+',(q or '')[:256],re.U)[:12]
    return ' OR '.join('"'+w.replace('"','')+'"' for w in words)


def ingest(directory,db_path=DEFAULT_DB,limit=500):
    root=Path(directory).resolve(strict=True)
    if not root.is_dir():raise ValueError('Directory expected')
    out=Path(db_path);out.parent.mkdir(parents=True,exist_ok=True)
    count=0
    with connect(out) as db:
        for p in sorted(root.rglob('*')):
            if count>=limit:break
            if p.is_symlink() or not p.is_file() or p.suffix.lower() not in ('.txt','.md') or p.stat().st_size>2_000_000:continue
            text=p.read_text(encoding='utf-8',errors='replace')
            if not text.strip():continue
            # Avoid processing the same document twice after rerun.
            db.execute('DELETE FROM docs WHERE source=?',(str(p),))
            db.execute('INSERT INTO docs (title,text,source) VALUES (?,?,?)',(p.name,text[:2_000_000],str(p)))
            count+=1
        db.commit()
    return count


def search(q,db_path=DEFAULT_DB,limit=5):
    if not Path(db_path).exists():return []
    tokens=safe_query(q)
    if not tokens:return []
    with connect(db_path) as db:
        rows=db.execute('SELECT rowid, title, snippet(docs,1,"[", "]", "…", 24), source FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?', (tokens,min(20,int(limit)))).fetchall()
    return [{'id':i,'title':title,'snippet':text,'source':source,'link':f'/api/article?id={i}'} for i,title,text,source in rows]


def article(article_id,db_path=DEFAULT_DB):
    if not Path(db_path).exists():return None
    try:i=int(article_id)
    except (ValueError,TypeError):return None
    with connect(db_path) as db:
        row=db.execute('SELECT title,text,source FROM docs WHERE rowid=?',(i,)).fetchone()
    return {'title':row[0],'text':row[1][:120000],'source':row[2]} if row else None


def main():
    import argparse,json
    p=argparse.ArgumentParser(description='Explicit offline text index for local RAG; does not automatically read every ZIM')
    p.add_argument('action',choices=['add','search'])
    p.add_argument('value')
    a=p.parse_args()
    if a.action=='add':print('Indexed',ingest(a.value),'local .txt/.md files')
    else:print(json.dumps(search(a.value),indent=2))

if __name__=='__main__':main()
