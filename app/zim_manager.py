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
OS_BACKUP_RESERVE = 20 * 1024**3
DEFAULT_OS_BACKUP_DIR = Path("/var/lib/doomsday-vault/zim-backups")
BUNDLE_MANIFEST = Path(__file__).with_name('bundle_manifest.json')


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

def _catalog_url(href):
    """Normalize OPDS .zim.meta4 acquisition references to the actual ZIM.

    Kiwix routinely uses a .zim.meta4 link even when advertising type
    application/x-zim.  The downloader needs the .zim bytes, not the metalink.
    """
    parsed = urllib.parse.urlsplit(href)
    path = parsed.path
    if path.lower().endswith('.zim.meta4'):
        path = path[:-len('.meta4')]
    url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    try:
        return safe_url(url)
    except ValueError:
        return None


def parse_catalog(xml):
    root=ET.fromstring(xml)
    atom='{http://www.w3.org/2005/Atom}'
    ret=[]
    for entry in root.findall(atom+'entry'):
        title=(entry.findtext(atom+'title') or '').strip()
        ident=(entry.findtext(atom+'id') or title).strip()
        options=[]
        for link in entry.findall(atom+'link'):
            href=link.get('href','')
            rel=link.get('rel','')
            if 'acquisition/open-access' not in rel:
                continue
            url=_catalog_url(href)
            if not url:
                continue
            # Prefer an explicit direct .zim over a normalized .zim.meta4 link.
            direct=urllib.parse.urlsplit(href).path.lower().endswith('.zim')
            options.append((not direct,link,url))
        if not options:
            continue
        options.sort(key=lambda option:option[0])
        _,link,url=options[0]
        size=0
        # Prefer entry-level zimSize: a .meta4 link's 'length' is sometimes
        # just the size of the metalink file, not the archive itself.
        for node in entry.iter():
            key=node.tag.rsplit('}',1)[-1].lower()
            if key in ('zimsize','filesize') and node.text:
                with contextlib.suppress(ValueError,TypeError):
                    size=max(size,int(node.text.strip()))
        if not size:
            # Older catalog entries use the link 'length' for actual ZIM bytes.
            # Ignore suspiciously tiny values of normalized metalink links.
            with contextlib.suppress(ValueError,TypeError):
                link_bytes=int(link.get('length','0'))
                if link_bytes >= 1024*1024 or urllib.parse.urlsplit(link.get('href','')).path.lower().endswith('.zim'):
                    size=link_bytes
        ret.append({'id':ident,'title':title or Path(urllib.parse.urlsplit(url).path).name,'url':url,'size':size})
    return ret


def catalog(search='',limit=100,fetcher=None):
    # Catalog endpoint is paginated. Filter the entries after parsing, and
    # advance even when none of a particular page match a title search.
    items=[];seen=set(); start=0;count=100
    fetcher=fetcher or (lambda url:request(url,timeout=45).read())
    while len(items)<limit and start<10000:
        page_count=min(count,10000-start)
        url=CATALOG+'?'+urllib.parse.urlencode({'start':start,'count':page_count})
        data=fetcher(url)
        root=ET.fromstring(data)
        feed_count=len(root.findall('{http://www.w3.org/2005/Atom}entry'))
        found=parse_catalog(data)
        for item in found:
            if item['url'] not in seen:
                seen.add(item['url'])
                if not search or search.casefold() in item['title'].casefold():
                    items.append(item)
                    if len(items)>=limit:break
        start+=page_count
        if feed_count<page_count:break
    return items

def enqueue(items):
    """Update backup choices even for already installed ZIMs, without deleting copies."""
    with locked_queue():
        q=read_queue()
        old_by_url={x['url']:x for x in q}
        for item in items:
            safe_url(item['url'])
            requested=bool(item.get('backup',False))
            if item['url'] in old_by_url:
                old=old_by_url[item['url']]
                old['backup']=requested
                if requested and old.get('status')=='done':
                    # Revisit the already verified archive and sync to any new backup folder.
                    old['status']='downloaded'
                if not requested:old['backup_result']='excluded-by-user'
                continue
            row={**item,'backup':requested,'status':'pending','error':''}
            if not requested:row['backup_result']='excluded-by-user'
            q.append(row)
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

def _safe_backup_path(value, cfg, os_folder=False):
    """Validate a directory with no symlink traversal or archive/staging overlap.

    The path may not exist yet, but the nearest existing ancestor MUST exist and
    be on the OS pool if this is the OS-folder option. Creation is postponed
    until after the final YES confirmation.
    """
    path=Path(value).expanduser()
    if not path.is_absolute() or '..' in path.parts or str(path)=='/':
        raise ValueError('Enter an absolute dedicated backup folder, not /')
    if any(path==Path(x) or path.is_relative_to(Path(x)) for x in
           ('/proc','/sys','/dev','/run','/boot','/etc','/usr','/opt','/tmp','/var/tmp')):
        raise ValueError('Backup folder must not be inside a protected OS directory')
    for other in (cfg.get('zim_dir'),cfg.get('staging_dir')):
        if other:
            o=Path(other).resolve(strict=False)
            q=path.resolve(strict=False)
            if o==q or o.is_relative_to(q) or q.is_relative_to(o):
                raise ValueError('Backup folder cannot overlap the live ZIM library or staging area')
    # Refuse symlink redirection even in a parent directory.
    for component in (path,*path.parents):
        if component.is_symlink():raise ValueError('Symlinked backup paths are not supported')
    ancestor=path
    while not ancestor.exists():
        ancestor=ancestor.parent
    if not ancestor.is_dir():raise ValueError('Backup parent is not a directory')
    if path.exists() and not path.is_dir():raise ValueError('Backup path is not a directory')
    src=dataset_at(ancestor)
    if not src:raise ValueError('Backup parent storage is not mounted')
    if os_folder:
        root_source=dataset_at(Path('/'))
        if not root_source or _pool_identity(ancestor)!=_pool_identity(Path('/')):
            raise ValueError('This is not a folder on the OS filesystem/pool')
    return path,ancestor,src


