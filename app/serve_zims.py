#!/usr/bin/env python3
"""Serve verified ZIMs from all currently enrolled read-only archive datasets."""
import os, pathlib, shutil, sys
from zim_manager import config, guard_path
from dual_storage import locations,archive_config
from archive_guard import checked_archive

def main():
    cfg=config()
    paths=[]
    for loc in locations(cfg):
        c=archive_config(cfg,loc['key'])
        try:
            path,ds=checked_archive(c)
            guard_path(path,ds)
        except (RuntimeError,ValueError) as e:
            if loc['key']=='primary':raise
            print('Secondary archive unavailable; serving installed primary library only:',e,file=sys.stderr)
            continue
        if list(path.glob('*.zim')):
            paths.append(str(path))
    if not paths:sys.exit('No verified .zim archives yet; Kiwix will start after downloads')
    exe=shutil.which('kiwix-serve')
    if not exe:sys.exit('kiwix-serve not installed; sudo apt install kiwix-tools')
    os.execv(exe,[exe,'--address=0.0.0.0','--port=8080','--blockexternal','--skipInvalid',*paths])
if __name__=='__main__':main()
