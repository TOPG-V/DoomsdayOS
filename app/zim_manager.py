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
    """Normalize official .zim.meta4 OPDS references to the corresponding .zim."""
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
            direct=urllib.parse.urlsplit(href).path.lower().endswith('.zim')
            options.append((not direct,link,url))
        if not options:
            continue
        options.sort(key=lambda option:option[0])
        _,link,url=options[0]
        size=0
        for node in entry.iter():
            key=node.tag.rsplit('}',1)[-1].lower()
            if key in ('zimsize','filesize') and node.text:
                with contextlib.suppress(ValueError,TypeError):
                    size=max(size,int(node.text.strip()))
        if not size:
            with contextlib.suppress(ValueError,TypeError):
                link_bytes=int(link.get('length','0'))
                if link_bytes >= 1024*1024 or urllib.parse.urlsplit(link.get('href','')).path.lower().endswith('.zim'):
                    size=link_bytes
        ret.append({'id':ident,'title':title or Path(urllib.parse.urlsplit(url).path).name,'url':url,'size':size})
    return ret


def catalog(search='',limit=100,fetcher=None):
    # Advance across pages even if a given page has no title matches.
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
    if term.casefold()=='all':term=''
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
    largest=max((int(x.get('size') or 0) for x in chosen),default=0)
    final_bytes=payload*final_copies
    staging_bytes=largest*staging_copies
    dst_pool=dataset_at(dst).split('/')[0]
    stage_pool=dataset_at(stage).split('/')[0]
    same_pool=bool(dst_pool and dst_pool==stage_pool)
    required_final=final_bytes+(staging_bytes if same_pool else 0)+RESERVE
    required_stage=(staging_bytes+RESERVE) if not same_pool else required_final
    return {
        'payload':payload,'largest':largest,'final_bytes':final_bytes,
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
    bdir=cfg.get('backup_dir')
    if bdir:
        bd=Path(bdir);bfree=disk_free(bd);bf=zfs_copies(bd)
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
    enqueue(chosen)
    print('Queued. Automatic timer resumes downloads after power interruption.')
    return chosen

def main():
    p=argparse.ArgumentParser();p.add_argument('command',choices=['wizard','browse','run','status','enqueue-url']);p.add_argument('--url');p.add_argument('--sha256');p.add_argument('--size',type=int,default=0)
    a=p.parse_args()
    if a.command=='wizard':interactive_wizard()
    elif a.command=='browse':interactive_catalog_wizard()
    elif a.command=='run':run_queue()
    elif a.command=='status':
        for x in read_queue():print(x['status'],x['title'],x.get('error',''))
    elif a.command=='enqueue-url':
        if not a.url or not a.sha256: p.error('--url and --sha256 required')
        enqueue([{'id':a.url,'title':Path(urllib.parse.urlsplit(a.url).path).name,'url':a.url,'size':a.size,'sha256':a.sha256,'backup':False}])
if __name__=='__main__':main()
