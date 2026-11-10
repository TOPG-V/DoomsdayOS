# DoomsdayOS — Complete setup and troubleshooting command list

**Ubuntu command reference • 2026-11-10**  
Repository: https://github.com/TOPG-V/DoomsdayOS

Run these commands **on Ubuntu**, not Windows PowerShell. This reference covers Core and Full AI installs, updates, ZFS, ZIMs, the GUI, services, backups, llama.cpp hardware detection, and diagnostics. **Run sections as needed, not every command blindly.** Replace paths, pool names and model filenames with real values.

## 1. Check the operating system and disks before installing

```bash
cat /etc/os-release
uname -a
lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS
findmnt -n -o SOURCE,FSTYPE /
sudo zpool status
sudo zfs list -o name,mountpoint,mounted
```

ZFS utilities missing? Install the appropriate Ubuntu OpenZFS packages *before* attempting a ZFS-dependent setup. Do not create, destroy, repartition or format a pool to solve a service error.

## 2. Full AI version: one-command installer

```bash
curl -fsSL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/scripts/quick-install.sh -o /tmp/doomsday-install.sh
bash /tmp/doomsday-install.sh --ai
```

For the **Core/no-AI** version instead:

```bash
curl -fsSL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/scripts/quick-install.sh -o /tmp/doomsday-install.sh
bash /tmp/doomsday-install.sh --core
```

Review downloaded scripts before executing them as root. The installer does not include GGUF model weights and may need the Internet for packages. Existing vault installations should be backed up first.

## 3. Update the ZIM dashboard and smallest-first downloader

```bash
curl -fsSL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/scripts/update-downloads-ui.sh -o /tmp/update-downloads-ui.sh
sudo bash /tmp/update-downloads-ui.sh
```

This updates the web files and ZIM manager, not your existing ZIM dataset. The queue prefers smaller known-size ZIMs before larger ones when choosing new work; it does not interrupt an already active transfer.

## 4. Install CPU/GPU llama.cpp backend selection update

```bash
sudo bash -c 'set -e
cd /opt/doomsday-firstboot/app
for file in llama_backend.py llama_runtime.py; do
  curl -fL "https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/app/$file" -o "$file.new"
  python3 -m py_compile "$file.new"
  if [ -f "$file" ]; then cp -a "$file" "$file.backup"; fi
  mv "$file.new" "$file"
done'
```

Backend detection is conservative. The installed `llama-server` must itself have the GPU backend compiled and working, otherwise DoomsdayOS uses CPU.

## 5. First-time setup and essential services

```bash
sudo doomsday-setup
sudo systemctl daemon-reload
sudo systemctl enable --now doomsday-ai-web.service doomsday-kiwix.service doomsday-zim-download.timer
systemctl status doomsday-ai-web.service doomsday-kiwix.service --no-pager
systemctl list-timers --all 'doomsday-*.timer'
```

Local browser addresses (on the Ubuntu host):

- Main GUI: http://127.0.0.1:8765
- ZIM dashboard: http://127.0.0.1:8765/#downloads
- Kiwix: http://127.0.0.1:8080

## 6. ZIMs: select, queue, download, monitor

```bash
sudo doomsday-zims wizard
sudo doomsday-zims browse
sudo doomsday-zims status
sudo systemctl start --no-block doomsday-zim-download.service
sudo systemctl status doomsday-zim-download.service --no-pager
sudo journalctl -fu doomsday-zim-download.service
```

Press Ctrl+C to stop following the logs; this does **not** stop the service. The GUI shows queue entries, downloaded archives and staged byte progress. For direct URL+trusted checksum and exact size, open `/#downloads`. CLI option (replace placeholders):

```bash
sudo doomsday-zims enqueue-url --url 'https://download.kiwix.org/zim/CATEGORY/FILE.zim' --sha256 YOUR_64_HEX_SHA256 --size SIZE_IN_BYTES
sudo systemctl start --no-block doomsday-zim-download.service
```

Use only direct official Kiwix URLs, an independently verified SHA-256 and the real size in bytes.

Other options:

```bash
sudo doomsday-zims backups
sudo doomsday-zims direct-list --source bundle
sudo doomsday-zims direct-check --source queue
sudo doomsday-zims speed-status
sudo doomsday-zims tune-speed
```

