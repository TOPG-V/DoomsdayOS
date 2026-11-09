#!/usr/bin/env python3
"""Non-destructive hardware recheck. Import only explicitly enrolled backup pools."""
import json, os, subprocess
from pathlib import Path

def run(*args):return subprocess.run(args,capture_output=True,text=True)

def main():
    # Never import random ZFS pools, never force-import, never wipe disks.
    cfg=Path('/etc/doomsday-vault/recovery.env')
    # Only check/import the explicitly enrolled GUID, without copying full backup
    # editions each time a USB interface comes online.
    common=Path('/usr/local/lib/doomsday-vault/common.sh')
    if cfg.exists() and common.exists():
        subprocess.run(['/bin/bash','-c','source /usr/local/lib/doomsday-vault/common.sh; if [[ "$EXTERNAL_POOL" != none ]]; then find_external_pool >/dev/null || true; fi'],timeout=45,check=False)
    # Re-check wireless AP capabilities after moving computer or plugging new dongle.
    from wifi_manager import ensure_hotspot
    ensure_hotspot()
if __name__=='__main__':main()
