#!/usr/bin/env python3
"""Kiwix OPDS selector and crash-safe, integrity-checked ZIM download queue.
Only HTTPS publisher archives, no third-party dependencies. No implicit disk formatting.
"""
from __future__ import annotations
import argparse, contextlib, fcntl, hashlib, ipaddress, json, os, re, shutil, socket, subprocess, sys, tempfile, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

BASE = Path(os.environ.get('DOOMSDAY_STATE_DIR','/var/lib/doomsday-vault'))
CONF = Path(os.environ.get('DOOMSDAY_CONFIG','/etc/doomsday-vault/firstboot.json'))
CATALOG = 'https://library.kiwix.org/catalog/v2/entries'
# Permit official HTTPS download sources only: protects privileged automatic downloader from SSRF.
HOSTS = ('download.kiwix.org', 'mirror.download.kiwix.org')
UA = 'DoomsdayVault/3 (offline archive downloader)'
CHUNK = 1024 * 1024
RESERVE = 3 * 1024**3


def safe_url(url):
    p=urllib.parse.urlsplit(url)
    if p.scheme!='https' or p.username or p.password or p.port or (p.hostname or '').lower() not in HOSTS:
        raise ValueError('Only HTTPS downloads from official download.kiwix.org hosts are permitted')
    if not p.path.lower().endswith('.zim') or '/../' in p.path:
        raise ValueError('Not an official .zim path')
    return url

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        p=urllib.parse.urlsplit(newurl)
        if p.scheme != 'https' or not p.hostname or p.username or p.password:
            raise ValueError('Unsafe HTTP redirect')
        # Kiwix uses HTTPS third-party mirrors. Do not redirect to LAN, loopback or
        # reserved addresses; security-sensitive deployments should pin mirrors instead.
        hostname=p.hostname
        addresses=socket.getaddrinfo(hostname,443,type=socket.SOCK_STREAM)
        if not addresses or not all(ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError('Refusing redirect to non-public network address')
        return super().redirect_request(req, fp, code, msg, headers, newurl)
OPENER=urllib.request.build_opener(SafeRedirect())

def request(url, headers=None, timeout=60):
    return OPENER.open(urllib.request.Request(url,headers={'User-Agent':UA,'Accept-Encoding':'identity',**(headers or {})}),timeout=timeout)

def config():
    defaults={'zim_dir':'/srv/doomsday-zims', 'backup_dir':'', 'require_sha256':True, 'immutable_zims':True, 'min_free_gib':3}
    if CONF.exists(): defaults.update(json.loads(CONF.read_text()))
    return defaults

def atomic_json(path:Path, data):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name+'.',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,indent=2,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def queue_path():return BASE/'zim-queue.json'
def read_queue():
    try: return json.loads(queue_path().read_text())
    except FileNotFoundError:return []

@contextlib.contextmanager
def locked_queue():
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with open(BASE/'zim.lock','a+') as f:
        fcntl.flock(f.fileno(),fcntl.LOCK_EX)
        yield
        fcntl.flock(f.fileno(),fcntl.LOCK_UN)

def size_text(n): return f'{n/1024**3:.2f} GiB'

def dataset_at(path):
    if not path.exists():return ''
    r=subprocess.run(['findmnt','-T',str(path),'-n','-o','SOURCE'],capture_output=True,text=True)
    return r.stdout.strip() if r.returncode==0 else ''

def guard_path(path,expected=''):
    # Prevent hot-unplugged data pools from silently redirecting new ZIMs onto /.
    path=Path(path)
    src=dataset_at(path)
    if expected and src!=expected:
        raise ValueError(f'Required storage dataset {expected} is absent at {path} (found {src or "none"}); not writing to the wrong disk')
    if str(path).startswith('/tank/') and not src.startswith('tank/'):
        raise ValueError('Data pool tank is not mounted. Download postponed safely.')
    return src

def disk_free(path):
    guard_path(path)
    return shutil.disk_usage(path).free

def zfs_copies(path):
    import subprocess
    r=subprocess.run(['findmnt','-T',str(path),'-n','-o','SOURCE'],capture_output=True,text=True)
    if r.returncode:
        return 1
    ds=r.stdout.strip()
    r=subprocess.run(['zfs','get','-H','-o','value','copies',ds],capture_output=True,text=True)
    try:return int(r.stdout.strip()) if r.returncode==0 else 1
    except ValueError:return 1

def is_separate_physical_pool(src,dst):
    import subprocess
    def pool(path):
        r=subprocess.run(['findmnt','-T',str(path),'-n','-o','SOURCE'],capture_output=True,text=True)
        return r.stdout.strip().split('/')[0] if r.returncode==0 else ''
    a,b=pool(src),pool(dst)
    return bool(a and b and a!=b)

def fetch_checksum(url):
    # Official Kiwix sidecar uses SHA-256; reject responses that cannot be parsed.
    safe_url(url)
    side=url+'.sha256'
    try:
        with request(side,timeout=30) as r:
            text=r.read(4096).decode('utf-8','replace').strip()
    except Exception as e:
        raise ValueError('Cannot obtain verified SHA-256 sidecar '+side+': '+str(e)) from e
    m=re.search(r'(?i)\b([0-9a-f]{64})\b',text)
    if not m: raise ValueError('Official sidecar has no SHA-256 digest')
    return m.group(1).lower()

def parse_catalog(xml):
    root=ET.fromstring(xml)
    atom='{http://www.w3.org/2005/Atom}'
    ret=[]
    for entry in root.findall(atom+'entry'):
        title=(entry.findtext(atom+'title') or '').strip()
        ident=(entry.findtext(atom+'id') or title).strip()
        for link in entry.findall(atom+'link'):
            href=link.get('href','')
            rel=link.get('rel','')
            if 'acquisition/open-access' not in rel or not href.lower().split('?')[0].endswith('.zim'):continue
            try:safe_url(href.split('?')[0])
            except ValueError:continue
            size=0
            # OPDS implementations vary: size or zimSize may be entry-level.
            for key,val in {**entry.attrib,**link.attrib}.items():
                if key.lower().endswith(('zimsize','length','size')):
                    with contextlib.suppress(ValueError):size=max(size,int(val))
            for node in entry.iter():
                key=node.tag.rsplit('}',1)[-1].lower()
                if key in ('zimsize','size','filesize') and node.text:
                    with contextlib.suppress(ValueError):size=max(size,int(node.text.strip()))
            ret.append({'id':ident,'title':title or Path(urllib.parse.urlsplit(href).path).name,'url':href.split('?')[0],'size':size})
            break
    return ret

def catalog(search='',limit=100,fetcher=None):
    # Bounded pagination by default; user can ask for all via --all but must confirm capacities.
    items=[];seen=set(); start=0;count=100
    fetcher=fetcher or (lambda url:request(url,timeout=45).read())
    while len(items)<limit:
        url=CATALOG+'?'+urllib.parse.urlencode({'start':start,'count':min(count,limit-len(items))})
        found=parse_catalog(fetcher(url))
        if not found:break
        for item in found:
            if item['url'] not in seen:
                seen.add(item['url'])
                if not search or search.casefold() in item['title'].casefold():items.append(item)
        start+=count
        if len(found)<min(count,limit):break
        # Bound broad searches without a user-provided filter (never quietly fetch entire internet catalog).
        if start>=10000:break
    return items

def enqueue(items):
    with locked_queue():
        q=read_queue(); existing={x['url'] for x in q}
        for item in items:
            safe_url(item['url'])
            if item['url'] in existing:continue
            q.append({**item,'backup':bool(item.get('backup',False)),'status':'pending','error':''})
        atomic_json(queue_path(),q)

def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(4*CHUNK),b''):h.update(b)
    return h.hexdigest()

