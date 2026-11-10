#!/usr/bin/env python3
"""Explicit import of a user-selected, single-file .zim torrent with SHA-256.

Trust is never inferred from the torrent's SHA-1 piece hashes. The publisher
or user must supply an independently trusted 64-digit SHA-256 digest.
"""
from __future__ import annotations
import hashlib,re,shutil,subprocess
from pathlib import Path
import urllib.parse

MAX_META=4*1024*1024
GIB=1024**3


def _bdecode(data:bytes):
    """Restricted bencode parser, bounded by MAX_META and nesting depth."""
    if len(data)>MAX_META:raise ValueError('Torrent metadata file too large')
    def parse(pos,depth=0):
        if depth>24 or pos>=len(data):raise ValueError('Invalid bencode')
        t=data[pos:pos+1]
        if t==b'i':
            j=data.find(b'e',pos+1)
            if j<0:raise ValueError('Unterminated integer')
            raw=data[pos+1:j]
            if not re.fullmatch(rb'(0|-[1-9][0-9]*|[1-9][0-9]*)',raw):raise ValueError('Malformed integer')
            return int(raw),j+1
        if t==b'l':
            out=[];pos+=1
            while data[pos:pos+1]!=b'e':
                x,pos=parse(pos,depth+1);out.append(x)
                if len(out)>10000:raise ValueError('Torrent too complex')
            return out,pos+1
        if t==b'd':
            out={};pos+=1
            while data[pos:pos+1]!=b'e':
                key,pos=parse(pos,depth+1)
                if not isinstance(key,bytes) or key in out:raise ValueError('Malformed torrent dictionary')
                out[key],pos=parse(pos,depth+1)
                if len(out)>10000:raise ValueError('Torrent too complex')
            return out,pos+1
        if b'0'<=t<=b'9':
            sep=data.find(b':',pos)
            if sep<0 or sep-pos>14:raise ValueError('Malformed string')
            raw=data[pos:sep]
            if not re.fullmatch(rb'(0|[1-9][0-9]*)',raw):raise ValueError('Malformed byte length')
            length=int(raw);start=sep+1;end=start+length
            if end>len(data):raise ValueError('Truncated string')
            return data[start:end],end
        raise ValueError('Bad bencode token')
    root,end=parse(0)
    if end!=len(data) or not isinstance(root,dict):raise ValueError('Invalid torrent root')
    return root


