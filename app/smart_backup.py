#!/usr/bin/env python3
"""Disk-aware, portable Doomsday Vault backup planner and verified file copier.

Uses ONLY previously enrolled exact ZFS dataset mounts as automatic destinations.
Never formats/imports disks, changes boot records, follows symlinks, or rolls
back the live OS. Full system image replication stays in stream-replica.sh.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

GIB = 1024 ** 3
STATE = Path(os.environ.get('DOOMSDAY_STATE_DIR', '/var/lib/doomsday-vault'))
POLICY = Path(os.environ.get('DOOMSDAY_SMART_BACKUP_POLICY','/etc/doomsday-vault/smart-backup.json'))
FIRSTBOOT = Path(os.environ.get('DOOMSDAY_CONFIG','/etc/doomsday-vault/firstboot.json'))
RECOVERY_ENV = Path(os.environ.get('DOOMSDAY_RECOVERY_ENV','/etc/doomsday-vault/recovery.env'))
FLEET = Path(os.environ.get('DOOMSDAY_FLEET_STATE',str(STATE/'fleet.json')))
DEFAULTS = {'enabled': True, 'sources': [str(STATE/'zimlens'),str(STATE/'research'),'/etc/doomsday-vault'],
            'os_reserve_gib':20, 'external_reserve_gib':5,'minimum_external_free_percent':10,
            'max_source_gib':50, 'max_files':100000, 'keep_generations':3, 'interval_days':14,
            'auto_restore_missing': False}
MANIFEST = 'backup-manifest.json'
MARKER = '.doomsday-smart-backup'


def _run(*cmd, timeout=15):
    try:
        p = subprocess.run(list(cmd),capture_output=True,text=True,timeout=timeout,check=False)
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f'Command unavailable: {cmd[0]}') from exc
    if p.returncode:raise RuntimeError(f'{cmd[0]} failed: {(p.stderr or p.stdout).strip()[:200]}')
    return p.stdout.strip()


def _read_json(path,default):
    try:return json.loads(Path(path).read_text(encoding='utf-8'))
    except FileNotFoundError:return default


def settings():
    raw = _read_json(POLICY,{})
    if not isinstance(raw,dict):raise ValueError('Smart backup policy must be an object')
    config={**DEFAULTS, **raw}
    if type(config['enabled']) is not bool:raise ValueError('enabled must be boolean')
    if type(config['auto_restore_missing']) is not bool:raise ValueError('auto_restore_missing must be boolean')
    if not isinstance(config['sources'],list) or len(config['sources'])>32:raise ValueError('At most 32 explicit sources')
    for path in config['sources']:
        if not isinstance(path,str) or not Path(path).is_absolute() or path=='/' or '..' in Path(path).parts:
            raise ValueError('Sources must be absolute, nonroot paths')
    for key,lo,hi in [('os_reserve_gib',10,2048),('external_reserve_gib',1,2048),('minimum_external_free_percent',0,50),
                      ('max_source_gib',1,4096),('max_files',1,2000000),('keep_generations',1,10),('interval_days',1,365)]:
        value=config[key]
        if type(value) is not int or not lo<=value<=hi:raise ValueError(f'Invalid {key}')
    return config


def _cmd_ok(*cmd):
    try:return _run(*cmd)
    except RuntimeError:return ''


def root_pool():
    root=_cmd_ok('findmnt','-n','-o','SOURCE','/')
    return root.split('/')[0] if '/' in root and not root.startswith('/dev/') else ''


def _guid(pool):
    value=_run('zpool','get','-H','-o','value','guid',pool)
    if not re.fullmatch(r'\d+',value):raise RuntimeError('Pool GUID is not numeric')
    return value


def _mount_identity(dataset,path):
    """Require an exact ZFS filesystem mount, not a directory on its parent pool."""
    if not path or not dataset or '/' not in dataset or not Path(path).is_dir() or Path(path).is_symlink():return False
    try:source,fstype=_run('findmnt','-M',str(path),'-n','-o','SOURCE,FSTYPE').split()
    except (RuntimeError,ValueError):return False
    return source==dataset and fstype=='zfs'


def _zpool_members(pool):
    """Physical parent devices extracted from imported ZFS topology, fail closed."""
    text=_cmd_ok('zpool','status','-P',pool)
    devices=set();unknown=False
    if not text:return set()
    in_config=False
    for line in text.splitlines():
        if line.lstrip().startswith('config:'):in_config=True;continue
        if not in_config:continue
        if line.lstrip().startswith('errors:'):break
        bits=line.strip().split()
        if not bits or not bits[0].startswith('/dev/'):
            continue
        path=os.path.realpath(bits[0])
        ancestor=_cmd_ok('lsblk','-dn','-o','PKNAME',path)
        if ancestor and not ancestor.startswith('/dev/'):
            ancestor='/dev/'+ancestor
        if not ancestor:
            # Whole-disk vdev, not a partition.
            device_type=_cmd_ok('lsblk','-dn','-o','TYPE',path)
            if device_type=='disk':ancestor=path
            else:unknown=True;continue
        # Resolve nested partitions / NVMe mapper to top-level physical disk.
        for _ in range(5):
            upper=_cmd_ok('lsblk','-dn','-o','PKNAME',ancestor)
            if not upper:break
            ancestor='/dev/'+upper.removeprefix('/dev/')
        devices.add(os.path.realpath(ancestor))
    if unknown:return set()
    return devices


def disks():
    """Read-only list of block disks including disconnected and external media."""
    try:data=json.loads(_run('lsblk','--json','--bytes','-o','NAME,PATH,TYPE,SIZE,RO,RM,TRAN,MODEL,SERIAL,WWN,FSTYPE,MOUNTPOINTS'))
    except (RuntimeError,ValueError):return []
    out=[]
    for d in data.get('blockdevices',[]):
        if d.get('type') not in ('disk','loop'):continue
        out.append({'path':d.get('path') or '/dev/'+str(d.get('name')),'kind':d.get('type'),
                    'bytes':int(d.get('size') or 0),'transport':d.get('tran') or 'unknown',
                    'model':str(d.get('model') or '').strip()[:100],
                    'read_only':bool(int(d.get('ro') or 0)),'removable':bool(int(d.get('rm') or 0)),
                    'has_stable_id':bool(d.get('wwn') or d.get('serial'))})
    return out


def _env():
    if not RECOVERY_ENV.exists():return {}
    result={}
    for line in RECOVERY_ENV.read_text().splitlines():
        hit=re.fullmatch(r'([A-Z_][A-Z0-9_]*)=(.*)',line.strip())
        if hit:result[hit[1]]=hit[2].strip().strip('"\'')
    return result


def _enrolled():
    """Previously approved backup datasets only; never new/untrusted disks."""
    rows=[]
    env=_env()
    if env.get('EXTERNAL_DATASET') and env.get('EXTERNAL_DIR') and env.get('EXTERNAL_GUID'):
        rows.append({'key':'recovery','dataset':env['EXTERNAL_DATASET'],'mount':env['EXTERNAL_DIR'],
                     'expected_guid':env['EXTERNAL_GUID']})
    fleet=_read_json(FLEET,[])
    if isinstance(fleet,list):
        for index,row in enumerate(fleet):
            if isinstance(row,dict) and row.get('backup_dataset') and row.get('backup_dir'):
                rows.append({'key':row.get('key','fleet'+str(index)),'dataset':row['backup_dataset'],
                             'mount':row['backup_dir'],'expected_guid':row.get('pool_guid')})
    cfg=_read_json(FIRSTBOOT,{})
    if isinstance(cfg,dict):
        for label,key,path in [('primary','backup_dataset','backup_dir'),('secondary','dual_backup_secondary_dataset','dual_backup_secondary_dir'),
                               ('fleet-primary','dual_backup_primary_dataset','dual_backup_primary_dir')]:
            if cfg.get(key) and cfg.get(path):
                rows.append({'key':label,'dataset':cfg[key],'mount':cfg[path],'expected_guid':cfg.get(label+'_pool_guid')})
    distinct=[];seen=set()
    for row in rows:
        token=(row['dataset'],row['mount'])
        if token not in seen:distinct.append(row);seen.add(token)
    return distinct[:50]


def destinations(root=None,policy=None):
    policy=policy or settings()
    root=root if root is not None else root_pool()
    os_phys=_zpool_members(root) if root else set()
    result=[]
    for row in _enrolled():
        ds=str(row['dataset']);pool=ds.split('/')[0]
        view={**row,'pool':pool,'eligible':False,'reason':'', 'available_bytes':0,'capacity_bytes':0}
        if not re.fullmatch(r'[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+',ds) or ds.startswith(('rpool/ROOT/','bpool/BOOT/')):
            view['reason']='Invalid/sensitive backup dataset'
        elif not _mount_identity(ds,row['mount']):
            view['reason']='Enrolled dataset not mounted at its exact path'
        elif pool==root:
            view['reason']='Same root ZFS pool: local copy only; not independent'
        else:
            try:
                guid=_guid(pool)
                if row.get('expected_guid') and guid != str(row['expected_guid']):
                    view['reason']='Pool GUID changed; refused'
                else:
                    phys=_zpool_members(pool)
                    if not os_phys or not phys:
                        view['reason']='Physical device identity unverified'
                    elif not os_phys.isdisjoint(phys):
                        view['reason']='Shares a physical disk with OS pool'
                    elif _cmd_ok('zfs','get','-H','-o','value','readonly',ds)!='off':
                        view['reason']='Backup dataset is not writable'
                    else:
                        available=int(_run('zfs','list','-Hp','-o','available',ds))
                        reserve=max(policy['external_reserve_gib']*GIB,
                                    (available*policy['minimum_external_free_percent'])//100)
                        view.update({'eligible':available>reserve,'available_bytes':available,
                                     'capacity_bytes':max(0,available-reserve),
                                     'reason':'Eligible independent enrolled ZFS dataset' if available>reserve else 'No free space after reserve',
                                     'guid':guid})
            except (RuntimeError,ValueError):view['reason']='Unable to validate imported pool or free space'
        result.append(view)
    return result


def _source_files(sources,limit_files,limit_bytes):
    """Bounded, deterministic files; symlinks and special files are never copied."""
    results=[];total=0;warnings=[]
    for idx,raw in enumerate(sources):
        parent=Path(raw)
        if not parent.exists():
            warnings.append('Missing source: '+raw);continue
        if parent.is_symlink():
            warnings.append('Symlinked source refused: '+raw);continue
        paths=[parent] if parent.is_file() else []
        if parent.is_dir():
            for root,dirs,files in os.walk(parent,topdown=True,followlinks=False):
                dirs[:]=sorted(d for d in dirs if not (Path(root)/d).is_symlink() and not (Path(root)/d).is_mount())
                paths.extend(Path(root)/name for name in sorted(files))
        for f in paths:
            if f.is_symlink():continue
            try:
                info=f.lstat()
                if not stat.S_ISREG(info.st_mode):continue
            except OSError:continue
            if not parent.is_file():
                relative=f.relative_to(parent).as_posix()
            else:relative=parent.name
            total+=info.st_size
            if len(results)>=limit_files or total>limit_bytes:
                raise ValueError('Selected backup sources exceed safe file-count/size limits')
            results.append({'original':str(f),'relative':f'{idx}/{relative}',
                            'size':info.st_size,'mtime_ns':info.st_mtime_ns})
    return results,total,warnings


def estimate(policy=None):
    policy=policy or settings()
    records,total,warnings=_source_files(policy['sources'],policy['max_files'],policy['max_source_gib']*GIB)
    return {'files':len(records),'bytes':total,'required_bytes':total+max(64*1024*1024,total//100),
            'sources':policy['sources'],'warnings':warnings}


def system_capacity():
    """Conservative OS-pool snapshot and external ZFS replication capacity report.

    Allocated bytes is a worst-case proxy for full stream size, not an exact
    estimate of compressed ZFS sends or future incremental changes.
    """
    root=root_pool()
    if not root:return {'zfs_os':False,'reason':'OS root not on ZFS; no automatic ZFS system clone'}
    try:
        parts=_run('zpool','list','-Hp','-o','size,free,alloc,health',root).split()
        size,free,allocated=map(int,parts[:3])
        state=parts[3] if len(parts)>3 else 'unknown'
    except (RuntimeError,ValueError,IndexError):
        return {'zfs_os':True,'pool':root,'reason':'Cannot read root ZFS pool capacity'}
    cfg=settings();reserved=max(cfg['os_reserve_gib']*GIB,size//10)
    full_estimate=int(allocated*1.15)+256*1024**2
    external=[d for d in destinations(root,cfg) if d['key']=='recovery']
    enough=[d for d in external if d['eligible'] and d['capacity_bytes']>=full_estimate]
    return {'zfs_os':True,'pool':root,'health':state,'size_bytes':size,'free_bytes':free,
            'used_bytes':allocated,'local_backup_eligible':free>=reserved,
            'local_safe_reserve_bytes':reserved,'full_stream_upper_bound_bytes':full_estimate,
            'external_system_replica_capacity_ok':bool(enough),
            'external_pool_enrolled':bool(external),
            'note':'First full stream may compress/deduplicate differently. Existing incremental streams may need far less space.'}


def _local_status():
    """Local OS recovery status is evidence-based; no pretend 'full backup'."""
    root=root_pool()
    results={'root_pool':root or None,'snapshots':0,'local_job_success_marker':False,
             'external_system_snapshots':0,'external_system_replica_detected':False,
             'note':'Local ZFS snapshots are on the OS disk and do not survive disk failure. Success markers are not proof of restore.'}
    if root:
        output=_cmd_ok('zfs','list','-Hp','-t','snapshot','-o','name','-r',root)
        results['snapshots']=len([x for x in output.splitlines() if '@' in x])
    for path in (STATE/'last-success-local',STATE/'recovery-mark.json'):
        if path.exists():results['local_job_success_marker']=True
    recovery=_env()
    ds=recovery.get('EXTERNAL_DATASET','')
    mount=recovery.get('EXTERNAL_DIR','')
    guid=recovery.get('EXTERNAL_GUID','')
    if ds and mount and guid and _mount_identity(ds,mount) and _cmd_ok('zpool','get','-H','-o','value','guid',ds.split('/')[0])==guid:
        reps=ds+'/dv-replicas'
        out=_cmd_ok('zfs','list','-Hp','-t','snapshot','-o','name','-r',reps)
        count=sum(1 for x in out.splitlines() if '@dvstream_' in x)
        results['external_system_snapshots']=count
        results['external_system_replica_detected']=count>0
    return results


def plan():
    cfg=settings();estimate_ = estimate(cfg);dest=destinations(policy=cfg)
    ready=sorted([d for d in dest if d['eligible'] and d['capacity_bytes']>=estimate_['required_bytes']],
                 key=lambda d:(d['capacity_bytes']-estimate_['required_bytes'],d['dataset']))
    chosen=ready[0] if ready else None
    return {'enabled':cfg['enabled'],'local_system':_local_status(), 'system_capacity':system_capacity(), 'estimate':estimate_,
            'connected_disks':disks(),'destinations':dest,
            'recommended':{k:chosen[k] for k in ('dataset','mount','capacity_bytes','guid')} if chosen else None,
            'external_copy_possible':bool(chosen),
            'limitations':['Full OS ZFS streams use the separately enrolled recovery destination, not this data-copy job.',
                           'No automatic full OS/EFI rollback or forced pool import.',
                           'No new backup disk is enrolled or formatted automatically.']}


def _safe_tree(path,base):
    """Refuse symlinks anywhere below a prevalidated dataset root."""
    b=Path(base);p=Path(path)
    if p!=b and b not in p.parents:raise ValueError('Destination escapes enrolled dataset')
    while p!=b:
        if p.is_symlink():raise ValueError('Symlink in backup destination')
        p=p.parent
    if b.is_symlink():raise ValueError('Symlinked dataset mount')


def _machine_key():
    try:machine=Path('/etc/machine-id').read_text().strip()
    except OSError:machine='installation'
    return hashlib.sha256(machine.encode()).hexdigest()[:16]


def _copy_verified(src,target,expected_size):
    h=hashlib.sha256();size=0
    fd=os.open(src,os.O_RDONLY | getattr(os,'O_NOFOLLOW',0) | getattr(os,'O_NONBLOCK',0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Source is not a regular file')
        with os.fdopen(fd,'rb') as read, open(target,'xb') as output:
            os.chmod(target,0o600)
            while chunk:=read.read(1024*1024):
                output.write(chunk);h.update(chunk);size+=len(chunk)
            output.flush();os.fsync(output.fileno())
    except BaseException:
        Path(target).unlink(missing_ok=True)
        raise
    if size!=expected_size:
        Path(target).unlink(missing_ok=True)
        raise RuntimeError('Source changed during copy; refusing incomplete backup')
    return h.hexdigest()


def _write_json(path,obj):
    p=Path(path);tmp=p.parent/('.manifest-'+uuid.uuid4().hex)
    try:
        with open(tmp,'x',encoding='utf-8') as out:
            os.chmod(tmp,0o600);json.dump(obj,out,indent=2,sort_keys=True)
            out.write('\n');out.flush();os.fsync(out.fileno())
        os.replace(tmp,p)
    finally:tmp.unlink(missing_ok=True)


def _backup_root(destination):
    return Path(destination['mount'])/'.doomsday-smart-backups'/_machine_key()


def _verify_generation(folder):
    p=Path(folder)
    if not p.is_dir() or p.is_symlink() or not (p/MARKER).is_file():
        raise ValueError('Not a completed Doomsday smart backup')
    data=json.loads((p/MANIFEST).read_text(encoding='utf-8'))
    if data.get('format')!=1 or not isinstance(data.get('records'),list):raise ValueError('Invalid manifest format')
    results=[]
    for row in data['records']:
        rel=row.get('relative')
        if not isinstance(rel,str) or not rel or '..' in Path(rel).parts or Path(rel).is_absolute():
            raise ValueError('Unsafe backup path')
        src=p/'files'/rel
        if any(x.is_symlink() for x in (src,*list(src.parents)[:len(src.parents)-len(p.parents)])) or not src.is_file():
            results.append({'file':row.get('original'),'ok':False,'error':'Missing/symlink file'});continue
        h=hashlib.sha256()
        with src.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        good=h.hexdigest()==row.get('sha256') and src.stat().st_size==row.get('size')
        results.append({'file':row.get('original'),'ok':good})
    return {'generation':p.name,'files':len(results),'valid':all(x['ok'] for x in results),
            'bad':[x for x in results if not x['ok']], 'data':data}


def _lock():
    import fcntl
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    fd=os.open(STATE/'smart-backup.lock',os.O_CREAT|os.O_RDWR,0o600)
    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
    return fd


def perform_backup():
    if os.geteuid()!=0:raise PermissionError('Run as root')
    policy=settings()
    if not policy['enabled']:return {'status':'disabled'}
    fdesc=_lock()
    try:
        report=plan()
        recommended=report['recommended']
        if not recommended:return {'status':'deferred','reason':'No independently enrolled mounted backup dataset with sufficient space','plan':report}
        d=next(x for x in destinations(policy=policy) if x['dataset']==recommended['dataset'] and x['eligible'])
        files,total,warnings=_source_files(policy['sources'],policy['max_files'],policy['max_source_gib']*GIB)
        if not files:return {'status':'deferred','reason':'No readable files in configured source directories'}
        required=total+max(64*1024*1024,total//100)
        if required>d['capacity_bytes'] or not _mount_identity(d['dataset'],d['mount']) or _guid(d['pool'])!=d['guid']:
            raise RuntimeError('Destination identity or capacity changed before starting')
        base=_backup_root(d);_safe_tree(base,d['mount'])
        base.mkdir(parents=True,exist_ok=True,mode=0o700)
        stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+uuid.uuid4().hex[:8]
        folder=base/(stamp+'.pending');folder.mkdir(mode=0o700)
        manifest={'format':1,'created_at':dt.datetime.now(dt.timezone.utc).isoformat(),
                  'source_pool':root_pool(),'target_pool':d['pool'],'target_guid':d['guid'],
                  'target_dataset':d['dataset'],'records':[],'warnings':warnings}
        try:
            for item in files:
                src=Path(item['original']);dst=folder/'files'/item['relative'];dst.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                _safe_tree(dst,d['mount'])
                check=src.stat()
                if check.st_size!=item['size'] or check.st_mtime_ns!=item['mtime_ns']:
                    raise RuntimeError('Source changed during backup: '+str(src))
                checksum=_copy_verified(src,dst,item['size'])
                after=src.stat()
                if after.st_size!=item['size'] or after.st_mtime_ns!=item['mtime_ns']:
                    raise RuntimeError('Source modified while copying: '+str(src))
                manifest['records'].append({**item,'sha256':checksum})
            _write_json(folder/MANIFEST,manifest)
            (folder/MARKER).write_text('format 1\n',encoding='utf-8')
            os.chmod(folder/MARKER,0o600)
            verified=_verify_generation(folder)
            if not verified['valid']:raise RuntimeError('Backup verification failed')
            complete=base/stamp
            folder.rename(complete)
            # Never delete a generation here. Capacity/retention warnings can
            # guide a separately confirmed cleanup without erasing the last copy.
            from backup_policy import mark
            mark('smart-copy')
            return {'status':'ok','path':str(complete),'files':len(files),'bytes':total,
                    'target_dataset':d['dataset'],'verified':True}
        except BaseException:
            # Only self-created incomplete folders are removed.
            if folder.exists() and folder.name.endswith('.pending'):shutil.rmtree(folder)
            raise
    finally:os.close(fdesc)


def list_generations():
    items=[]
    for row in destinations():
        if not row['eligible']:continue
        folder=_backup_root(row)
        if not folder.is_dir() or folder.is_symlink():continue
        for path in sorted(folder.iterdir(),reverse=True):
            if path.is_dir() and not path.is_symlink() and (path/MARKER).is_file():
                items.append({'path':str(path),'dataset':row['dataset'],'name':path.name})
    return items


def verify_latest():
    results=[]
    for row in list_generations():
        try:
            status=_verify_generation(row['path'])
            results.append({'path':row['path'],'valid':status['valid'],'files':status['files'],'bad':status['bad']})
        except (OSError,ValueError) as exc:
            results.append({'path':row['path'],'valid':False,'error':str(exc)[:200]})
    return {'generations_checked':len(results),'verified':sum(bool(x['valid']) for x in results),
            'results':results}


def restore_file(generation,original,confirmation,replace=False):
    """Explicit, one-file atomic restore; NEVER whole OS rollback."""
    if os.geteuid()!=0:raise PermissionError('Run as root')
    if confirmation!=('REPLACE FILE' if replace else 'RESTORE FILE'):
        raise ValueError('Explicit confirmation required')
    cfg=settings();original=str(Path(original))
    if '..' in Path(original).parts or not Path(original).is_absolute():
        raise ValueError('Restore path must be a normalized absolute path')
    if not any(Path(original)==Path(root) or Path(root) in Path(original).parents for root in cfg['sources']):
        raise ValueError('Path not in configured allowlisted sources')
    candidates={str(Path(x['path'])):x for x in list_generations()}
    if str(Path(generation)) not in candidates:raise ValueError('Unknown/unmounted verified backup generation')
    good=_verify_generation(generation)
    if not good['valid']:raise RuntimeError('Backup failed checksum validation')
    matching=[r for r in good['data']['records'] if r['original']==original]
    if len(matching)!=1:raise ValueError('File not in snapshot')
    row=matching[0];source=Path(generation)/'files'/row['relative'];target=Path(original)
    if target.is_symlink() or any(p.is_symlink() for p in target.parents):raise ValueError('Unsafe symlink target')
    if target.exists() and not target.is_file():raise ValueError('Only regular files can be restored')
    if target.exists() and not replace:raise ValueError('File exists; explicit --replace needed')
    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=target.parent/('.doomsday-restore-'+uuid.uuid4().hex)
    try:
        sha=_copy_verified(source,tmp,row['size'])
        if sha!=row['sha256']:raise RuntimeError('Restored file hash mismatch')
        if target.exists():
            old=target.parent/(target.name+'.before-doomsday-restore-'+uuid.uuid4().hex[:8])
            target.rename(old)
            try:tmp.rename(target)
            except BaseException:old.rename(target);raise
        else:tmp.rename(target)
        return {'restored':original,'sha256':sha,'replaced_existing':replace}
    finally:tmp.unlink(missing_ok=True)


def heal_missing():
    """Opt-in restore of missing *workspace* files only, never OS config or EFI.

    Current files that differ from a backup are only reported. This routine
    does not overwrite, restart services, or change any ZFS datasets.
    """
    cfg=settings()
    if not cfg['auto_restore_missing']:
        return {'status':'disabled','message':'Enable auto_restore_missing after explicit review'}
    root=Path(STATE/'zimlens')
    backups=sorted(list_generations(),key=lambda x:x['name'],reverse=True)
    if not backups:return {'status':'unavailable','reason':'No verified external backup generation'}
    candidates=[]
    # Prefer newest successfully verified generation.
    for record in backups:
        try:
            v=_verify_generation(record['path'])
            if v['valid']:
                for item in v['data']['records']:
                    original=Path(item['original'])
                    if original!=root and root not in original.parents:continue
                    if not original.exists():candidates.append((record['path'],str(original)))
                if candidates:break
        except (OSError,ValueError):continue
    restored=[];errors=[]
    for backup,path in candidates:
        try:restored.append(restore_file(backup,path,'RESTORE FILE'))
        except (OSError,ValueError,RuntimeError) as exc:errors.append({'path':path,'error':str(exc)[:200]})
    return {'status':'checked','restored_missing':restored,'errors':errors,
            'note':'Never overwrites existing files or restores OS boot/configuration automatically.'}


def main(argv=None):
    parser=argparse.ArgumentParser(description='Safe automatic backup planner for approved ZFS datasets')
    subs=parser.add_subparsers(dest='action',required=True)
    for name in ('disks','plan','status','run','verify','heal'):subs.add_parser(name)
    r=subs.add_parser('restore-file');r.add_argument('--generation',required=True);r.add_argument('--path',required=True)
    r.add_argument('--confirm',required=True);r.add_argument('--replace',action='store_true')
    a=parser.parse_args(argv)
    if a.action=='disks':out={'disks':disks(),'destinations':destinations()}
    elif a.action=='plan':out=plan()
    elif a.action=='status':out={'plan':plan(),'backups':list_generations()}
    elif a.action=='run':out=perform_backup()
    elif a.action=='verify':out=verify_latest()
    elif a.action=='heal':out=heal_missing()
    else:out=restore_file(a.generation,a.path,a.confirm,a.replace)
    print(json.dumps(out,indent=2,sort_keys=True))
    if a.action=='verify' and any(not r['valid'] for r in out['results']):return 2
    return 0

if __name__=='__main__':raise SystemExit(main())