## 7. ZFS: find a missing USB drive without formatting

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL,UUID,MOUNTPOINTS
sudo zpool import
sudo zpool status -P
sudo zfs list -o name,mountpoint,mounted
findmnt -M /tank/zims -o SOURCE,FSTYPE,TARGET
```

**Only if `tank` is available for import and not already imported:**

```bash
sudo zpool import -d /dev/disk/by-id tank
```

**Only if the `tank` pool is imported but the existing `tank/zims` dataset is unmounted:**

```bash
sudo zfs mount tank/zims
ls -lah /tank/zims
```

Do **not** use `zpool import -f` while a pool could be in use elsewhere. Do not write into an unmounted `/tank/zims` folder: it could actually be on the OS disk.

## 8. Second ZFS pool, backups, and storage health

```bash
sudo zpool list
sudo zpool status -P
sudo doomsday-zims dual-setup
sudo doomsday-zims optimize
sudo doomsday-zims backups
sudo systemctl start doomsday-stream-replica.service
sudo systemctl status doomsday-stream-replica.service --no-pager
sudo /usr/local/lib/doomsday-vault/status.sh
```

Only enroll a verified *different physical drive / pool*. Enrollment and backup planning require user decisions. The following example requires an existing **mounted** dataset; substitute the correct target:

```bash
sudo /usr/local/lib/doomsday-vault/enroll-external.sh tank/backups
```

Check archive protection and integrity:

```bash
sudo python3 /opt/doomsday-firstboot/app/archive_guard.py status
sudo python3 /opt/doomsday-firstboot/app/archive_guard.py verify
sudo zfs get readonly,copies tank/zims
sudo zpool status
```

A ZFS scrub can be requested on an already known healthy/imported pool, but it generates disk activity; schedule appropriately:

```bash
sudo zpool scrub tank
```

## 9. llama.cpp: detect CPU / CUDA / ROCm / Vulkan

```bash
lscpu | head -25
lspci | grep -Ei 'VGA|3D|Display'
command -v llama-server
llama-server --list-devices
python3 /opt/doomsday-firstboot/app/llama_backend.py
```

Optional driver/tool diagnostics (some may be absent):

```bash
nvidia-smi
rocminfo
vulkaninfo --summary
```

To request a backend persistently for the model-manager service (change `auto` to `cpu`, `cuda`, `rocm` or `vulkan`):

```bash
sudo mkdir -p /etc/systemd/system/doomsday-model-manager.service.d
printf '[Service]\nEnvironment=DOOMSDAY_LLAMA_BACKEND=auto\n' | sudo tee /etc/systemd/system/doomsday-model-manager.service.d/backend.conf
sudo systemctl daemon-reload
```

This does not install GPU drivers, CUDA/ROCm/Vulkan toolchains or GPU-enabled llama.cpp binaries. GPU acceleration requires a compatible build, a working driver and enough memory. Stop/restart the model for changes to take effect.

## 10. Register, download and start chosen AI models

```bash
sudo doomsday-ai status
sudo doomsday-ai catalog
sudo doomsday-ai recommend
sudo doomsday-ai configure
sudo doomsday-ai list
```

Register existing files, replacing example paths:

```bash
sudo doomsday-ai register text /absolute/path/chat.gguf
sudo doomsday-ai register decision /absolute/path/embeddings.gguf
sudo doomsday-ai register vision /absolute/path/vision.gguf --mmproj /absolute/path/mmproj.gguf
```

Start only the models you actually registered:

```bash
sudo doomsday-ai start text
sudo doomsday-ai start decision
sudo doomsday-ai start vision
sudo doomsday-ai status
```

Stop them:

```bash
sudo doomsday-ai stop all
```

The optional model catalog supports user-selected downloads via `sudo doomsday-ai choose` and `sudo doomsday-ai download ROLE`. No model is downloaded merely by selecting Full AI mode. The model scheduler may choose to unload idle models to preserve RAM.

## 11. Search/index existing archives, recovery and hardware checks

```bash
sudo doomsday-index-zims status
sudo doomsday-index-zims run --limit 3500
sudo /opt/doomsday-firstboot/scripts/portability-report.sh
sudo doomsday-recovery-watch status
```

## 12. Quick health checks and error logs

```bash
curl http://127.0.0.1:8765/health
curl -I http://127.0.0.1:8080
sudo doomsday-zims status
sudo doomsday-ai status
sudo zpool status
sudo journalctl -u doomsday-ai-web.service -n 80 --no-pager
sudo journalctl -u doomsday-kiwix.service -n 80 --no-pager
sudo journalctl -u doomsday-zim-download.service -n 80 --no-pager
sudo journalctl -u doomsday-model-manager.service -n 80 --no-pager
```

For failed services:

```bash
systemctl --failed
sudo systemctl restart doomsday-ai-web.service doomsday-kiwix.service
```

Restart only after collecting logs if diagnosing a recurring failure. A service can report `active` while an archive is missing, or a model is not configured; inspect the per-feature status too.

## 13. What **not** to do

- Never format a disk or destroy/recreate a ZFS pool merely to resolve a missing archive.
- Never force-import a pool that could be mounted on another host.
- Never disable checksum verification for ZIM publishing.
- Never assume two ZFS datasets in one physical drive are an independent backup.
- Never run `doomsday-ai stop all` while work is in progress unless intentionally interrupting it.
- Never treat the prototype recovery or USB portability flows as guaranteed without testing on spare hardware.

Related guide: [Kiwix/ZFS/USB errors](kiwix-zfs-usb-port-and-invalid-zim.md).