def content_range_ok(h,start):
    m=re.match(r'bytes (\d+)-(\d+)/(\d+|\*)$',h or '')
    return bool(m and int(m.group(1))==start)

def download(item, dst, opener=request):
    url=safe_url(item['url']); name=Path(urllib.parse.urlsplit(url).path).name
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.zim',name):raise ValueError('Unexpected filename')
    if not dst.is_dir():raise ValueError('ZIM mount/directory absent; refusing to recreate it on system drive')
    guard_path(dst)
    final=dst/name; part=dst/(name+'.part'); meta=dst/(name+'.part.json')
    expected=(item.get('sha256') or fetch_checksum(url)).lower()
    if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('Invalid SHA-256')
    item['sha256']=expected
    if final.exists():
        if sha256(final)==expected:return final
        raise ValueError('Existing ZIM has different hash; leave untouched for recovery')
    saved={}
    if meta.exists():
        try:saved=json.loads(meta.read_text())
        except (ValueError,OSError):saved={}
    if part.exists() and (saved.get('url')!=url or saved.get('sha256')!=expected):
        # Unknown partials should not be blindly appended to.
        raise ValueError('Partial file does not match URL/checksum metadata; investigate manually')
    offset=part.stat().st_size if part.exists() else 0
    hdr={}
    if offset:
        hdr['Range']=f'bytes={offset}-'
        if saved.get('etag'):hdr['If-Range']=saved['etag']
        elif saved.get('last_modified'):hdr['If-Range']=saved['last_modified']
    r=opener(url,hdr)
    with r:
        status=getattr(r,'status',200)
        if offset and (status!=206 or not content_range_ok(r.headers.get('Content-Range'),offset)):
            # Server did not honor the range; restart from zero safely.
            r.close();offset=0
            r=opener(url,{})
        with r:
            meta_data={'url':url,'sha256':expected,'etag':r.headers.get('ETag'),'last_modified':r.headers.get('Last-Modified')}
            if saved.get('etag') and offset and meta_data['etag'] and saved['etag']!=meta_data['etag']:
                raise ValueError('ETag changed during resume; refusing to append')
            atomic_json(meta,meta_data)
            free=disk_free(dst); est=int(item.get('size') or 0)
            mult=zfs_copies(dst); remaining=max(0,est-offset)
            if est and free < remaining*mult+RESERVE:
                raise ValueError(f'Insufficient ZIM capacity: need {size_text(remaining*mult+RESERVE)} free, have {size_text(free)}')
            with open(part,'ab' if offset else 'wb') as f:
                while True:
                    b=r.read(CHUNK)
                    if not b:break
                    f.write(b)
                f.flush();os.fsync(f.fileno())
    if item.get('size') and part.stat().st_size!=int(item['size']):
        raise ValueError('Downloaded byte size differs from catalog; retained .part for retry')
    actual=sha256(part)
    if actual!=expected:
        bad=dst/(name+'.bad-sha256')
        os.replace(part,bad)
        meta.unlink(missing_ok=True)
        raise ValueError('SHA-256 mismatch; quarantined corrupted download as '+str(bad))
    os.replace(part,final);meta.unlink(missing_ok=True)
    return final