def torrent_info(path):
    f=Path(path).resolve(strict=True)
    if not f.is_file() or f.suffix.lower()!='.torrent' or f.stat().st_size>MAX_META:
        raise ValueError('Expected a local .torrent metadata file (max 4 MiB)')
    root=_bdecode(f.read_bytes())
    info=root.get(b'info')
    if not isinstance(info,dict) or b'files' in info:raise ValueError('Only single-file ZIM torrents are supported; multi-file torrents are rejected')
    try:name=info[b'name.utf-8'] if b'name.utf-8' in info else info[b'name'];name=name.decode('utf-8','strict')
    except (KeyError,UnicodeError,AttributeError) as e:raise ValueError('Invalid torrent filename') from e
    if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*\.zim',name) or
            name in ('.','..') or '..' in name):raise ValueError('Unsafe torrent filename')
    length=info.get(b'length',0)
    piece_size=info.get(b'piece length',0)
    pieces=info.get(b'pieces')
    if (type(length) is not int or not 0<length<4*1024**4 or type(piece_size) is not int or
            piece_size<=0 or piece_size>64*1024**2 or not isinstance(pieces,bytes) or
            len(pieces)!=20*((length+piece_size-1)//piece_size)):
        raise ValueError('Invalid single-file torrent piece metadata')
    return {'name':name,'size':length,'torrent':str(f)}


def run_torrent_import(path,sha256,storage='primary',cfg=None,runner=subprocess.run):
    if not re.fullmatch(r'[0-9a-fA-F]{64}',sha256 or ''):
        raise ValueError('Explicit independently trusted SHA-256 is required')
    expected=sha256.lower()
    info=torrent_info(path)
    import zim_manager
    from dual_storage import archive_config
    from archive_guard import checked_archive,stage_dir,publish_verified,sha256 as file_sha
    cfg=cfg or zim_manager.config()
    if storage not in ('primary','secondary'):raise ValueError('Invalid storage choice')
    targetcfg=archive_config(cfg,storage)
    archive,archive_ds=checked_archive(targetcfg)
    stage=stage_dir(targetcfg)
    final=archive/info['name'];staged=stage/info['name']
    if final.is_file():
        if file_sha(final)==expected:return final
        raise ValueError('A different archive with this filename already exists; leaving it untouched')
    if staged.is_symlink() or (staged.exists() and not staged.is_file()):
        raise ValueError('Unsafe staged path; no files were changed')
    a_factor=zim_manager.zfs_copies(archive);s_factor=zim_manager.zfs_copies(stage)
    same_pool=archive_ds.split('/')[0]==targetcfg['staging_dataset'].split('/')[0]
    needed_archive=info['size']*a_factor+3*GIB+(info['size']*s_factor if same_pool and not staged.exists() else 0)
    needed_stage=info['size']*s_factor+3*GIB
    if (zim_manager.disk_free(archive)<needed_archive or
            (not same_pool and zim_manager.disk_free(stage)<needed_stage)):
        raise ValueError('INSUFFICIENT SPACE for original plus temporary torrent staging; nothing started')
    if not staged.exists() or file_sha(staged)!=expected:
        exe=shutil.which('aria2c')
        if not exe:raise RuntimeError('Install aria2c to import a custom torrent: sudo apt install aria2')
        args=[exe,'--dir='+str(stage),'--seed-time=0','--continue=true',
              '--file-allocation=none','--auto-file-renaming=false','--allow-overwrite=false',
              '--enable-rpc=false','--max-connection-per-server=2',info['torrent']]
        runner(args,check=True)
    if not staged.is_file() or staged.is_symlink():raise RuntimeError('Torrent did not produce a safe ZIM file')
    if staged.stat().st_size!=info['size'] or file_sha(staged)!=expected:
        raise RuntimeError('Downloaded torrent did not match expected size/SHA-256; retained in staging for investigation')
    out=publish_verified(staged,expected,'user-torrent:'+Path(path).name,cfg_override=targetcfg)
    staged.unlink(missing_ok=True)
    subprocess.run(['systemctl','try-restart','doomsday-kiwix.service'],check=False,capture_output=True)
    return out


def _batch_entries(batch_file):
    """Explicit local JSON manifest; never fetch torrents from arbitrary URLs."""
    import json
    src=Path(batch_file).resolve(strict=True)
    if not src.is_file() or src.stat().st_size>128*1024:
        raise ValueError('Use a local JSON batch manifest under 128 KiB')
    entries=json.loads(src.read_text(encoding='utf-8'))
    if not isinstance(entries,list) or not 1<=len(entries)<=40:
        raise ValueError('Provide 1 to 40 torrent entries')
    validated=[];seen=set()
    for row in entries:
        if not isinstance(row,dict):raise ValueError('Each torrent must be an object')
        checksum=row.get('sha256','')
        if not isinstance(checksum,str) or not re.fullmatch('[0-9a-fA-F]{64}',checksum):
            raise ValueError('Every torrent needs independently trusted SHA-256')
        storage=row.get('storage','primary')
        if storage not in ('primary','secondary'):raise ValueError('Unsupported storage pool choice')
        file=Path(row['torrent_file'])
        if not file.is_absolute():file=src.parent/file
        info=torrent_info(file)
        if info['name'] in seen:raise ValueError('Duplicate filename in batch: '+info['name'])
        seen.add(info['name'])
        validated.append({**info,'sha256':checksum.lower(),'storage':storage})
    return validated


def run_torrent_batch(batch_file, max_parallel=4, *, cfg=None,
                      runner=subprocess.run, clock=None, record_speed=True):
    """Download several SHA-pinned ZIM torrents without overloading ZFS.

    Groups are bound to their already-mounted staging datasets. aria2 applies
    a GLOBAL download cap across simultaneous torrents on that dataset. After
    each wave, historical per-file transfer rate adjusts next-wave parallelism.
    No original is published until its entire SHA-256 and byte size match.
    """
    import time
    from collections import defaultdict
    import zim_manager as zm
    from archive_guard import checked_archive, stage_dir, publish_verified, sha256 as file_sha
    from dual_storage import archive_config
    from transfer_optimizer import (get_budget, torrent_parallelism,
                                    read_policy, record_torrent_speed, dataset_identity)
    exe=shutil.which('aria2c')
    if not exe:raise RuntimeError('aria2c unavailable: sudo apt install aria2')
    cfg=cfg or zm.config()
    rows=_batch_entries(batch_file)
    chosen=defaultdict(list)
    for row in rows:chosen[row['storage']].append(row)
    done=[]
    if clock is None:clock=time.monotonic
    # Do not interfere with the running HTTP downloader or drive optimizer.
    with zm.exclusive_download_worker():
        for storage,group in chosen.items():
            local=archive_config(cfg,storage)
            archive,_=checked_archive(local)
            stage=stage_dir(local)
            a_copies=zm.zfs_copies(archive)
            s_copies=zm.zfs_copies(stage)
            same_pool=zm._pool_identity(archive)==zm._pool_identity(stage)
            budget=get_budget(stage,auto=True)
            policy=read_policy()['datasets'].get(dataset_identity(stage),{})
            historic=float(policy.get('torrent_mib_s',0))
            running=[]
            for row in group:
                archive_file=archive/row['name'];staged=stage/row['name']
                if archive_file.exists():
                    if not archive_file.is_file() or file_sha(archive_file)!=row['sha256']:
                        raise ValueError('Existing original has a wrong checksum: '+row['name'])
                    done.append(archive_file)
                    continue
                if staged.is_symlink() or staged.exists() and not staged.is_file():
                    raise ValueError('Unsafe staged torrent file: '+row['name'])
                running.append(row)
            # Each wave checks allocated space before it starts. Multiple
            # .part files and final publications can coexist temporarily.
            while running:
                parallel=torrent_parallelism(budget,historic,max_parallel)
                # Don't start simultaneous large ZIMs if their temporary
                # staging would exceed capacity; downgrade to fewer torrents.
                # Already-verified originals stay untouched throughout.
                while True:
                    wave=running[:parallel]
                    total=sum(x['size'] for x in wave)
                    final_storage=sum(x['size']*a_copies for x in wave)
                    temp_storage=sum(x['size']*s_copies for x in wave)
                    reserve=zm.RESERVE
                    if same_pool:
                        fits=zm.disk_free(archive)>=final_storage+temp_storage+reserve
                    else:
                        fits=(zm.disk_free(archive)>=final_storage+reserve and
                              zm.disk_free(stage)>=temp_storage+reserve)
                    if fits:break
                    if parallel==1:
                        raise ValueError('INSUFFICIENT ZFS SPACE for one torrent, including original, staging and reserve')
                    parallel-=1
                running=running[len(wave):]
                # Preserve partially completed torrents. aria2 knows resume
                # state but we NEVER trust it as a final content checksum.
                args=[exe,'--dir='+str(stage),'--seed-time=0','--continue=true',
                      '--file-allocation=none','--auto-file-renaming=false',
                      '--allow-overwrite=false','--enable-rpc=false',
                      '--max-concurrent-downloads='+str(parallel),
                      '--max-overall-download-limit='+str(budget)+'M',
                      '--max-connection-per-server=2',
                      *[x['torrent'] for x in wave]]
                began=clock()
                result=runner(args,check=False)
                elapsed=max(0.001,clock()-began)
                if result.returncode:
                    raise RuntimeError('Torrent wave interrupted; staged files retained for safe resume')
                for row in wave:
                    staged=stage/row['name']
                    if not staged.is_file() or staged.is_symlink() or staged.stat().st_size!=row['size']:
                        raise RuntimeError('Torrent incomplete or wrong size; retained in staging: '+row['name'])
                    if file_sha(staged)!=row['sha256']:
                        raise RuntimeError('Torrent SHA-256 mismatch; retained in staging: '+row['name'])
                    out=publish_verified(staged,row['sha256'],'user-torrent:'+Path(row['torrent']).name,
                                         cfg_override=local)
                    staged.unlink(missing_ok=True)
                    done.append(out)
                # Estimate peer speed from the wave's total transferred bytes;
                # adapt number of active torrents at the next wave.
                historic=(total/(1024**2))/elapsed/max(1,len(wave))
                if record_speed:
                    record_torrent_speed(stage,historic)
                print(f'Verified {len(wave)} torrent(s); measured ~{historic:.2f} MiB/s each; '
                      f'next wave uses disk budget {budget} MiB/s.',flush=True)
    if done and Path('/run/systemd/system').exists():
        subprocess.run(['systemctl','try-restart','doomsday-kiwix.service'],check=False,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    return done


def run_torrent_pack(directory, max_parallel=4, *, cfg=None,
                     manifest=None, **kwargs):
    """Batch the user's original 39 Kiwix torrents, using pinned SHA-256 only.

    Missing SHA-256 (e.g. Python DevDocs in the old ZIP) is skipped rather
    than blindly trusting torrent piece hashes. Queue placement is retained.
    """
    import json
    import tempfile
    import zim_manager as zm
    directory=Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError('Extract your original Zim.zip; provide its existing Zim/ directory')
    ref=manifest or zm.BUNDLE_MANIFEST
    data=json.loads(Path(ref).read_text(encoding='utf-8'))
    originals={x['id']:x for x in data['items']}
    queue={Path(urllib.parse.urlsplit(x['url']).path).name:x
           for x in zm.read_queue()}
    torrents=sorted(directory.glob('*.torrent'))
    if not torrents:raise ValueError('No .torrent files in selected folder')
    batch=[];skipped=[]
    for file in torrents:
        if file.is_symlink():raise ValueError('Symlinked torrent file not allowed')
        info=torrent_info(file)
        row=originals.get(info['name'])
        if not row:
            skipped.append(info['name']+' (not in original pinned manifest)')
            continue
        if int(row['size'])!=info['size']:
            raise ValueError('Torrent size differs from pinned manifest: '+info['name'])
        if not re.fullmatch('[0-9a-f]{64}',row.get('sha256','')):
            skipped.append(info['name']+' (publisher SHA-256 missing; use direct-check)')
            continue
        position=queue.get(info['name'],{})
        batch.append({'torrent_file':str(file.resolve()),'sha256':row['sha256'],
                      'storage':position.get('storage','primary')})
    for message in skipped:print('SKIPPED:',message)
    if not batch:raise ValueError('No SHA-pinned torrent files available for safe batch import')
    # Sensitive local state in /var/lib, rather than adding transient .json to Downloads.
    zm.BASE.mkdir(parents=True,mode=0o700,exist_ok=True)
    fd,path=tempfile.mkstemp(prefix='torrent-pack-',suffix='.json',dir=zm.BASE)
    try:
        import os
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as out:json.dump(batch,out)
        print('Safe torrent files in batch:',len(batch))
        return run_torrent_batch(path,max_parallel=max_parallel,cfg=cfg,**kwargs)
    finally:Path(path).unlink(missing_ok=True)
