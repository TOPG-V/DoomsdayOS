#!/usr/bin/env python3
"""Immutable ZFS ZIM library manager.

Long-term archive policy:
  * An archive always lives on a dedicated ZFS filesystem, never on / or tank itself.
  * .zim.part files reside in a separate writable staging filesystem.
  * Publish only after SHA-256 matches a previously trusted publisher checksum.
  * A short, root-locked publishing window temporarily disables readonly.
  * A service restores readonly=on after power loss during publishing.
  * Integrity checks never automatically destroy the only copy of a damaged file.
"""
from __future__ import annotations
import argparse, contextlib, datetime as dt, fcntl, hashlib, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

BASE = Path(os.environ.get('DOOMSDAY_STATE_DIR', '/var/lib/doomsday-vault'))
CONF = Path(os.environ.get('DOOMSDAY_CONFIG', '/etc/doomsday-vault/firstboot.json'))
LOCK = Path(os.environ.get('DOOMSDAY_ARCHIVE_LOCK', '/run/lock/doomsday-zim-archive.lock'))


def run(*argv):
    return subprocess.run(list(map(str, argv)), text=True, capture_output=True, check=False)


def must(*argv):
    out=run(*argv)
    if out.returncode:
        raise RuntimeError('Command failed: '+ ' '.join(map(str,argv)) + ': '+ (out.stderr or out.stdout).strip())
    return out.stdout.strip()


def sha256(path:Path):
    h=hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda:f.read(4*1024*1024),b''): h.update(block)
    return h.hexdigest()


def current_config():
    return json.loads(CONF.read_text()) if CONF.exists() else {}


def exact_dataset(path:Path):
    # An exact mount-point is crucial: applying readonly to a parent filesystem
    # such as rpool/ROOT would make the entire OS unwriteable.
    p=run('findmnt','-M',str(path),'-n','-o','SOURCE,FSTYPE')
    if p.returncode: return ''
    bits=p.stdout.split()
    return bits[0] if len(bits)==2 and bits[1]=='zfs' else ''


def checked_archive(cfg):
    target=Path(cfg['zim_dir'])
    ds=cfg.get('zim_dataset','')
    if not target.is_dir() or not ds or not exact_dataset(target)==ds:
        raise RuntimeError('Dedicated ZIM ZFS dataset missing; refusing to alter other filesystems')
    if ds in ('rpool','bpool','tank') or '/' not in ds or ds.endswith('/ROOT') or '/ROOT/' in ds or '/BOOT/' in ds:
        raise RuntimeError('Refusing read-only on a pool root or system dataset')
    root_ds=run('findmnt','-n','-o','SOURCE','/').stdout.strip()
    if ds==root_ds or ds==run('findmnt','-n','-o','SOURCE','/boot').stdout.strip():
        raise RuntimeError('Refusing to set the running system or boot filesystem readonly')
    return target, ds


def stage_dir(cfg):
    p=Path(cfg['staging_dir'])
    ds=cfg.get('staging_dataset','')
    if not p.is_dir() or not ds or exact_dataset(p)!=ds:
        raise RuntimeError('Writable ZIM staging dataset not mounted; refusing to use a fallback path')
    if p == Path(cfg['zim_dir']):raise RuntimeError('Staging and archive directories cannot be identical')
    return p


@contextlib.contextmanager
def archive_lock():
    LOCK.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with LOCK.open('a+') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def set_ro(ds, value:bool):
    must('zfs','set','readonly='+('on' if value else 'off'),ds)


def protect():
    cfg=current_config()
    if not cfg.get('immutable_zims'): return 'Archive protection not configured (run wizard)'
    with archive_lock():
        _,ds=checked_archive(cfg)
        set_ro(ds, True)
        return 'Verified archive dataset now read-only: '+ds


def record_manifest(filename, checksum, size, source_url):
    BASE.mkdir(parents=True,exist_ok=True)
    path=BASE/'zim-manifest.json'
    try: data=json.loads(path.read_text())
    except (OSError,ValueError):data={}
    data[filename]={'sha256':checksum,'bytes':size,'source_url':source_url,'verified_at':dt.datetime.now(dt.timezone.utc).isoformat()}
    fd,tmp=tempfile.mkstemp(dir=BASE,prefix='.zim-manifest.')
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f:
            json.dump(data,f,indent=2,sort_keys=True)
            f.write('\n');f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)


