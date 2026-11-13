#!/usr/bin/env python3
"""Conservative ZFS fleet allocator: 1..50 already-imported distinct pools.

No raw disk commands, no zpool create/destroy, no partition writes. Enrolling
creates named CHILD datasets on an existing non-root pool only after consent.
Every plan is a preview; queue changes require an explicit second approval.
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, urllib.parse
from pathlib import Path

STATE=Path(os.environ.get('DOOMSDAY_FLEET_STATE','/var/lib/doomsday-vault/fleet.json'))
GIB=1024**3
MAX_DRIVES=50
RESERVE=3*GIB

def call(*args):
    r=subprocess.run(args,capture_output=True,text=True,timeout=25)
    if r.returncode:raise RuntimeError('Command failed: '+ ' '.join(args)+': '+r.stderr.strip()[:400])
    return r.stdout.strip()

def pool_for(dataset):return str(dataset).split('/')[0]

def root_pool():
    try:
        source=call('findmnt','-n','-o','SOURCE','/')
        return pool_for(source) if '/' in source else None
    except (OSError,RuntimeError):return None

def available_pools():
    lines=call('zpool','list','-H','-o','name').splitlines()
    return [x.strip() for x in lines if re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]*',x.strip())]

def read_fleet(path=STATE):
    if not Path(path).is_file():return []
    rows=json.loads(Path(path).read_text())
    if not isinstance(rows,list) or len(rows)>MAX_DRIVES:raise ValueError('Invalid fleet configuration')
    keys=set();pools=set()
    for row in rows:
        if not isinstance(row,dict) or not all(k in row for k in ('key','pool','zim_dataset','staging_dataset','zim_dir','staging_dir','backup_dataset','backup_dir')):
            raise ValueError('Incomplete fleet entry')
        if row['key'] in keys or row['pool'] in pools:raise ValueError('Repeated fleet key or pool')
        if pool_for(row['zim_dataset'])!=row['pool'] or pool_for(row['staging_dataset'])!=row['pool'] or pool_for(row['backup_dataset'])!=row['pool']:
            raise ValueError('Fleet dataset on unexpected pool')
        keys.add(row['key']);pools.add(row['pool'])
    return rows

def fleet_entry(pool,key):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,35}',pool):raise ValueError('Unexpected pool name')
    if not re.fullmatch(r'drive(?:[3-9]|[1-4][0-9]|50)',key):raise ValueError('Drive key must be drive3 through drive50')
    base=f'/srv/doomsday-drives/{pool}'
    return {'key':key,'pool':pool,'zim_dataset':pool+'/DOOMSDAY_FLEET_ZIMS',
      'staging_dataset':pool+'/DOOMSDAY_FLEET_STAGE','backup_dataset':pool+'/DOOMSDAY_FLEET_BACKUP',
      'zim_dir':base+'/zims','staging_dir':base+'/stage','backup_dir':base+'/backup'}

def enroll_existing(pool,input_fn=input,write=True):
    """Non-destructive enrollment of imported empty pool child datasets.

    Never formats the device or adopts existing non-Doomsday datasets.
    """
    if os.geteuid()!=0:raise PermissionError('Run with sudo')
    import zim_manager
    cfg=zim_manager.config()
    if pool not in available_pools():raise ValueError('Pool is not imported')
    if pool==root_pool():raise ValueError('OS root pool is excluded from fleet auto-enrollment')
    enrolled=read_fleet();already={pool_for(cfg.get('zim_dataset',''))}
    if cfg.get('zim_dataset_2'):already.add(pool_for(cfg['zim_dataset_2']))
    already.update(x['pool'] for x in enrolled)
    if pool in already:raise ValueError('Pool already enrolled')
    if len(already)+1>MAX_DRIVES:raise ValueError('Maximum 50 distinct pools')
    key='drive'+str(next(i for i in range(3,51) if 'drive'+str(i) not in {x['key'] for x in enrolled}))
    entry=fleet_entry(pool,key)
    for kind in ('zim','staging','backup'):
        ds=entry[('zim' if kind=='zim' else kind)+'_dataset']
        path=Path(entry[('zim' if kind=='zim' else kind)+'_dir'])
        r=subprocess.run(['zfs','list','-H','-o','name',ds],capture_output=True,text=True)
        if r.returncode==0:raise ValueError('Refusing to adopt existing dataset: '+ds)
        if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
            raise ValueError('Refusing non-empty or symlink mount point: '+str(path))
    print('Imported ZFS pool:',pool)
    print('Will create ONLY these child datasets on this pool:')
    for kind in ('zim','staging','backup'):print(' ',entry[('zim' if kind=='zim' else kind)+'_dataset'])
    print('No physical disk formatting, partitioning or existing dataset replacement. Files will use copies=1; cross-pool backups provide redundancy.')
    if input_fn('Type ENROLL '+pool+' to create child datasets: ').strip()!='ENROLL '+pool:
        return None
    if not write:return entry
    # Pin the imported pool GUID at the time of explicit enrollment. Names
    # and /dev/sdX numbers can be reused after a USB drive is replaced.
    pool_guid=call('zpool','get','-H','-o','value','guid',pool)
    if not re.fullmatch(r'\d+',pool_guid):
        raise RuntimeError('Cannot verify enrolled ZFS pool GUID')
    entry['pool_guid']=pool_guid
    for kind in ('zim','staging','backup'):
        ds=entry[('zim' if kind=='zim' else kind)+'_dataset'];mp=entry[('zim' if kind=='zim' else kind)+'_dir']
        call('zfs','create','-o','mountpoint='+mp,'-o','atime=off','-o','compression=lz4','-o','copies=1',ds)
    # The trusted publisher writes verified originals by temporarily unsetting readonly.
    call('zfs','set','readonly=on',entry['zim_dataset'])
    enrolled.append(entry)
    STATE.parent.mkdir(parents=True,exist_ok=True)
    from zim_manager import atomic_json
    atomic_json(STATE,enrolled)
    return entry

def drive_info(cfg,extra=None):
    """Check actual mounted datasets, not free space inferred from directory names."""
    from dual_storage import verify_locations
    from archive_guard import exact_dataset
    from zim_manager import disk_free,zfs_copies
    locs=verify_locations(cfg)
    result=[]
    for loc in locs:
        p=pool_for(loc['archive_ds'])
        result.append({'key':loc['key'],'pool':p,'archive':str(loc['archive']),
                       'backup':str(loc.get('backup') or ''),
                       'backup_ready':bool(loc.get('backup_ds') and exact_dataset(Path(loc['backup']))==loc['backup_ds']),
                       'free':int(disk_free(loc['archive'])),'reserve':20*GIB if p==root_pool() else RESERVE,
                       'copies':zfs_copies(loc['archive']),'staging_copies':zfs_copies(loc['stage'])})
    return result

def allocate(items,drives,backup_urls=None,replicas=2):
    """Plan first copy + up to two *additional* replicas on distinct pools.

    Returns exact fit or raises; stage peak is conservatively reserved. The caller
    must refresh free space and mount identity immediately before any writes.
    """
    if not 1<=len(drives)<=MAX_DRIVES:raise ValueError('Use 1-50 drives')
    if len({d['pool'] for d in drives})!=len(drives):raise ValueError('Only one drive per independent ZFS pool')
    if replicas not in (0,1,2):raise ValueError('Up to two additional backup copies')
    backup_urls=set(backup_urls or ())
    budgets={d['key']:{**d,'remaining':int(d['free'])-int(d.get('reserve',RESERVE)),'stage_peak':0} for d in drives}
    if min(d['remaining'] for d in budgets.values())<=0:raise ValueError('Not enough free space after safety reserve')
    assignments={};backup_assignments={};warnings=[]
    for item in sorted(items,key=lambda x:-int(x.get('size') or 0)):
        size=int(item.get('size') or 0);url=item.get('url')
        if size<=0 or not url:raise ValueError('Each archive needs a known size and URL')
        choices=[]
        for d in budgets.values():
            new_peak=max(d['stage_peak'],size*int(d['staging_copies']))
            added_stage=new_peak-d['stage_peak']
            cost=size*int(d['copies'])+added_stage
            if d['remaining']>=cost:
                choices.append((d['remaining']-cost,d['key'],cost,new_peak))
        if not choices:raise ValueError('INSUFFICIENT SPACE for '+str(item.get('title',url))+': no archive+stage fit on any enrolled pool')
        _,key,cost,peak=min(choices)
        d=budgets[key];d['remaining']-=cost;d['stage_peak']=peak;assignments[url]=key
    # Once originals are complete, all staging buffers are released.
    for d in budgets.values():d['remaining']+=d['stage_peak'];d['stage_peak']=0
    for item in sorted(items,key=lambda x:-int(x['size'])):
        url=item['url'];size=int(item['size'])
        if url not in backup_urls:continue
        backup_assignments[url]=[]
        for _ in range(min(replicas,len(drives)-1)):
            candidates=[d for d in budgets.values() if d['key'] not in [assignments[url]]+backup_assignments[url] and d.get('backup_ready',True) and d['remaining']>=size*int(d['copies'])]
            if not candidates:
                warnings.append('Not enough independent backup space for '+str(item.get('title',url)))
                break
            target=max(candidates,key=lambda d:d['remaining']-size*int(d['copies']))
            target['remaining']-=size*int(target['copies'])
            backup_assignments[url].append(target['key'])
        if len(backup_assignments[url])<replicas:
            warnings.append('Requested redundancy not fully satisfied for '+str(item.get('title',url)))
    return {'originals':assignments,'backups':backup_assignments,'remaining_bytes':{k:x['remaining'] for k,x in budgets.items()},'warnings':warnings,
            'note':'PREVIEW ONLY: no queue changes, no disks formatted, backups after originals finish'}

def apply_plan(cfg,queue,drives,replicas=2,input_fn=input):
    """Explicitly assign NEW queue originals and backup destinations.

    No existing .part/verified file is moved. If existing data is found we
    refuse rather than silently changing its storage key.
    """
    import zim_manager
    from dual_storage import verify_locations
    from urllib.parse import urlsplit
    with zim_manager.exclusive_download_worker():
        live={x['key']:x for x in verify_locations(cfg)}
        if set(live)!={x['key'] for x in drives}:raise RuntimeError('Enrolled pool configuration changed')
        for item in queue:
            if item.get('status') not in ('pending','retry'):
                raise ValueError('Fleet apply only supports a new pending queue; preserve existing completed archives')
            name=Path(urlsplit(item['url']).path).name
            for loc in live.values():
                if any((loc['stage']/candidate).exists() for candidate in (name,name+'.part')) or (loc['archive']/name).exists():
                    raise ValueError('Existing ZIM or partial on '+loc['key']+'; plan only, never move it automatically')
        # Refresh actual free space and dataset mount identities immediately before commit.
        fresh=drive_info(cfg)
        selected={x['url'] for x in queue if x.get('backup')}
        plan=allocate(queue,fresh,selected,replicas)
        if plan['warnings']:
            print('BACKUP SHORTAGES:')
            for item in plan['warnings']:print(' ',item)
        print('Originals: ',json.dumps(plan['originals'],indent=2))
        print('Backup pools:',json.dumps(plan['backups'],indent=2))
        if input_fn('Type APPLY FLEET to update this pending queue (no disk formatting): ').strip()!='APPLY FLEET':
            return {'cancelled':True}
        with zim_manager.locked_queue():
            current=zim_manager.read_queue()
            if {x['url'] for x in current}!={x['url'] for x in queue} or any(x.get('status') not in ('pending','retry') for x in current):
                raise RuntimeError('Queue changed during planning, refusing to apply')
            for item in current:
                item['storage']=plan['originals'][item['url']]
                copies=plan['backups'].get(item['url'],[])
                item['backup']=bool(copies)
                item['backup_storages']=copies
                item['backup_storage']=copies[0] if copies else None
                # Partial redundancy is exposed explicitly, not claimed as complete.
                item['backup_result']='planned:'+str(len(copies))+' independent pool copy/copies'
            zim_manager.atomic_json(zim_manager.queue_path(),current)
        return plan

def main(argv=None):
    parser=argparse.ArgumentParser(description='Safe DoomsdayOS fleet management (up to 50 ZFS pools)')
    sub=parser.add_subparsers(dest='cmd',required=True)
    sub.add_parser('scan');enroll=sub.add_parser('enroll');enroll.add_argument('pool');plan=sub.add_parser('plan');plan.add_argument('--replicas',type=int,choices=[0,1,2],default=2);apply=sub.add_parser('apply');apply.add_argument('--replicas',type=int,choices=[0,1,2],default=2)
    a=parser.parse_args(argv)
    if a.cmd=='enroll':print(json.dumps(enroll_existing(a.pool) or {'cancelled':True},indent=2));return
    import zim_manager
    cfg=zim_manager.config()
    if a.cmd=='scan':print(json.dumps({'pools':available_pools(),'os_pool':root_pool(),'enrolled':read_fleet(),'drives':drive_info(cfg)},indent=2));return
    queue=zim_manager.read_queue()
    if a.cmd=='apply':print(json.dumps(apply_plan(cfg,queue,drive_info(cfg),a.replicas),indent=2));return
    print(json.dumps(allocate(queue,drive_info(cfg),{x['url'] for x in queue if x.get('backup')},a.replicas),indent=2))
if __name__=='__main__':main()
