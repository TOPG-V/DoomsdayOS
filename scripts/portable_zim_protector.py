#!/usr/bin/env python3
"""Read-only, cross-platform ZIM integrity auditing and optional ZFS readonly lock.

Does not import pools, alter partitions, repair or remove files. Existing DoomsdayOS
zim-manifest.json entries hold the trusted publisher SHA-256 checksum. Locally
created baselines are explicitly unauthenticated, not proof of original content.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
BLOCK = 4 * 1024 * 1024


def run(*argv):
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(BLOCK), b''):
            h.update(chunk)
    return h.hexdigest()


def choose_archive(path: str) -> Path:
    root = Path(path).expanduser().absolute()
    if root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise ValueError('Archive must be an existing real directory, not a symlink')
    return root


def manifest_entries(path: str) -> dict:
    p = Path(path).expanduser()
    if not p.is_file() or p.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Manifest missing or larger than 16 MiB')
    payload = json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('Manifest must be an object')
    # Either DoomsdayOS immutable-archive schema or generated portable baseline.
    entries = payload.get('files', payload)
    if not isinstance(entries, dict):
        raise ValueError('Invalid manifest entries')
    parsed = {}
    for name, data in entries.items():
        if (not isinstance(name, str) or name != Path(name).name
                or not name.lower().endswith('.zim') or name in ('.', '..')):
            raise ValueError('Unsafe ZIM filename in manifest')
        if not isinstance(data, dict):
            raise ValueError('Invalid manifest record')
        sha = data.get('sha256', '')
        if not isinstance(sha, str) or not SHA256.fullmatch(sha):
            raise ValueError('Missing SHA-256 digest in manifest')
        length = data.get('bytes')
        if length is not None and (type(length) is not int or length < 0):
            raise ValueError('Invalid byte count')
        parsed[name] = {'sha256': sha.lower(), 'bytes': length,
                        'dataset': str(data.get('archive_dataset') or '')}
    if not parsed:
        raise ValueError('Manifest does not contain any ZIMs')
    return parsed


def filter_dataset(entries:dict, dataset:str|None) ->dict:
    if not dataset:
        return entries
    selected={name:entry for name,entry in entries.items()
              if not entry.get('dataset') or entry['dataset']==dataset}
    if not selected:
        raise ValueError('No manifest entries for the selected dataset: '+dataset)
    return selected


def listed_zims(archive: Path) -> list[Path]:
    out=[]
    for entry in sorted(archive.iterdir()):
        if entry.name.lower().endswith('.zim'):
            if entry.is_symlink() or not entry.is_file():
                raise ValueError('Nonregular ZIM entry is unsafe: '+entry.name)
            out.append(entry)
    return out


def verify(archive: Path, manifest: dict, *, all_files=False, hash_fn=digest) -> dict:
    results=[]
    for name, record in sorted(manifest.items()):
        p=archive/name
        if p.is_symlink() or not p.is_file():
            results.append({'name':name,'status':'missing'});continue
        try:
            size=p.stat().st_size
            if record['bytes'] is not None and size != record['bytes']:
                results.append({'name':name,'status':'size-mismatch'});continue
            actual=hash_fn(p)
            results.append({'name':name,'status':'ok' if actual == record['sha256'] else 'sha256-mismatch'})
        except OSError:
            results.append({'name':name,'status':'read-error'})
    if all_files:
        for p in listed_zims(archive):
            if p.name not in manifest:
                results.append({'name':p.name,'status':'untracked'})
    return {'archive':str(archive),'results':results,
            'ok':all(r['status']=='ok' for r in results),
            'warning':'Checksums verify against the supplied manifest only. Confirm manifest hashes against trusted publishers.'}


def status(archive: Path) -> dict:
    d={'archive':str(archive),'zims':len(listed_zims(archive)),'fs':'unknown',
       'readonly':None,'dataset':None,'pool_health':None}
    if os.name!='posix' or sys.platform!='linux':return d
    # -M only succeeds for an exact mountpoint; never infer ZFS dataset from /dev/sdX.
    p=run('findmnt','-M',str(archive),'-n','-o','SOURCE,FSTYPE')
    if p and p.returncode==0:
        bits=p.stdout.split()
        if len(bits)==2:
            d['fs']=bits[1]
            if bits[1]=='zfs':
                d['dataset']=bits[0]
                ro=run('zfs','get','-H','-o','value','readonly',bits[0])
                if ro and ro.returncode==0:
                    d['readonly']=ro.stdout.strip()=='on'
                pool=bits[0].split('/')[0]
                health=run('zpool','list','-H','-o','health',pool)
                if health and health.returncode==0:
                    d['pool_health']=health.stdout.strip()
    return d


def protect_zfs(archive: Path, manifest: dict, *, confirmed=False) -> dict:
    if not confirmed:raise ValueError('Pass --yes after inspecting the status and checking backups')
    if os.name!='posix' or sys.platform!='linux':
        raise RuntimeError('ZFS readonly locking is supported on Linux only')
    report=status(archive)
    ds=report.get('dataset')
    if not ds or '/' not in ds or ds.endswith('/ROOT') or '/ROOT/' in ds or '/BOOT/' in ds:
        raise RuntimeError('Refusing to change a pool root or non-dedicated ZFS dataset')
    for mount in ('/', '/boot', '/boot/efi'):
        current=run('findmnt','-n','-o','SOURCE',mount)
        if current and current.returncode==0 and ds==current.stdout.strip():
            raise RuntimeError('Refusing to set an OS/boot dataset read-only')
    if ds.rsplit('/',1)[-1].lower() not in ('zims','kiwix','zim-archive','zim-archives'):
        raise RuntimeError('Refusing to protect a dataset not explicitly named for ZIM archives')
    if len(listed_zims(archive)) != len(manifest):
        raise RuntimeError('Archive contains missing/untracked ZIMs; refusing protection')
    validation=verify(archive, manifest, all_files=True)
    if not validation['ok']:
        raise RuntimeError('At least one archive failed verification; refusing protection')
    # No readonly=off operation is ever exposed by this tool.
    p=run('zfs','set','readonly=on',ds)
    if p is None or p.returncode:
        raise RuntimeError('ZFS readonly setting failed; run as administrator on a mounted dataset')
    check=status(archive)
    if check['readonly'] is not True:
        raise RuntimeError('Readonly setting could not be confirmed')
    return check


def baseline(archive:Path, destination: str)->Path:
    target=Path(destination).expanduser().absolute()
    if target.exists() or target.is_symlink():raise ValueError('Refusing to overwrite an existing manifest')
    if target.parent.resolve() == archive.resolve():
        raise ValueError('Store the baseline outside the protected ZIM directory')
    files={p.name:{'sha256':digest(p),'bytes':p.stat().st_size} for p in listed_zims(archive)}
    if not files:raise ValueError('No completed ZIM files were found')
    payload={'format':'portable-zim-baseline-v1','trust':'LOCAL_BASELINE_NOT_PUBLISHER_AUTHENTICATED',
             'files':files}
    # exclusive create prevents overwriting existing manifests.
    with target.open('x',encoding='utf-8') as f:
        json.dump(payload,f,indent=2,sort_keys=True);f.write('\n')
    return target


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=('status','verify','baseline','protect'))
    parser.add_argument('--archive',required=True,help='Existing mounted ZIM directory (not a drive letter or /dev/sdX)')
    parser.add_argument('--manifest',help='Existing trusted DoomsdayOS zim-manifest.json (for verify/protect)')
    parser.add_argument('--output',help='Destination for baseline manifest; must not exist')
    parser.add_argument('--dataset',help='Filter a multi-pool DoomsdayOS manifest by archive ZFS dataset')
    parser.add_argument('--yes',action='store_true',help='Explicitly approve readonly=on on validated dedicated ZFS dataset')
    args=parser.parse_args(argv)
    try:
        path=choose_archive(args.archive)
        if args.action=='status':data=status(path)
        elif args.action=='baseline':
            if not args.output:raise ValueError('--output is required')
            data={'saved_baseline':str(baseline(path,args.output)),
                  'warning':'Baseline is only a change detector. It does NOT authenticate publisher contents.'}
        else:
            if not args.manifest:raise ValueError('--manifest is required for verify/protect')
            entries=manifest_entries(args.manifest)
            active_dataset=args.dataset or status(path).get('dataset')
            entries=filter_dataset(entries, active_dataset)
            if args.action=='protect':
                record=json.loads(Path(args.manifest).read_text(encoding='utf-8'))
                if record.get('trust')=='LOCAL_BASELINE_NOT_PUBLISHER_AUTHENTICATED':
                    raise ValueError('Cannot use an unauthenticated local baseline for ZFS protection; provide trusted publisher digests')
            data=verify(path,entries,all_files=True) if args.action=='verify' else protect_zfs(path,entries,confirmed=args.yes)
        print(json.dumps(data,indent=2,sort_keys=True))
        return 0 if data.get('ok',True) else 2
    except (ValueError,RuntimeError,OSError,json.JSONDecodeError) as e:
        print('Portable ZIM Protector: '+str(e),file=sys.stderr);return 2

if __name__=='__main__':raise SystemExit(main())