def publish_verified(stage_file:Path, expected_sha:str, url:str=''):
    cfg=current_config()
    dst,ds=checked_archive(cfg)
    st=stage_dir(cfg)
    stage_file=Path(stage_file)
    if stage_file.parent.resolve()!=st.resolve() or not stage_file.name.endswith('.zim'):
        raise RuntimeError('Only verified .zim files from the enrolled staging dataset are allowed')
    if not re.fullmatch('[0-9a-f]{64}', expected_sha):raise ValueError('Missing trusted SHA-256 value')
    if sha256(stage_file)!=expected_sha:raise RuntimeError('Staged archive checksum mismatch')
    final=dst/stage_file.name
    with archive_lock():
        if final.exists():
            if sha256(final)==expected_sha: return final
            raise RuntimeError('Archive filename collision: refusing to overwrite existing original')
        # Do not enable writes until staging+destination are identified and space is checked.
        # Both source and destination occupy space during the copy.
        free=shutil.disk_usage(dst).free
        copies=must('zfs','get','-H','-o','value','copies',ds)
        multiplier=int(copies) if copies in ('1','2','3') else 1
        reserve=int(cfg.get('min_free_gib',3))*1024**3
        if free < stage_file.stat().st_size * multiplier+reserve:
            raise RuntimeError('Insufficient destination space for verified publishing')
        tmp=dst/('.publishing-'+stage_file.name)
        if tmp.exists(): raise RuntimeError('Unfinished prior publication found; inspect it manually')
        set_ro(ds,False)
        published=False
        try:
            with stage_file.open('rb') as src, tmp.open('xb') as out:
                shutil.copyfileobj(src,out,4*1024*1024)
                out.flush();os.fsync(out.fileno())
            if sha256(tmp)!=expected_sha: raise RuntimeError('Published bytes failed secondary verification')
            os.chmod(tmp,0o444)
            os.replace(tmp,final)
            dirfd=os.open(dst,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(dirfd)
            finally:os.close(dirfd)
            published=True
        finally:
            # Never leave the entire archive writable after an interrupted copy.
            set_ro(ds,True)
        if published:
            record_manifest(final.name,expected_sha,final.stat().st_size,url)
            # Snapshot the verified archive state (not a replacement for another drive).
            snap='zim-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            result=run('zfs','snapshot',ds+'@'+snap)
            if result.returncode: print('Warning: snapshot skipped:',result.stderr.strip(),file=sys.stderr)
            return final


def restore_from_enrolled_backup(name, expected):
    """Optional automatic repair of ONE ZIM when the independent backup hashes correctly.

    Never rolls back Ubuntu or overwrites unknown files. Keeps the damaged original.
    """
    cfg=current_config()
    archive, ds = checked_archive(cfg)
    bd=cfg.get('backup_dir','')
    backup_ds=cfg.get('backup_dataset','')
    if not bd or not backup_ds:
        raise RuntimeError('No independently enrolled ZIM backup available')
    bp=Path(bd)
    if exact_dataset(bp)!=backup_ds or backup_ds.split('/')[0]==ds.split('/')[0]:
        raise RuntimeError('Backup must be a different mounted ZFS pool; refusing unsafe restore')
    if Path(name).name!=name or not name.endswith('.zim'):
        raise RuntimeError('Unsafe archive name')
    backup=bp/name
    if not backup.is_file() or sha256(backup)!=expected:
        raise RuntimeError('Independent backup is missing or does not match trusted SHA-256')
    target=archive/name
    # Never alter an existing verified copy.
    if target.is_file() and sha256(target)==expected: return target
    if shutil.disk_usage(archive).free < (backup.stat().st_size * 3 + 3*1024**3):
        raise RuntimeError('Not enough space for non-destructive replacement and quarantine')
    with archive_lock():
        set_ro(ds,False)
        try:
            staged=archive/('.recovering-'+name)
            if staged.exists():raise RuntimeError('Previous recovery staging exists; inspect manually')
            with backup.open('rb') as src, staged.open('xb') as out:
                shutil.copyfileobj(src,out,4*1024*1024)
                out.flush();os.fsync(out.fileno())
            if sha256(staged)!=expected:raise RuntimeError('Recovered copy failed verification')
            os.chmod(staged,0o444)
            if target.exists():
                quarantine=archive/('.corrupt-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+name)
                if quarantine.exists():raise RuntimeError('Quarantine collision')
                os.replace(target,quarantine)
            os.replace(staged,target)
            dirfd=os.open(archive,os.O_RDONLY|os.O_DIRECTORY)
            try:os.fsync(dirfd)
            finally:os.close(dirfd)
        finally:
            set_ro(ds,True)
    print('Repaired from verified independent backup:',name)
    return target


def verify_archives():
    cfg=current_config()
    dst,ds=checked_archive(cfg)
    records=BASE/'zim-manifest.json'
    if not records.exists():
        print('No trusted ZIM SHA-256 manifest yet; nothing authenticated for verification')
        return 0
    data=json.loads(records.read_text())
    bad=0
    for name,entry in sorted(data.items()):
        if Path(name).name!=name or not name.endswith('.zim'):continue
        target=dst/name
        try: valid=target.is_file() and sha256(target)==entry['sha256']
        except OSError:valid=False
        if not valid:
            if cfg.get('auto_restore_verified_zims',False):
                try:
                    restore_from_enrolled_backup(name,entry['sha256'])
                    continue
                except (RuntimeError,OSError) as e:
                    print('Restore unavailable:',e,file=sys.stderr)
            print('CORRUPT OR MISSING:',target,'— manual restore from verified backup required')
            bad+=1
        else:print('OK:',name)
    return bad


def status():
    cfg=current_config()
    if not cfg.get('immutable_zims'):
        print('Immutable ZIM dataset: not configured');return
    target,ds=checked_archive(cfg)
    print('Archive dataset:',ds)
    print('Mount:',target)
    print('Read-only:',must('zfs','get','-H','-o','value','readonly',ds))
    print('Copies:',must('zfs','get','-H','-o','value','copies',ds))
    print('Staging:',stage_dir(cfg))


def main():
    p=argparse.ArgumentParser()
    p.add_argument('operation',choices=['protect','verify','status'])
    args=p.parse_args()
    if args.operation=='protect':print(protect())
    elif args.operation=='status':status()
    elif args.operation=='verify':
        if verify_archives():sys.exit(2)

if __name__=='__main__':main()
