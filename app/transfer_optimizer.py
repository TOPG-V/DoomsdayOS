#!/usr/bin/env python3
"""Conservative, drive-local write budget for HTTPS and aria2 ZIM transfers.

This is a benchmark-based upper bound, not a disk-health or sustained-write
certification. It does not modify ZFS properties or partition devices.
"""
from __future__ import annotations
import argparse
import json
import math
import os
import shutil
import tempfile
import time
from pathlib import Path

MIB=1024**2
GIB=1024**3
SAFE_RESERVE=3*GIB
BENCH_BYTES=48*MIB
POLICY_DEFAULT=Path(os.environ.get('DOOMSDAY_TRANSFER_POLICY','/var/lib/doomsday-vault/transfer-policy.json'))


def read_policy(path=None):
    f=Path(path or POLICY_DEFAULT)
    try:
        data=json.loads(f.read_text())
        return data if data.get('schema')==1 and isinstance(data.get('datasets'),dict) else {'schema':1,'datasets':{}}
    except (OSError,ValueError,TypeError):return {'schema':1,'datasets':{}}


def save_policy(data,path=None):
    # Reuse the daemon's atomic, private state writer.
    from zim_manager import atomic_json
    atomic_json(Path(path or POLICY_DEFAULT),data)


def dataset_identity(path):
    from zim_manager import guard_path
    ds=guard_path(path)
    if not ds:
        raise ValueError('Cannot verify mounted filesystem for drive speed benchmark')
    return ds


def benchmark_write(directory, *, bytes_total=BENCH_BYTES, clock=time.monotonic):
    """Temporarily write incompressible blocks to an existing staging dataset.

    fsync accounts for much of the filesystem's write buffering, but ZFS / VM
    caches and drive write caches can still overestimate sustained throughput.
    """
    directory=Path(directory)
    if not directory.is_dir() or directory.is_symlink():
        raise ValueError('Benchmark target must be an existing, non-symlink directory')
    bytes_total=int(bytes_total)
    if bytes_total<4*MIB or bytes_total>128*MIB:
        raise ValueError('Benchmark size must be 4 to 128 MiB')
    if shutil.disk_usage(directory).free < bytes_total+SAFE_RESERVE:
        raise ValueError('Insufficient spare space for non-destructive write benchmark')
    fd,name=tempfile.mkstemp(prefix='.doomsday-write-test-',suffix='.tmp',dir=directory)
    begin=clock()
    try:
        os.fchmod(fd,0o600)
        # Generate fresh bytes, avoiding compressible all-zero test files on ZFS.
        written=0
        while written<bytes_total:
            block=os.urandom(min(4*MIB,bytes_total-written))
            mv=memoryview(block)
            while len(mv):
                n=os.write(fd,mv)
                if n<=0:raise OSError('Zero-length benchmark write')
                mv=mv[n:]
            written+=len(block)
        os.fsync(fd)
        seconds=max(clock()-begin,0.001)
    finally:
        os.close(fd)
        os.unlink(name)
    return {'write_mib_s':round((bytes_total/MIB)/seconds,2),'seconds':round(seconds,3),
            'bytes':bytes_total,'measured_at':int(time.time())}


def tune(directory, *, policy_path=None, bytes_total=BENCH_BYTES):
    source=dataset_identity(directory)
    result=benchmark_write(directory,bytes_total=bytes_total)
    data=read_policy(policy_path)
    data['datasets'][source]=result
    save_policy(data,policy_path)
    return source,result


def budget_mib_s(speed):
    """Leave >=30% measured throughput for the OS and ZFS processing.

    Clamp the untrusted benchmark at 256 MiB/s (VM/page-cache overestimation).
    """
    if not isinstance(speed,(float,int)) or not math.isfinite(speed) or speed<=0:
        return 16
    return max(4,min(160,int(min(speed,256)*0.65)))


def get_budget(directory,*,policy_path=None, auto=False, max_age_days=14):
    identity=dataset_identity(directory)
    data=read_policy(policy_path)
    rec=data['datasets'].get(identity,{})
    now=time.time()
    if auto and (not rec or now-float(rec.get('measured_at',0))>max_age_days*86400):
        try:
            _,rec=tune(directory,policy_path=policy_path)
        except (OSError,ValueError):
            # Do not risk writing a benchmark if the pool is almost full.
            pass
    return budget_mib_s(rec.get('write_mib_s',0))


def torrent_parallelism(write_budget, observed_file_mib_s=0, requested=4):
    """Choose one file for fast torrents, several for slow peers; cap at four."""
    requested=max(1,min(4,int(requested)))
    budget=max(1,float(write_budget))
    observed=max(0,float(observed_file_mib_s))
    if observed>=0.55*budget:return 1
    if observed==0:return min(2,requested)  # cold start before measured speed
    return max(1,min(requested,int(0.70*budget/max(observed,0.25))))


def record_torrent_speed(directory,per_file_mib_s,policy_path=None):
    source=dataset_identity(directory)
    data=read_policy(policy_path)
    rec=data['datasets'].setdefault(source,{})
    # Exponentially smoothed per-file rate, not total batch throughput.
    previous=float(rec.get('torrent_mib_s',0))
    value=max(0,min(10000,float(per_file_mib_s)))
    if value:
        rec['torrent_mib_s']=round((previous*0.6+value*0.4) if previous else value,3)
    save_policy(data,policy_path)


class RateLimiter:
    """Simple time-accounted write limiter. Does not buffer or write data."""
    def __init__(self,mib_per_s,clock=time.monotonic,sleep=time.sleep):
        self.limit=max(0,float(mib_per_s))*MIB
        self.clock,self.sleep=clock,sleep
        self.start=self.clock();self.bytes=0

    def account(self,n):
        self.bytes+=n
        if self.limit<=0:return
        remaining=self.bytes/self.limit-(self.clock()-self.start)
        if remaining>0:self.sleep(remaining)


def main():
    p=argparse.ArgumentParser(description='Benchmark safe ZFS staging write throughput')
    p.add_argument('command',choices=['tune','status'])
    p.add_argument('--staging',default=None)
    a=p.parse_args()
    from zim_manager import config
    path=Path(a.staging or config().get('staging_dir','/srv/doomsday-zim-staging'))
    if a.command=='tune':
        key,result=tune(path)
        print(f'{key}: measured {result["write_mib_s"]} MiB/s, transfer cap {budget_mib_s(result["write_mib_s"])} MiB/s')
    else:
        key=dataset_identity(path)
        print(json.dumps({'dataset':key,'benchmark':read_policy()['datasets'].get(key),
                          'transfer_limit_mib_s':get_budget(path)},indent=2))

if __name__=='__main__':main()