def backup_zim(source,backupdir):
    if not backupdir:return 'none'
    backupdir=Path(backupdir)
    if not backupdir.is_dir():raise ValueError('Backup storage missing; will retry after reconnect')
    guard_path(backupdir)
    target=backupdir/source.name
    srcsum=sha256(source)
    if target.exists() and sha256(target)==srcsum:return 'verified-existing'
    if target.exists():raise ValueError('Backup filename exists with incorrect checksum; refusing overwrite')
    required=source.stat().st_size*zfs_copies(backupdir)+RESERVE
    if disk_free(backupdir)<required:raise ValueError('Backup destination lacks capacity')
    part=backupdir/(source.name+'.copying')
    with open(source,'rb') as a,open(part,'wb') as b:
        shutil.copyfileobj(a,b,4*CHUNK);b.flush();os.fsync(b.fileno())
    if sha256(part)!=srcsum:raise ValueError('Backup verification failed')
    os.replace(part,target)
    return 'verified-new'

def run_queue():
    """Downloader writes only to staging; ZFS archive is immutable between verified publishes."""
    cfg=config()
    if not cfg.get('immutable_zims'):
        raise RuntimeError('Immutable archive not configured; run sudo ./scripts/prepare-zims.sh')
    from archive_guard import checked_archive, stage_dir, publish_verified
    dst, _ = checked_archive(cfg)
    stage = stage_dir(cfg)
    backupdir=cfg.get('backup_dir','')
    new_downloads=0
    with locked_queue():
        q=read_queue()
        for item in q:
            if item['status']=='done':continue
            try:
                item['status']='downloading';atomic_json(queue_path(),q)
                guard_path(dst,cfg.get('zim_dataset',''))
                guard_path(stage,cfg.get('staging_dataset',''))
                name=Path(urllib.parse.urlsplit(item['url']).path).name
                expected=(item.get('sha256') or fetch_checksum(item['url'])).lower()
                if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('No trusted SHA-256')
                item['sha256']=expected
                if (dst/name).is_file():
                    if sha256(dst/name)!=expected:
                        raise ValueError('Installed archive differs from checksum; preserve it for recovery')
                    from archive_guard import record_manifest
                    record_manifest(name,expected,(dst/name).stat().st_size,item['url'])
                    out=dst/name
                else:
                    stage_file=download(item,stage)
                    out=publish_verified(stage_file,expected,item['url'])
                    stage_file.unlink(missing_ok=True)
                    new_downloads+=1
                item['status']='downloaded';atomic_json(queue_path(),q)
                if item.get('backup') and backupdir:
                    guard_path(Path(backupdir),cfg.get('backup_dataset',''))
                    item['backup_result']=backup_zim(out,backupdir)
                elif item.get('backup'):item['backup_result']='deferred-no-backup-drive'
                item['status']='done';item['error']=''
            except Exception as e:
                item['status']='retry';item['error']=str(e)[:600]
                print('Queued for retry:',item.get('title'),item['error'],file=sys.stderr)
            atomic_json(queue_path(),q)
    if new_downloads and Path('/run/systemd/system').exists():
        subprocess.run(['systemctl','try-restart','doomsday-kiwix.service'],check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)


