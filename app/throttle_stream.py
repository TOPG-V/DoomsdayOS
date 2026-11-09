#!/usr/bin/env python3
"""Limit a ZFS stream in bounded chunks. Never buffers a full backup on disk."""
import argparse, os, sys, time

def pipe(source,dest, mibps):
    size=1024*1024
    rate=float(mibps)*size
    start=time.monotonic();sent=0
    while True:
        block=source.read(size)
        if not block:break
        dest.write(block)
        sent+=len(block)
        if rate>0:
            expected=sent/rate
            delay=start+expected-time.monotonic()
            if delay>0:time.sleep(delay)
    dest.flush()

def main():
    p=argparse.ArgumentParser();p.add_argument('--mibps',type=float,default=16);a=p.parse_args()
    if not 0<=a.mibps<=1024:p.error('speed must be 0..1024 MiB/s, 0 means unlimited')
    pipe(sys.stdin.buffer,sys.stdout.buffer,a.mibps)

if __name__=='__main__':main()