def _backup_destination(cfg, input_fn=input):
    """Collect an optional backup destination without changing anything yet."""
    old=cfg.get('backup_dir','')
    print('\nBACKUP LOCATION (optional):')
    print('1) Keep current location: '+(old or 'none'))
    print('2) Choose a folder on the OS drive')
    print('3) Choose an existing folder on another mounted drive')
    print('4) Do not make ZIM backups')
    choice=input_fn('Select 1/2/3/4 (Enter = 1): ').strip() or '1'
    if choice=='4':return '', '', False
    if choice=='1':
        if not old:return '', '', False
        path,probe,src=_safe_backup_path(old,cfg)
        return str(path),src,_pool_identity(probe)==_pool_identity(Path('/'))
    if choice=='2':
        value=input_fn(f'OS backup folder (Enter = {DEFAULT_OS_BACKUP_DIR}): ').strip()
        path,_,src=_safe_backup_path(value or str(DEFAULT_OS_BACKUP_DIR),cfg,os_folder=True)
        print('WARNING: Same physical OS drive is NOT an independent backup!')
        print('It cannot protect against total disk failure, and may fill the OS disk.')
        return str(path),src,True
    if choice=='3':
        value=input_fn('Existing mounted backup directory (absolute path): ').strip()
        path,probe,src=_safe_backup_path(value,cfg)
        if not path.is_dir():raise ValueError('External destination must already exist and be mounted')
        independent=is_separate_physical_pool(Path(cfg['zim_dir']),path)
        if not independent:
            print('WARNING: This location is not independently verified as another physical drive.')
        return str(path),src,_pool_identity(probe)==_pool_identity(Path('/'))
    raise ValueError('Choose 1, 2, 3 or 4')


def _pool_identity(path):
    """ZFS datasets in the same pool share capacity; otherwise identify filesystem."""
    src=dataset_at(path)
    if src and not src.startswith('/') and ('/' in src or src in ('rpool','bpool','tank')):
        return 'zfs:'+src.split('/')[0]
    try:return 'device:'+str(Path(path).stat().st_dev)
    except OSError:return 'mount:'+src


def _backup_capacity(selected, backup_path, backup_probe, plan, archive, stage,
                     archive_free, stage_free, cfg):
    """Bytes available for ADDITIONAL backups after primary archive staging peak."""
    backup_pool=_pool_identity(backup_probe)
    root_pool=_pool_identity(Path('/'))
    is_os=backup_pool==root_pool
    reserve=OS_BACKUP_RESERVE if is_os else RESERVE
    if backup_pool==_pool_identity(archive):
        budget=archive_free-plan['required_final']-reserve
    elif backup_pool==_pool_identity(stage):
        budget=stage_free-plan['required_stage']-reserve
    else:
        budget=disk_free(backup_probe)-reserve
    copies=zfs_copies(backup_probe)
    requested=sum(int(x.get('size') or 0)*copies for x in selected)
    return {'available':max(0,budget),'need':requested,'copies':copies,
            'reserve':reserve,'shares_archive':backup_pool==_pool_identity(archive),
            'on_os':is_os}


def _parse_numbers(text, count):
    parts=[t.strip() for t in text.split(',')]
    if not parts or any(not t.isdecimal() for t in parts):
        raise ValueError('Use comma-separated numbers like 1,3,7')
    nums={int(t) for t in parts}
    if any(n<1 or n>count for n in nums):
        raise ValueError('Selected ZIM number is out of range')
    return nums


