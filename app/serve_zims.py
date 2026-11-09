#!/usr/bin/env python3
"""Serve verified ZIMs over the trusted local network via official Kiwix."""
import os, pathlib, shutil, sys
from zim_manager import config, guard_path

def main():
    path=pathlib.Path(config()['zim_dir'])
    if not path.is_dir():sys.exit('ZIM directory missing. Will retry after the drive reconnects.')
    guard_path(path,config().get('zim_dataset',''))
    files=list(path.glob('*.zim'))
    if not files:sys.exit('No verified .zim archives yet; Kiwix will start after downloads')
    exe=shutil.which('kiwix-serve')
    if not exe:sys.exit('kiwix-serve not installed; sudo apt install kiwix-tools')
    os.execv(exe,[exe,'--address=0.0.0.0','--port=8080','--blockexternal','--skipInvalid',str(path)])
if __name__=='__main__':main()
