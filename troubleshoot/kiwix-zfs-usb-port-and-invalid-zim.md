# Kiwix exit code 1, invalid ZIM entries, and ZFS USB-port changes

**DoomsdayOS troubleshooting guide — 2026-11-10**

This guide applies to an Ubuntu DoomsdayOS installation with an existing ZFS `tank/zims` dataset. It handles these symptoms:

- Kiwix reports `IP not available on this system: 0.0.0.0` and exits with code 1.
- Kiwix reports that it is skipping an invalid archive even though a `.zim` is present.
- The ZFS data drive was reconnected through a different USB port and the archive path appears unavailable.

## What the patch fixes

The Kiwix launcher at `app/serve_zims.py` no longer forces `--address=0.0.0.0`, which some packaged Kiwix versions reject. When building a Kiwix XML library from several archives fails, it checks each archive individually and identifies those Kiwix cannot read. The patch does **not** change or delete existing ZIM bytes.

Reference commit: [add5fccc2c8be82a976060a05f6f1005cc747296](https://github.com/TOPG-V/DoomsdayOS/commit/add5fccc2c8be82a976060a05f6f1005cc747296).

## 1. Update the installed launcher

Run on the Ubuntu machine (Internet connection required for download):

```bash
sudo bash -c 'set -e
cd /opt/doomsday-firstboot/app
cp -a serve_zims.py serve_zims.py.backup
curl -fL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/app/serve_zims.py -o serve_zims.py.new
python3 -m py_compile serve_zims.py.new
mv serve_zims.py.new serve_zims.py
systemctl restart doomsday-kiwix.service
systemctl status doomsday-kiwix.service --no-pager
'
```

This updates the launcher only. If `/opt/doomsday-firstboot/app/serve_zims.py` does not exist, check the DoomsdayOS installation path before running this command.

## 2. Diagnose ZFS device discovery and mount status

ZFS identifies pools by metadata; moving between USB ports usually changes the Linux `/dev/sdX` name, not the pool identity.

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS
sudo zpool status
sudo zpool import
sudo zfs list -o name,mountpoint,mounted
```

If the **unimported** pool `tank` is shown as available:

```bash
sudo zpool import -d /dev/disk/by-id tank
```

If `tank` is **already imported** but its ZIM dataset isn't mounted:

```bash
sudo zfs mount tank/zims
ls -lah /tank/zims
```

Verify the actual mount source and dataset name:

```bash
findmnt -M /tank/zims -o SOURCE,FSTYPE,TARGET
sudo zfs get -H -o name,property,value mountpoint,mounted,readonly tank/zims
```

The configured `zim_dir` and `zim_dataset` in `/etc/doomsday-vault/firstboot.json` must match the actual dedicated ZFS mount and dataset. **Do not** write archives into an unmounted `/tank/zims` directory on the OS filesystem.

**Safety:** Never format, recreate, or destroy the existing pool as a troubleshooting step. Do not force-import a pool that may still be active on another computer. If ZFS reports the pool is active elsewhere, safely export it from that machine first and investigate before importing.

## 3. Verify whether the ZIM is actually readable

A visible `.zim` file is not necessarily complete or compatible. Diagnose with:

```bash
ls -lah /tank/zims/*.zim
command -v kiwix-serve
command -v kiwix-manage
sudo journalctl -u doomsday-kiwix -n 80 --no-pager
```

If the launcher names a specific unreadable archive, check its size and compare its complete SHA-256 digest against a trusted publisher checksum or the DoomsdayOS manifest. The launcher does not automatically repair or delete it.

If `tank/zims` is absent from the list of mounted datasets, resolve that first; do not treat the absence of a mount as archive corruption.

## 4. Restart and test Kiwix

```bash
sudo systemctl restart doomsday-kiwix
curl -I http://127.0.0.1:8080
sudo journalctl -u doomsday-kiwix -n 50 --no-pager
```

Local URLs on the Ubuntu host:

- Kiwix library: http://127.0.0.1:8080
- DoomsdayOS workspace: http://127.0.0.1:8765

Remote access from other devices depends on listener addresses and network firewall configuration. Removing the forced `0.0.0.0` address does **not** guarantee Kiwix will listen on every network interface.

## Current limitation

The launcher patch is committed, but unattended ZFS pool re-import across arbitrary USB ports is **not** implemented or validated by this guide. Use the manual inspection/import steps above; unattended mounting needs a separately tested, identity-aware service with appropriate safeguards.