def _select_backups(chosen, cfg, backup_path, backup_src, plan, dst, stage,
                    avail, stage_free, input_fn=input):
    """Never silently exclude archives; user picks precisely which to copy."""
    chosen=[dict(x) for x in chosen]
    if not backup_path:
        print('Backup copies: none selected. Main read-only ZIMs will still download.')
        return chosen
    backup=Path(backup_path)
    _,probe,src=_safe_backup_path(backup,cfg)
    if src!=backup_src:raise ValueError('Backup filesystem changed while choosing')
    print('\nSELECT ZIM BACKUPS (originals stay downloaded regardless):')
    for i,x in enumerate(chosen,1):
        print(f' {i:>2}. {x["title"][:65]} ({size_text(int(x.get("size") or 0))})')
    print('Enter all, none, numbers like 1,3, or "exclude 2,5" to back up all EXCEPT 2 and 5.')
    count=len(chosen)
    raw=input_fn('Back up which ZIMs? (Enter = none): ').strip().lower()
    def initial(raw):
        if raw in ('none','', 'skip'):return set()
        if raw=='all':return set(range(1,count+1))
        if raw.startswith('exclude '):return set(range(1,count+1))-_parse_numbers(raw[8:],count)
        return _parse_numbers(raw,count)
    try:nums=initial(raw)
    except ValueError as e:
        print('Invalid ZIM backup selection:',e,'Nothing queued.')
        return None
    while True:
        selected=[chosen[i-1] for i in sorted(nums)]
        budget=_backup_capacity(selected,backup,probe,plan,dst,stage,avail,stage_free,cfg)
        if budget['need']<=budget['available']:
            print(f'Backup copies selected: {len(selected)}/{count}; estimated {size_text(budget["need"])}; available for backups {size_text(budget["available"])}.')
            break
        deficit=budget['need']-budget['available']
        print(f'INSUFFICIENT BACKUP SPACE: need {size_text(budget["need"])};')
        print(f'available for extra copies: {size_text(budget["available"])}; shortage {size_text(deficit)}.')
        if budget['shares_archive']:
            print('This folder shares space with the original ZIMs and temporary downloads.')
        print('Exclude some ZIM backup copies; original downloads are not removed.')
        decision=input_fn('Numbers to EXCLUDE from backups, "none" for no backups, or Enter to cancel: ').strip().lower()
        if decision=='':return None
        if decision=='none':nums=set();continue
        try:excluded=_parse_numbers(decision.replace('exclude ','',1),count)
        except ValueError as e:
            print('Invalid exclusion:',e);continue
        nums-=excluded
    for i,x in enumerate(chosen,1):x['backup']=i in nums
    if nums and budget['on_os']:
        print('IMPORTANT: OS-folder backup is on the system pool/drive; keep a separate physical copy for disaster recovery.')
    if len(nums)<count:
        print(f'{count-len(nums)} ZIM(s) excluded from backups; all remain in the original ZIM package.')
    print('Excluding a ZIM does not delete an older backup copy already on disk.')
    return chosen


def backup_zim(source,backupdir, cfg=None):
    if not backupdir:return 'none'
    cfg=cfg or config()
    backupdir=Path(backupdir)
    if not backupdir.is_dir():raise ValueError('Backup storage missing; will retry after reconnect')
    _,_,src=_safe_backup_path(backupdir,cfg)
    expected=cfg.get('backup_dataset','')
    if not expected or src!=expected:
        raise ValueError('Backup filesystem changed; refusing to write to an unknown disk')
    guard_path(backupdir,expected)
    target=backupdir/source.name
    srcsum=sha256(source)
    if target.exists() and sha256(target)==srcsum:return 'verified-existing'
    if target.exists():raise ValueError('Backup filename exists with incorrect checksum; refusing overwrite')
    reserve=OS_BACKUP_RESERVE if _pool_identity(backupdir)==_pool_identity(Path('/')) else RESERVE
    required=source.stat().st_size*zfs_copies(backupdir)+reserve
    if disk_free(backupdir)<required:
        raise ValueError(f'INSUFFICIENT BACKUP SPACE: need {size_text(required)}, available {size_text(disk_free(backupdir))}. Original preserved.')
    part=backupdir/(source.name+'.copying')
    if part.exists():
        raise ValueError('An unfinished .copying backup already exists; inspect it before retrying')
    with open(source,'rb') as a,open(part,'xb') as b:
        shutil.copyfileobj(a,b,4*CHUNK);b.flush();os.fsync(b.fileno())
    if sha256(part)!=srcsum:raise ValueError('Backup verification failed')
    os.replace(part,target)
    return 'verified-new'

def _queue_original(item, cfg):
    """Download/verify the original in its pinned location (or the single pool)."""
    from archive_guard import checked_archive, stage_dir, publish_verified, record_manifest
    from dual_storage import locations, archive_config
    locs=locations(cfg)
    key=item.get('storage', 'primary')
    if len(locs)==2 and key not in ('primary','secondary'):
        raise RuntimeError('Run sudo doomsday-zims optimize to assign this ZIM to a drive')
    if key=='secondary' and len(locs)!=2:
        raise RuntimeError('The enrolled secondary ZFS pool is unavailable; no fallback to OS drive')
    c=archive_config(cfg,key)
    dst,ds=checked_archive(c)
    stage=stage_dir(c)
    name=Path(urllib.parse.urlsplit(item['url']).path).name
    expected=(item.get('sha256') or fetch_checksum(item['url'])).lower()
    if not re.fullmatch('[0-9a-f]{64}',expected):raise ValueError('No trusted SHA-256')
    item['sha256']=expected
    # When two drives are active, detect an existing ORIGINAL on the other pool.
    # Never write a duplicate simply because placement metadata was changed.
    for loc in locs:
        other=loc['archive']/name
        if not other.is_file():continue
        if sha256(other)!=expected:raise ValueError('Existing ZIM has a checksum mismatch: '+str(other))
        if loc['key']!=key:
            raise ValueError('Archive already exists on '+loc['key']+' pool. Run optimize to pin it there; original preserved.')
        record_manifest(name,expected,other.stat().st_size,item['url'],ds)
        return other,False
    guard_path(dst,ds)
    guard_path(stage,c.get('staging_dataset',''))
    staged=download(item,stage)
    out=publish_verified(staged,expected,item['url'],cfg_override=c)
    staged.unlink(missing_ok=True)
    return out,True


def _backup_destination_for_item(item,cfg):
    """Choose an enrolled opposite-pool backup for dual mode, otherwise user folder."""
    if not item.get('backup'):return None,None
    if cfg.get('zim_dir_2'):
        from dual_storage import locations, pool_name
        current=item.get('storage','primary')
        opposite='secondary' if current=='primary' else 'primary'
        if item.get('backup_storage')!=opposite:
            raise ValueError('Run sudo doomsday-zims optimize: backup has no validated opposite-pool placement')
        field='dual_backup_secondary' if opposite=='secondary' else 'dual_backup_primary'
        directory=cfg.get(field+'_dir')
        dataset=cfg.get(field+'_dataset')
        if not directory or not dataset or pool_name(dataset)==pool_name(cfg['zim_dataset'+('_2' if current=='secondary' else '')]):
            raise ValueError('Opposite-pool backup destination unavailable')
        return directory,dataset
    return cfg.get('backup_dir'),cfg.get('backup_dataset')


