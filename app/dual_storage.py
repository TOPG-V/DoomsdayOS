#!/usr/bin/env python3
"""Conservative two-pool placement: no moves, repartitioning, or hidden fallbacks.

All pool identities are OpenZFS pool names (not /dev/sda order). A selected
backup goes on the opposite ZFS pool; this is stronger than copies=2 in one
pool, but cannot guarantee physical independence for unusual vdev layouts.
"""
from __future__ import annotations
import os
import subprocess
from pathlib import Path

GIB = 1024**3
RESERVE = 3*GIB
OS_RESERVE = 20*GIB


def pool_name(dataset):
    if not dataset or '/' not in dataset:
        raise ValueError('Expected mounted dedicated ZFS child dataset')
    return dataset.split('/')[0]


def _extra_locations():
    # Fleet enrollment records exact imported ZFS pools, not raw /dev disk ids.
    try:
        from storage_fleet import read_fleet
        rows=read_fleet()
    except FileNotFoundError:
        rows=[]
    return [{'key':x['key'],'archive':Path(x['zim_dir']),'archive_ds':x['zim_dataset'],
             'stage':Path(x['staging_dir']),'stage_ds':x['staging_dataset'],
             'backup':Path(x['backup_dir']),'backup_ds':x['backup_dataset']} for x in rows]

def locations(cfg):
    main={'key':'primary','archive':Path(cfg['zim_dir']), 'archive_ds':cfg['zim_dataset'],
          'stage':Path(cfg['staging_dir']),'stage_ds':cfg['staging_dataset'],
          'backup':Path(cfg.get('dual_backup_primary_dir') or cfg.get('backup_dir') or '/nonexistent'),
          'backup_ds':cfg.get('dual_backup_primary_dataset') or cfg.get('backup_dataset','')}
    if not cfg.get('zim_dir_2'):return [main] + _extra_locations()
    second={'key':'secondary','archive':Path(cfg['zim_dir_2']), 'archive_ds':cfg['zim_dataset_2'],
            'stage':Path(cfg['staging_dir_2']),'stage_ds':cfg['staging_dataset_2'],
            'backup':Path(cfg.get('dual_backup_secondary_dir') or '/nonexistent'),
            'backup_ds':cfg.get('dual_backup_secondary_dataset','')}
    return [main, second] + _extra_locations()


def archive_config(cfg, key):
    if key in ('primary',None):return cfg.copy()
    if key=='secondary' and cfg.get('zim_dir_2'):
        out=cfg.copy()
        for field in ('zim_dir','zim_dataset','staging_dir','staging_dataset'):
            out[field]=cfg[field+'_2']
        return out
    for x in _extra_locations():
        if x['key']==key:
            return {**cfg,'zim_dir':str(x['archive']),'zim_dataset':x['archive_ds'],
                    'staging_dir':str(x['stage']),'staging_dataset':x['stage_ds']}
    raise ValueError('ZIM storage key is not enrolled: '+str(key)[:40])


def verify_locations(cfg):
    from archive_guard import checked_archive, stage_dir
    locs=locations(cfg)
    pools=[]
    for loc in locs:
        checked_archive(archive_config(cfg,loc['key']))
        stage_dir(archive_config(cfg,loc['key']))
        pool=pool_name(loc['archive_ds'])
        if pool!=pool_name(loc['stage_ds']):
            raise ValueError('Each ZIM staging dataset must belong to its archive pool')
        pools.append(pool)
    if len(locs)>50 or len(set(pools))!=len(pools):raise ValueError('ZIM archive pools must be distinct (maximum 50)')
    return locs


def _installed(item, loc):
    from pathlib import Path
    import urllib.parse
    name=Path(urllib.parse.urlsplit(item['url']).path).name
    path=loc['archive']/name
    # This is for placement only. run_queue checks SHA-256 before trusting bytes.
    return path.is_file() and path.stat().st_size==int(item.get('size') or 0)