def interactive_wizard(input_fn=input,fetch=None):
    print('\nDoomsday ZIM Library — choose archives one by one or all from a filtered catalog.')
    print('Full Kiwix catalog may exceed available storage. No downloads start until you confirm.')
    term=input_fn('Filter term (e.g. wikipedia_en, medical; empty = first 100): ').strip()
    try:items=catalog(term,limit=100,fetcher=fetch)
    except Exception as e:
        print('Catalog unavailable:',e,'\nYou can rerun setup later when online.');return []
    if not items:print('No matching downloadable ZIMs.');return []
    for i,x in enumerate(items,1):print(f"{i:>3}  {x['title'][:65]:65} {size_text(x['size']) if x['size'] else 'size unknown'}")
    sel=input_fn('Numbers (1,3,7), all matching catalog entries, or skip: ').strip().lower()
    if sel in ('','skip','none'):return []
    if sel=='all':
        print('Fetching the full matching catalog (up to 10,000 entries) before checking space...')
        try:chosen=catalog(term,limit=10000,fetcher=fetch)
        except Exception as e:print('Cannot retrieve full catalog:',e);return []
    else:
        try:chosen=[items[int(n.strip())-1] for n in sel.split(',')]
        except (ValueError,IndexError):print('Invalid selection');return []
    cfg=config();dst=Path(cfg['zim_dir']);avail=disk_free(dst);factor=zfs_copies(dst)
    unknown=[x for x in chosen if not x['size']]
    total=sum(x['size'] for x in chosen if x['size'])*factor
    stage=Path(cfg.get('staging_dir',cfg['zim_dir']))
    stage_free=disk_free(stage); stage_copies=zfs_copies(stage)
    biggest=max((x['size'] for x in chosen), default=0)
    peak=total+biggest*stage_copies+RESERVE if (dataset_at(stage).split('/')[0]==dataset_at(dst).split('/')[0]) else total+RESERVE
    print(f'Final archives estimate: {size_text(total)}; temporary staging max ~{size_text(biggest*stage_copies)}; current pool free {size_text(avail)}.')
    if unknown:
        print('Unknown sizes: cannot safely select all until sizes are determined. Choose individually or obtain sizes first.')
        if sel=='all':return []
    if peak>avail or biggest*stage_copies+RESERVE>stage_free:
        print('Insufficient free space for archive + temporary staging at peak. Choose a smaller set. Nothing queued.')
        return []
    bdir=cfg.get('backup_dir')
    if bdir:
        bd=Path(bdir); bfree=disk_free(bd); bf=zfs_copies(bd)
        print(f'Backup drive: {size_text(bfree)} free, ~{size_text(sum(x["size"] for x in chosen)*bf)} for all selected.')
        if not is_separate_physical_pool(dst,bd):print('WARNING: Backup is on same ZFS pool; not independent against pool failure.')
        bc=input_fn('Which ZIMs to back up? Numbers in the SELECTED list, all, none: ').strip().lower()
        if bc=='all':candidates=list(range(1,len(chosen)+1))
        elif bc in ('','none','skip'):candidates=[]
        else:
            try:candidates=[int(s.strip()) for s in bc.split(',')]
            except ValueError:candidates=[]
        candidates=[n for n in dict.fromkeys(candidates) if 1<=n<=len(chosen)]
        remaining=bfree-RESERVE
        for n in candidates:
            it=chosen[n-1]
            cost=it['size']*bf
            if it['size']==0:
                print('Unknown backup size, deferred:',it['title']);continue
            if cost<=remaining:it['backup']=True;remaining-=cost
            else:print('Not enough backup space for:',it['title'])
    else:print('No independent ZIM backup destination configured. ZFS block copies and snapshots are not separate backups.')
    confirm=input_fn('Queue these '+str(len(chosen))+' archive(s) for SHA-verified resumable download? type YES: ')
    if confirm!='YES':return []
    enqueue(chosen)
    print('Queued. Automatic timer resumes downloads after power interruption.')
    return chosen

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['wizard','run','status','enqueue-url']);p.add_argument('--url');p.add_argument('--sha256');p.add_argument('--size',type=int,default=0)
    a=p.parse_args()
    if a.command=='wizard':interactive_wizard()
    elif a.command=='run':run_queue()
    elif a.command=='status':
        for x in read_queue():print(x['status'],x['title'],x.get('error',''))
    elif a.command=='enqueue-url':
        if not a.url or not a.sha256: p.error('--url and --sha256 required')
        enqueue([{'id':a.url,'title':Path(urllib.parse.urlsplit(a.url).path).name,'url':a.url,'size':a.size,'sha256':a.sha256,'backup':False}])
if __name__=='__main__':main()