def run_queue():
    """Download originals first, then backup selected ZIMs after staging is freed.

    Download and backup errors never delete the sole verified original. Every
    destination is revalidated before writing.  An interrupted backup stays in
    .copying and needs explicit inspection before retrying.
    """
    cfg=config()
    if not cfg.get('immutable_zims'):
        raise RuntimeError('Immutable archive not configured; run sudo ./scripts/prepare-zims.sh')
    from dual_storage import verify_locations
    verify_locations(cfg)
    downloaded=0
    with locked_queue():
        q=read_queue()
        for item in q:
            if item.get('status') in ('done','downloaded'):continue
            try:
                if cfg.get('zim_dir_2') and item.get('storage') not in ('primary','secondary'):
                    raise ValueError('Two-drive mode requires sudo doomsday-zims optimize before downloading')
                item['status']='downloading';atomic_json(queue_path(),q)
                out,new=_queue_original(item,cfg)
                if new:
                    downloaded+=1
                    # Publish completed archives to the offline browser promptly,
                    # without waiting for the entire hundreds-of-GB queue.
                    if Path('/run/systemd/system').exists():
                        subprocess.run(['systemctl','restart','--no-block','doomsday-kiwix.service'],
                                       check=False,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                item['status']='downloaded';item['error']=''
                atomic_json(queue_path(),q)
            except Exception as e:
                item['status']='retry';item['error']=str(e)[:600]
                print('Original pending retry:',item.get('title'),item['error'],file=sys.stderr)
                atomic_json(queue_path(),q)
        # Only make optional backups after ALL originals completed. This avoids
        # filling pools with redundant copies before huge staging files complete.
        originals_ready=all(x.get('status') in ('done','downloaded') for x in q)
        if not originals_ready:
            print('Some originals are incomplete. Deferring optional backups to protect space.')
        else:
            from dual_storage import archive_config
            for item in q:
                if item.get('status')!='downloaded':continue
                try:
                    if item.get('backup'):
                        bd,ds=_backup_destination_for_item(item,cfg)
                        if not bd:raise ValueError('No backup destination; select one with sudo doomsday-zims backups')
                        c=archive_config(cfg,item.get('storage','primary'))
                        archive=Path(c['zim_dir'])/Path(urllib.parse.urlsplit(item['url']).path).name
                        if not archive.is_file() or sha256(archive)!=item.get('sha256'):
                            raise ValueError('Original failed checksum verification before backing up')
                        from archive_guard import exact_dataset
                        if cfg.get('zim_dir_2') and exact_dataset(Path(bd))!=ds:
                            raise ValueError('Opposite-pool backup dataset unmounted; not copying to wrong disk')
                        bcfg={**cfg,'backup_dir':str(bd),'backup_dataset':ds}
                        item['backup_result']=backup_zim(archive,bd,bcfg)
                    else:item['backup_result']='excluded-by-user'
                    item['status']='done';item['error']=''
                except Exception as e:
                    # Still a valid downloaded original; do not re-download it.
                    item['backup_result']='deferred:'+str(e)[:240]
                    item['error']='BACKUP: '+str(e)[:500]
                    print('Optional backup deferred:',item.get('title'),item['error'],file=sys.stderr)
                atomic_json(queue_path(),q)

def load_preset(name, manifest_path=None):
    """Return exact archived collection entries, never an arbitrary entire OPDS catalog."""
    with open(manifest_path or BUNDLE_MANIFEST, encoding='utf-8') as f:
        data=json.load(f)
    if data.get('schema')!=1 or name not in ('full','partial','wikipedia'):
        raise ValueError('Unknown archive preset/manifest version')
    lookup={}
    for item in data['items']:
        safe_url(item['url'])
        if item['id']!=Path(urllib.parse.urlsplit(item['url']).path).name or item['id'] in lookup:
            raise ValueError('Invalid or duplicate preset archive filename')
        if not isinstance(item['size'],int) or item['size']<=0:
            raise ValueError('Preset archive size missing or invalid')
        if item.get('sha256') and not re.fullmatch('[0-9a-f]{64}',item['sha256']):
            raise ValueError('Invalid preset archive hash')
        lookup[item['id']]=item
    names=data['presets'][name]
    if not names or len(names)!=len(set(names)) or not all(n in lookup for n in names):
        raise ValueError('Incomplete preset archive list')
    chosen=[dict(lookup[n]) for n in names]
    if name=='partial' and any(n.startswith('ted_') for n in names):
        raise ValueError('Partial preset unexpectedly includes TED Talks')
    if name=='wikipedia' and names!=['wikipedia_en_all_maxi_2026-08.zim']:
        raise ValueError('Wikipedia-only preset must contain only English Wikipedia')
    return chosen


def interactive_wizard(input_fn=input, fetch=None):
    """Three one-key choices based on the original archived ZIM selection."""
    print('\nDOOMSDAY VAULT — Choose one download package')
    labels=[('1','full','FULL — everything in the original ZIM collection'),
            ('2','partial','PARTIAL — everything except TED Talks videos'),
            ('3','wikipedia','WIKIPEDIA ONLY — full English Wikipedia')]
    try:
        presets={key:load_preset(name) for key,name,_ in labels}
    except (OSError,ValueError,KeyError,TypeError) as e:
        print('Bundled download preset unavailable:',e)
        return []
    for key,name,label in labels:
        total=sum(x['size'] for x in presets[key])
        print(f'{key}) {label}: {total/1e9:,.1f} GB ({size_text(total)}), {len(presets[key])} ZIM(s)')
    print('Download sizes are from the original torrent metadata, NOT free-space requirements.')
    print('Press Enter to exit. For individual ZIM selection: sudo doomsday-zims browse')
    selected=input_fn('Select 1, 2, or 3: ').strip()
    if selected not in presets:
        print('No download package selected.')
        return []
    chosen=presets[selected]
    print('Selected:',next(label for key,name,label in labels if key==selected))
    print('Contents:')
    for index,item in enumerate(chosen,1):
        print(f'  {index:>2}. {item["title"]} — {item["size"]/1e9:.2f} GB')
    print('Existing ZIMs are not overwritten; queue resumes safely after interruption.')
    return queue_chosen(chosen,input_fn=input_fn,all_selected=True)


def interactive_catalog_wizard(input_fn=input, fetch=None):
    print('\nDoomsday ZIM Library — choose archives one by one or all from a filtered catalog.')
    print('Full Kiwix catalog may exceed available storage. No downloads start until you confirm.')
    term=input_fn('Search titles (e.g. Wikipedia, medical; Enter = browse first 100; ALL = browse): ').strip()
    if term.casefold()=='all':term=''  # Common expectation: all means browse, not search for 'all'.
    try:items=catalog(term,limit=100,fetcher=fetch)
    except Exception as e:
        print('Catalog unavailable:',e,'\nYou can rerun setup later when online.');return []
    if not items:
        print('No matching downloadable ZIMs. The live catalog returned no supported archive links.')
        print('Try a network check: curl -IL https://library.kiwix.org/catalog/v2/entries')
        return []
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
    return queue_chosen(chosen, input_fn=input_fn, all_selected=(sel=='all'))

def _storage_estimate(chosen, dst, stage, final_copies, staging_copies):
    """Estimate peak ZFS space while publishing one file at a time.

    A downloaded .zim is staged once, verified, copied to the immutable
    destination, then its staged copy is removed. Therefore the largest ZIM,
    not the entire catalog, determines temporary space.
    """
    payload=sum(int(x.get('size') or 0) for x in chosen)
    # Already published archives do not need downloading or restaging.
    # The worker still verifies their actual SHA-256 before trusting them.
    pending=[]
    already=0
    for x in chosen:
        filename=Path(urllib.parse.urlsplit(x.get('url','')).path).name
        installed=Path(dst)/filename
        try:
            exists=(installed.is_file() and installed.stat().st_size==int(x.get('size') or 0))
        except (OSError,ValueError,TypeError):exists=False
        if exists:already+=1
        else:pending.append(x)
    new_payload=sum(int(x.get('size') or 0) for x in pending)
    largest=max((int(x.get('size') or 0) for x in pending),default=0)
    final_bytes=new_payload*final_copies
    staging_bytes=largest*staging_copies
    dst_pool=dataset_at(dst).split('/')[0]
    stage_pool=dataset_at(stage).split('/')[0]
    same_pool=bool(dst_pool and dst_pool==stage_pool)
    required_final=final_bytes+(staging_bytes if same_pool else 0)+RESERVE
    required_stage=(staging_bytes+RESERVE) if not same_pool else required_final
    return {
        'payload':payload,'new_payload':new_payload,'already_present':already,
        'largest':largest,'final_bytes':final_bytes,
        'staging_bytes':staging_bytes,'same_pool':same_pool,
        'required_final':required_final,'required_stage':required_stage,
    }


def _enable_single_copy_for_zims(cfg):
    """Only after explicit typed consent; NEVER change system/boot datasets.

    ZFS copies=1 affects future writes. Existing blocks keep their old copies,
    and ZFS snapshots / an external backup remain the user's responsibility.
    """
    if os.geteuid()!=0:
        raise RuntimeError('Changing ZFS copies requires root: sudo doomsday-zims wizard')
    from archive_guard import checked_archive, stage_dir
    _,archive_ds=checked_archive(cfg)
    stage_dir(cfg)
    staging_ds=cfg['staging_dataset']
    root_ds=dataset_at(Path('/'))
    boot_ds=dataset_at(Path('/boot'))
    targets=list(dict.fromkeys([archive_ds,staging_ds]))
    for ds in targets:
        if ('/' not in ds or ds in (root_ds,boot_ds) or
                ds.endswith('/ROOT') or '/ROOT/' in ds or '/BOOT/' in ds):
            raise RuntimeError('Refusing to modify copies on an OS, boot or pool-root dataset: '+ds)
    before={}
    for ds in targets:
        result=subprocess.run(['zfs','get','-H','-o','value','copies',ds],capture_output=True,text=True)
        if result.returncode or result.stdout.strip() not in ('1','2','3'):
            raise RuntimeError('Unable to verify current ZFS copies for '+ds)
        before[ds]=result.stdout.strip()
    changed=[]
    try:
        for ds,old in before.items():
            if old=='1':continue
            result=subprocess.run(['zfs','set','copies=1',ds],capture_output=True,text=True)
            if result.returncode:
                raise RuntimeError('ZFS rejected copies=1 for '+ds+': '+result.stderr.strip())
            changed.append(ds)
    except Exception:
        # Best-effort restore of the previous setting if a later dataset fails.
        for ds in reversed(changed):
            subprocess.run(['zfs','set','copies='+before[ds],ds],capture_output=True,text=True)
        raise
    print('Space-saving mode enabled for future ZIM/archive staging writes only.')
    print('Existing ZIM blocks are not rewritten. OS/boot copies are unchanged.')


def queue_chosen(chosen, input_fn=input, all_selected=False):
    if not chosen:return []
    cfg=config();dst=Path(cfg['zim_dir']);stage=Path(cfg.get('staging_dir',cfg['zim_dir']))
    avail=disk_free(dst);stage_free=disk_free(stage)
    factor=zfs_copies(dst);stage_copies=zfs_copies(stage)
    unknown=[x for x in chosen if not x.get('size')]
    plan=_storage_estimate(chosen,dst,stage,factor,stage_copies)
    print(f'Actual download size: {plan["payload"]/1e9:,.1f} GB ({size_text(plan["payload"])}).')
    if plan['already_present']:
        print(f'Already present: {plan["already_present"]} ZIM(s); new downloads estimated: {size_text(plan["new_payload"])}.')
    print(f'ZFS archive copies={factor}, staging copies={stage_copies}.')
    print(f'Final ZIM storage estimate: {size_text(plan["final_bytes"])};')
    print(f'Largest temporary staged ZIM: {size_text(plan["staging_bytes"])}.')
    if plan['same_pool']:
        print(f'Estimated peak on the same pool including {size_text(RESERVE)} reserve: {size_text(plan["required_final"])}; free: {size_text(avail)}.')
    else:
        print(f'Archive pool needs {size_text(plan["required_final"])} (free {size_text(avail)}); staging pool needs {size_text(plan["required_stage"])} (free {size_text(stage_free)}).')
    if unknown:
        print('Unknown ZIM sizes: cannot safely select all until sizes are determined.')
        if all_selected:return []
    insufficient=plan['required_final']>avail or plan['required_stage']>stage_free
    lower_copies=False
    if insufficient:
        if plan['required_final']>avail:
            print(f'INSUFFICIENT SPACE FOR ORIGINAL ZIMS: need {size_text(plan["required_final"])},'
                  f' free {size_text(avail)}, shortage {size_text(plan["required_final"]-avail)}.')
        if plan['required_stage']>stage_free and not plan['same_pool']:
            print(f'INSUFFICIENT TEMPORARY STAGING SPACE: need {size_text(plan["required_stage"])},'
                  f' free {size_text(stage_free)}.')
        # The two-copy preservation default can make the 575 GB bundle require
        # about 1.6 TB peak. Offer a one-copy option, never an implicit change.
        efficient=_storage_estimate(chosen,dst,stage,1,1)
        eligible=bool(cfg.get('zim_dataset') and cfg.get('staging_dataset'))
        would_fit=(efficient['required_final']<=avail and efficient['required_stage']<=stage_free)
        if eligible and would_fit and (factor>1 or stage_copies>1) and not unknown:
            print(f'OPTIONAL SPACE-SAVING MODE: peak would be ~{size_text(efficient["required_final"])} on archive pool')
            if not efficient['same_pool']:
                print(f'and ~{size_text(efficient["required_stage"])} on staging pool.')
            print('This changes only the dedicated ZIM and staging datasets to copies=1 for FUTURE WRITES.')
            print('It REDUCES protection against damaged blocks. Keep a verified independent backup.')
            consent=input_fn('Type SINGLE COPY to opt in, or Enter to cancel: ').strip()
            if consent!='SINGLE COPY':
                print('Preserving copies settings. Nothing queued.')
                return []
            lower_copies=True
            factor=stage_copies=1
            plan=efficient
        else:
            print('Insufficient free space even with safe/available storage choices. Nothing queued.')
            return []
    # Backup copies are optional and never interfere with the original archive.
    # Destination selection and exclusions are staged until the final YES.
    try:
        bdir,backup_src,backup_on_os=_backup_destination(cfg,input_fn)
    except (OSError,ValueError) as e:
        print('Invalid backup location:',e,'Nothing queued.');return []
    try:
        selected=_select_backups(chosen,cfg,bdir,backup_src,plan,dst,stage,avail,stage_free,input_fn)
    except (OSError,ValueError) as e:
        print('Cannot plan ZIM backups:',e,'Nothing queued.');return []
    if selected is None:return []
    chosen=selected
    confirm=input_fn('Queue these '+str(len(chosen))+' archive(s) for SHA-verified resumable download? type YES: ')
    if confirm!='YES':
        print('Nothing changed or queued.')
        return []
    if lower_copies:
        # Apply immediately before enqueue, after BOTH consent prompts.
        _enable_single_copy_for_zims(cfg)
        updated=_storage_estimate(chosen,dst,stage,zfs_copies(dst),zfs_copies(stage))
        if updated['required_final']>disk_free(dst) or updated['required_stage']>disk_free(stage):
            print('Capacity changed or ZFS property not applied; nothing queued.')
            return []
    if bdir:
        try:
            p,probe,source=_safe_backup_path(bdir,cfg,os_folder=backup_on_os)
            if source!=backup_src:raise ValueError('Selected backup mount changed')
            if not p.exists():
                p.mkdir(parents=True,mode=0o700)
            checked,_,live_src=_safe_backup_path(p,cfg,os_folder=backup_on_os)
            if live_src!=backup_src:raise ValueError('Selected backup location changed after creation')
            cfg['backup_dir']=str(checked)
            cfg['backup_dataset']=live_src
        except (OSError,ValueError) as e:
            print('Failed to prepare backup folder:',e,'Nothing queued.');return []
    else:
        cfg['backup_dir']=''
        cfg['backup_dataset']=''
    # Independent auto-repair is unsafe if a user changes to OS-folder backups.
    if (backup_on_os or not bdir or not is_separate_physical_pool(dst,Path(bdir))):
        cfg['auto_restore_verified_zims']=False
    atomic_json(CONF,cfg)
    enqueue(chosen)
    print('Queued. Automatic timer resumes downloads after power interruption.')
    return chosen

def _queue_as_items():
    queue=read_queue()
    if not queue:
        print('The ZIM queue is empty. Use sudo doomsday-zims wizard first.')
    return queue


def select_existing_backups(input_fn=input):
    """User can modify backup choices after the original downloads are queued."""
    cfg=config()
    q=_queue_as_items()
    if not q:return []
    for i,item in enumerate(q,1):
        print(f'{i:>2}. {item.get("title",item["url"])} - {size_text(item.get("size") or 0)} - {item.get("status")}; backup={"yes" if item.get("backup") else "no"}')
    raw=input_fn('Choose backups: all, none, 29,34, or exclude 13,33 (Enter cancels): ').strip().lower()
    if not raw:return []
    count=len(q)
    try:
        if raw=='all':selected=set(range(1,count+1))
        elif raw=='none':selected=set()
        elif raw.startswith('exclude '):selected=set(range(1,count+1))-_parse_numbers(raw[8:],count)
        else:selected=_parse_numbers(raw,count)
    except ValueError as e:
        print('Invalid selection:',e);return []
    if cfg.get('zim_dir_2'):
        # Selection is validated together with original placements below.
        if any(x.get('storage') not in ('primary','secondary') for x in q):
            print('Run sudo doomsday-zims optimize to assign originals first.')
            return []
        return optimize_two_drives(input_fn=input_fn,selected_urls={q[i-1]['url'] for i in selected})
    print('Choose backup destination on OS or mounted external drive:')
    bdir,src,on_os=_backup_destination(cfg,input_fn)
    chosen=[{**x,'backup':i in selected} for i,x in enumerate(q,1)]
    if not bdir and selected:
        print('No backup destination selected; original downloads are unchanged.')
        return []
    if bdir:
        # Post-original backups are made AFTER the remaining original downloads,
        # not simultaneously with the largest staging allocation.
        dst=Path(cfg['zim_dir']);stage=Path(cfg['staging_dir'])
        plan=_storage_estimate(q,dst,stage,zfs_copies(dst),zfs_copies(stage))
        backup_plan={**plan,'required_final':plan['final_bytes']}
        # The existing UI accepts the user's selection. For post-queue changes
        # only use its capacity calculation; do not prompt for a different list.
        selected_items=[x for x in chosen if x['backup']]
        path=Path(bdir);_,probe,_=_safe_backup_path(path,cfg,os_folder=on_os)
        cap=_backup_capacity(selected_items,path,probe,backup_plan,dst,stage,
                             disk_free(dst),disk_free(stage),cfg)
        if cap['need']>cap['available']:
            print(f'INSUFFICIENT BACKUP SPACE: need {size_text(cap["need"])}, available {size_text(cap["available"])}; shortage {size_text(cap["need"]-cap["available"])}.')
            print('Choose fewer backup numbers and rerun. No changes made.')
            return []
        print(f'{len(selected_items)} backup(s) fit the estimated post-download budget ({size_text(cap["available"])} available).')
    if input_fn('Type YES to save these backup choices: ').strip()!='YES':
        print('Cancelled; no changes made.');return []
    if bdir:
        path,_,source=_safe_backup_path(bdir,cfg,os_folder=on_os)
        path.mkdir(parents=True,exist_ok=True)
        from archive_guard import exact_dataset
        if dataset_at(path)!=source:raise RuntimeError('Backup mount changed after selection')
        cfg['backup_dir']=str(path);cfg['backup_dataset']=source
    else:cfg['backup_dir']='';cfg['backup_dataset']=''
    if on_os or not bdir or not is_separate_physical_pool(Path(cfg['zim_dir']),Path(bdir)):
        cfg['auto_restore_verified_zims']=False
    with locked_queue():
        by_url={x['url']:x for x in read_queue()}
        for item in chosen:
            old=by_url[item['url']]
            old['backup']=item['backup']
            if item['backup'] and old['status']=='done':old['status']='downloaded'
            if not item['backup'] and old['status']=='downloaded':old['status']='done'
            if not item['backup']:old['backup_result']='excluded-by-user'
        atomic_json(CONF,cfg)
        atomic_json(queue_path(),list(by_url.values()))
    print('Backup choices saved. The next download run handles any selected copies.')
    return chosen


def optimize_two_drives(input_fn=input, selected_urls=None):
    """User-reviewed fit plan, retaining existing files and partial downloads."""
    from dual_storage import verify_locations, plan_originals, backup_plan
    cfg=config()
    if not cfg.get('zim_dir_2'):
        print('No second ZFS pool configured. Run sudo doomsday-zims dual-setup first.')
        return []
    locs=verify_locations(cfg)
    q=_queue_as_items()
    if not q:return []
    free={loc['key']:disk_free(loc['archive']) for loc in locs}
    copies={loc['key']:(zfs_copies(loc['archive']),zfs_copies(loc['stage'])) for loc in locs}
    part_locks={}
    for item in q:
        name=Path(urllib.parse.urlsplit(item['url']).path).name
        for loc in locs:
            if (loc['stage']/(name+'.part')).exists() or (loc['stage']/name).exists():
                if item['url'] in part_locks:raise RuntimeError('Partial download exists on both pools; inspect before planning')
                part_locks[item['url']]=loc['key']
        # If a previous placement was pinned, never move its .part even if it has no file yet.
    try:
        placement,budgets=plan_originals(q,cfg,free,copies,existing_parts=part_locks)
        selected=selected_urls if selected_urls is not None else {x['url'] for x in q if x.get('backup')}
        from archive_guard import exact_dataset
        for k in ('primary','secondary'):
            path=Path(cfg['dual_backup_'+k+'_dir'])
            required=cfg['dual_backup_'+k+'_dataset']
            if exact_dataset(path)!=required:
                raise ValueError('Dedicated backup dataset is not mounted: '+str(path))
        bc={k:zfs_copies(Path(cfg['dual_backup_'+k+'_dir'])) for k in ('primary','secondary')}
        bplacement,costs=backup_plan(q,placement,budgets,free,bc,selected)
    except (OSError,ValueError) as e:
        print('Cannot fit the requested ZIMs / backups across both pools:',e)
        print('Originals and backup settings have not been changed.')
        return []
    print('TWO DRIVE FIT PLAN — downloaded originals are not moved:')
    for loc in locs:
        key=loc['key'];b=budgets[key]
        n=sum(1 for k in placement.values() if k==key)
        print(f'{key}: {loc["archive_ds"]}: {n} originals, new originals {size_text(b["used"])},'
              f' backup {size_text(costs[key])}, staging peak {size_text(b["stage_peak"])},'
              f' reserve {size_text(b["reserve"])}, free {size_text(b["free"])}')
    print(f'Cross-pool backup selection: {len(bplacement)} ZIM(s). No automatic reformat or migration.')
    if input_fn('Type APPLY PLAN to save assignment and backup choices: ').strip()!='APPLY PLAN':
        print('No changes made.');return []
    with locked_queue():
        current=read_queue()
        # If another queue entry arrived meanwhile, refuse rather than plan stale list.
        if {x['url'] for x in current}!={x['url'] for x in q}:
            raise RuntimeError('ZIM queue changed. Rerun optimization before saving.')
        for item in current:
            key=item['url'];item['storage']=placement[key]
            item['backup']=key in selected
            item['backup_storage']=bplacement.get(key)
            if item['backup'] and item.get('status')=='done':item['status']='downloaded'
            if not item['backup'] and item.get('status')=='downloaded':item['status']='done'
        atomic_json(queue_path(),current)
    print('Optimization saved. Start/resume with sudo systemctl start --no-block doomsday-zim-download.service')
    return current


def dual_setup(input_fn=input):
    from dual_storage import create_dedicated,pool_list,pool_name
    cfg=config()
    try:
        current=pool_name(cfg['zim_dataset'])
        pools=[p for p in pool_list() if p!=current]
    except (ValueError,RuntimeError) as e:
        print('ZFS pool discovery failed:',e);return False
    print('Primary pool:',current,'Other imported ZFS pools:',', '.join(pools) or '(none)')
    if not pools:
        print('Connect/import a separate prepared ZFS pool first; DoomsdayOS will not partition or format it.')
        return False
    chosen=input_fn('Existing ZFS pool to use for the second archive (exact name, Enter cancels): ').strip()
    if not chosen:return False
    try:
        success=create_dedicated(cfg,chosen,input_fn=input_fn)
        if success:
            atomic_json(CONF,cfg)
            subprocess.run(['systemctl','try-restart','doomsday-zim-protect.service'],check=False)
            print('Two-drive archive enrolled; now run sudo doomsday-zims optimize')
        return success
    except (OSError,ValueError,RuntimeError,subprocess.CalledProcessError) as e:
        print('No dual drive configuration saved:',e)
        return False


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['wizard','browse','run','status','enqueue-url','backups','dual-setup','optimize'])
    p.add_argument('--url');p.add_argument('--sha256');p.add_argument('--size',type=int,default=0)
    a=p.parse_args()
    if a.command=='wizard':interactive_wizard()
    elif a.command=='browse':interactive_catalog_wizard()
    elif a.command=='backups':select_existing_backups()
    elif a.command=='dual-setup':dual_setup()
    elif a.command=='optimize':optimize_two_drives()
    elif a.command=='run':run_queue()
    elif a.command=='status':
        cfg=config()
        print('Backup destination:',cfg.get('backup_dir') or ('Opposite ZFS pool (selected ZIMs)' if cfg.get('zim_dir_2') else 'none'))
        for x in read_queue():
            print(x['status'],x['title'],
                  'drive='+x.get('storage','primary'),
                  'backup='+('selected' if x.get('backup') else 'excluded'),
                  x.get('backup_result',''),x.get('error',''))
    elif a.command=='enqueue-url':
        if not a.url or not a.sha256:p.error('--url and --sha256 required')
        enqueue([{'id':a.url,'title':Path(urllib.parse.urlsplit(a.url).path).name,'url':a.url,
                 'size':a.size,'sha256':a.sha256,'backup':False}])
if __name__=='__main__':main()