def plan_originals(items, cfg, free_by_key, copies_by_key, existing_parts=None):
    """Best-fit decreasing by peak (archive + largest staging) on each pool.

    Return a complete placement or raise. Only names/length are used to plan;
    the actual download verifies the trusted SHA-256 before publication.
    """
    locs=locations(cfg)
    if len(locs)!=2:raise ValueError('Enable a second dedicated ZFS archive first')
    if pool_name(locs[0]['archive_ds'])==pool_name(locs[1]['archive_ds']):
        raise ValueError('Both archives are on the same pool')
    if any(not isinstance(x.get('size'),int) or x['size']<=0 for x in items):
        raise ValueError('Unknown ZIM byte length: cannot optimize safely')
    existing_parts=existing_parts or {}
    os_pool=cfg.get('os_pool')
    budgets={}
    for loc in locs:
        k=loc['key']
        archive_copies,stage_copies=copies_by_key[k]
        if archive_copies not in (1,2,3) or stage_copies not in (1,2,3):raise ValueError('Invalid ZFS copies')
        reserve=OS_RESERVE if pool_name(loc['archive_ds'])==os_pool else RESERVE
        budgets[k]={'free':int(free_by_key[k]),'used':0,'stage_peak':0,
                    'reserve':reserve,'archive_copies':archive_copies,'stage_copies':stage_copies}
    result={}
    pending=[]
    for item in items:
        name=item['url']
        original=[loc['key'] for loc in locs if _installed(item,loc)]
        if len(original)>1:raise ValueError('Archive exists in both pools: inspect duplicate before planning')
        if original:
            result[name]=original[0];continue
        # Preserve any resumable .part files on their original pool.
        locked=existing_parts.get(name)
        if locked and locked not in budgets:raise ValueError('Partial download references an unknown drive')
        pending.append((item,locked))
    for item,locked in sorted(pending,key=lambda t:-t[0]['size']):
        choices=[]
        for loc in locs:
            k=loc['key']
            if locked and locked!=k:continue
            b=budgets[k]
            target_used=b['used']+item['size']*b['archive_copies']
            stage_peak=max(b['stage_peak'],item['size']*b['stage_copies'])
            total=target_used+stage_peak+b['reserve']
            spare=b['free']-total
            if spare>=0:
                # Keep the pool that has LESS remaining room busy (best-fit).
                choices.append((spare,k,target_used,stage_peak))
        if not choices:
            deficits={k: max(0,b['used']+item['size']*b['archive_copies']+
                      max(b['stage_peak'],item['size']*b['stage_copies'])+
                      b['reserve']-b['free']) for k,b in budgets.items()}
            raise ValueError('INSUFFICIENT SPACE for '+item['title']+
                             ': shortage by pool '+', '.join(f'{k}={v/GIB:.2f} GiB' for k,v in deficits.items()))
        _,k,used,stage=max(choices,key=lambda c:(-c[0],c[1]))
        budgets[k]['used']=used
        budgets[k]['stage_peak']=stage
        result[item['url']]=k
    return result,budgets


def backup_plan(items, placement, budgets, backup_free, backup_copies, selected_urls):
    """Back up selected ZIMs only to opposite pool after reserving originals.

    backup_free is measured on each matching pool and normally equals free_by_key;
    caller must not double-count ZFS free across datasets in one pool.
    """
    if not isinstance(selected_urls,set):selected_urls=set(selected_urls)
    chosen={}
    costs={'primary':0,'secondary':0}
    for item in sorted(items,key=lambda x:-x['size']):
        if item['url'] not in selected_urls:continue
        target='secondary' if placement[item['url']]=='primary' else 'primary'
        cost=item['size']*backup_copies[target]
        # Backups run only AFTER all original downloads and staging have finished.
        # Reserve the originals + backup, not staging + backups at the same time.
        remaining=backup_free[target]-budgets[target]['used']-budgets[target]['reserve']-costs[target]
        if cost>remaining:
            raise ValueError('INSUFFICIENT BACKUP SPACE for '+item['title']+': '+
                             f'{max(0,cost-remaining)/GIB:.2f} GiB short on {target} pool. Exclude this ZIM backup.')
        chosen[item['url']]=target
        costs[target]+=cost
    return chosen,costs


def pool_list():
    p=subprocess.run(['zpool','list','-H','-o','name'],capture_output=True,text=True)
    if p.returncode:raise RuntimeError('Cannot list ZFS pools: '+p.stderr.strip())
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


def create_dedicated(cfg, other_pool, input_fn=input):
    """User-approved non-destructive child datasets on an already imported pool.

    An existing child dataset is never silently reused or modified. No repartition.
    """
    from archive_guard import exact_dataset, checked_archive
    if os.geteuid()!=0:raise RuntimeError('Run sudo doomsday-zims dual-setup')
    checked_archive(cfg)
    primary_pool=pool_name(cfg['zim_dataset'])
    allowed=set(pool_list())-{primary_pool}
    if other_pool not in allowed:raise ValueError('Choose an imported pool different from '+primary_pool)
    if any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in other_pool):
        raise ValueError('Unexpected pool name')
    if cfg.get('zim_dir_2'):raise ValueError('Secondary storage already configured; will not replace it')
    # Fixed, dedicated child datasets on the selected pool. Do not touch others.
    paths={
      'zim':('/srv/doomsday-zims-secondary',other_pool+'/DOOMSDAY_ZIMS_SECONDARY'),
      'stage':('/srv/doomsday-zim-staging-secondary',other_pool+'/DOOMSDAY_ZIM_STAGING_SECONDARY'),
      'backup':('/srv/doomsday-backups-secondary',other_pool+'/DOOMSDAY_ZIM_BACKUPS'),
      'backup_primary':('/srv/doomsday-backups-primary',primary_pool+'/DOOMSDAY_ZIM_BACKUPS'),
    }
    print('New dedicated ZFS child datasets (never pools, partitions, OS roots):')
    for label,(path,ds) in paths.items():print(f'  {ds} -> {path}')
    print('No existing ZIM data will be moved. New secondary archives use copies=1 to save space.')
    print('Backup copies can then be placed on the other pool when selected.')
    if input_fn('Type ENABLE TWO DRIVES to create these child datasets: ').strip()!='ENABLE TWO DRIVES':
        print('Cancelled; no ZFS settings changed.');return False
    for label,(path,ds) in paths.items():
        p=Path(path)
        if p.is_symlink():raise RuntimeError('Refusing a symlinked mountpoint: '+path)
        result=subprocess.run(['zfs','list','-H','-o','name',ds],capture_output=True,text=True)
        if result.returncode==0:
            if exact_dataset(p)!=ds:
                raise RuntimeError('Dataset exists with different mountpoint, refusing to change: '+ds)
            continue
        if p.exists() and (not p.is_dir() or any(p.iterdir())):
            raise RuntimeError('Mountpoint is not an empty directory: '+path)
    for label,(path,ds) in paths.items():
        result=subprocess.run(['zfs','list','-H','-o','name',ds],capture_output=True,text=True)
        if result.returncode:  # User confirmed creation of known child datasets.
            args=['zfs','create','-o','mountpoint='+path,'-o','atime=off','-o','compression=lz4','-o','copies=1',ds]
            subprocess.run(args,check=True)
        if exact_dataset(Path(path))!=ds:raise RuntimeError('New dataset is not mounted where expected: '+ds)
    # Verify only the intended dataset gets read-only; no system/root pool edits.
    subprocess.run(['zfs','set','readonly=on',paths['zim'][1]],check=True)
    cfg.update({'zim_dir_2':paths['zim'][0],'zim_dataset_2':paths['zim'][1],
                'staging_dir_2':paths['stage'][0],'staging_dataset_2':paths['stage'][1],
                'dual_backup_primary_dir':paths['backup_primary'][0],
                'dual_backup_primary_dataset':paths['backup_primary'][1],
                'dual_backup_secondary_dir':paths['backup'][0],
                'dual_backup_secondary_dataset':paths['backup'][1],
                'os_pool':pool_name(subprocess.run(['findmnt','-n','-o','SOURCE','/'],capture_output=True,text=True,check=True).stdout.strip())})
    # Disable old auto-restore that assumes only one archive dataset.
    cfg['auto_restore_verified_zims']=False
    return True
