# DoomsdayOS — smart disk backup and PC portability

The smart backup layer complements **existing** recovery: local ZFS snapshots/Recovery V2, 14-day native ZFS streaming to a separately enrolled pool, SHA-verified ZIM downloads, and selected per-ZIM copies. It does not replace the existing backup jobs.

## First check without making changes

```bash
sudo doomsday-smart-backup disks    # detected physical disks and enrolled ZFS destinations
sudo doomsday-smart-backup plan     # estimated sizes, available space, local/external system backups
sudo doomsday-smart-backup status   # planned placement and verified backup generation inventory
sudo doomsday-portable-os report    # boot portability check, no changes
systemctl list-timers 'doomsday-*' --all
```

**Smart detection** uses imported ZFS pool GUIDs, exact dataset mounts, physical parent device identities (`zpool status -P` plus `lsblk`), and actual ZFS available space. It will not treat partitions on the same physical disk as independent backups. When a pool cannot be identified, is disconnected, is read-only, lacks its safety reserve, or is not already enrolled, the job **defers** and reports why. It does **not** automatically import, create, partition, wipe, mount, or enroll an unknown disk.

The planner estimates a first full system stream using approximately 115% of the root ZFS pool's allocated blocks plus a small buffer. This is **not an exact prediction**; compressed and incremental streams vary. It separately reports actual available space, a 20 GiB or 10% root safety reserve, mounted external capacity, local snapshot counts, and received external snapshot counts. A snapshot/backup success marker is *not* a verified bootable restore.

## Enroll an independent backup drive

For system ZFS image streaming, keep using the existing explicit enrollment flow:

```bash
sudo /usr/local/lib/doomsday-vault/enroll-external.sh tank/backups
```

Replace `tank/backups` with the **already imported and mounted, verified ZFS dataset** on your independent physical disk. Review the existing enrollment tool's prompts. For ZIM-specific or fleet dataset enrollment, use `sudo doomsday-fleet scan`, `sudo doomsday-fleet enroll POOL` and the existing wizard. The smart data-copy job reuses already enrolled backup datasets; it does not silently commandeer new disks. **Pool GUID pinning is required.** New fleet enrollments record the GUID. Older fleet/firstboot backup locations without a recorded GUID remain visible in the plan as unavailable until explicitly authorized:

```bash
sudo doomsday-smart-backup approve-destination \
  --dataset tank/backups \
  --confirm 'TRUST tank/backups'
```

This approval validates the existing dataset's exact mount, independent physical devices, writable state and current GUID, then persists that identity in the root-owned policy. It does not add, format or mount a disk.

### What is backed up where?

| Copy | Location | Data and safety | Cadence |
| --- | --- | --- | --- |
| Local ZFS recovery | System ZFS pool | System/boot ZFS datasets, snapshots and Recovery V2 editions; same-disk disk failure is **not** covered | Existing fortnightly eligibility and local recovery policy |
| External system ZFS streams | Explicitly enrolled independent pool (`dv-replicas`) | Incremental `zfs send` / `zfs receive`, with available EFI data and layout backup; never `receive -F` | Existing 14-day eligibility |
| Independent smart data backup | Previously enrolled writable ZFS backup dataset with verified physical independence | ZimLens workspace, local research library, and selected DoomsdayOS settings; root-private SHA-256 manifest and verified file copies | 14-day eligibility, daily check |
| External ZIM backups | Existing selected, individually verified archives | Copy only after publisher SHA-256 and independent-pool checks; does not assume there is room for all 39 ZIMs | Existing selected backup policy |
| Smart backup validation | Mounted enrolled external dataset | SHA-256 verification of retained generations, without auto-deleting any | Weekly |

The external backup is **not encrypted by this tool**. It may contain sensitive configuration and project information. Use a properly encrypted independent drive when appropriate; root-only ownership and permissions are not equivalent to encryption. The source folders may contain more data than the configured 50 GiB safety limit. If exceeded, the copy is deferred rather than silently making an incomplete backup.

## Adjust sources and limits

Optional root-owned policy file `/etc/doomsday-vault/smart-backup.json` (not overwritten on upgrades):

```json
{
  "enabled": true,
  "sources": [
    "/var/lib/doomsday-vault/zimlens",
    "/var/lib/doomsday-vault/research",
    "/etc/doomsday-vault"
  ],
  "interval_days": 14,
  "os_reserve_gib": 20,
  "external_reserve_gib": 5,
  "minimum_external_free_percent": 10,
  "max_source_gib": 50,
  "max_files": 100000,
  "auto_restore_missing": false,
  "trusted_destinations": []
}
```

These are *selected file/folder copies*, not full bare-metal OS images. Do not set `/` or an entire arbitrary mount as a source. The program skips symlinks and special files, checks size/mtime around copies, writes into newly created root-private directories inside exact enrolled mountpoints, hashes each copied file, and publishes a generation only after verification. It deliberately does not automatically delete old verified generations. Monitor space and archive retention before drives fill up.

The existing runtime `backup_policy.py` preserves its own 14-day schedule. You can trigger an approved copy immediately:

```bash
sudo systemctl start doomsday-smart-backup.service
sudo doomsday-smart-backup verify
```

If no independently approved destination is eligible, the command reports `deferred` and does not touch another disk.

## Integrity checking and restoration

```bash
sudo doomsday-smart-backup status
sudo doomsday-smart-backup verify
sudo doomsday-recovery-watch plan  # existing safe recovery plan for repeated boot failures
```

To restore a **single missing file**, use the exact generation and path shown by `status`. This is a manual, explicit action:

```bash
sudo doomsday-smart-backup restore-file \
  --generation /tank/backups/.doomsday-smart-backups/MACHINE-ID/GENERATION \
  --path /var/lib/doomsday-vault/zimlens/example.txt \
  --confirm 'RESTORE FILE'
```

Restoring over an existing file additionally requires `--replace --confirm 'REPLACE FILE'`; the replaced file is renamed to a backup sibling instead of deleted. A **nondefault** `auto_restore_missing` option can automatically restore only missing ZimLens workspace files from SHA-verified enrolled backups when `doomsday-smart-backup heal` is explicitly run. It never overwrites existing files, changes EFI, restores the live root system, or guesses whether a changed file is corruption versus a legitimate edit. The existing archive guard's independent-backup ZIM restoration remains separately opt-in.

**Full OS recovery** still requires rescue media, a verified independently stored snapshot/stream, a spare target, bootloader/EFI checks, and human approval. Silent full-disk replacement or root rollback could destroy working data and is intentionally not implemented.

## Portability to another PC / USB port

```bash
doomsday-portable-os report
sudo doomsday-portable-os prepare
```

The audit detects root/boot/EFI status, path-specific `/etc/fstab` entries (e.g. `/dev/sda2`), installed storage modules, ZFS pool GUIDs, and presence of the running kernel's initramfs. `prepare` writes a root-private **inventory and checksums** to `/var/lib/doomsday-vault/portability/portability-report.json` without changing boot settings.

When moving an OS disk between computers, verify UEFI vs BIOS, Secure Boot, storage drivers, GPU fallback (llama.cpp CPU), network adapters, and Wayland desktop portals. Test a spare-disk recovery and boot on the target PC; portability is not guaranteed simply because the ZFS datasets are healthy. Never change the live boot dataset or rebuild EFI automatically because a USB port changed.

## Timers / service checks

```bash
systemctl list-timers --all 'doomsday-smart*' 'doomsday-backup*' 'doomsday-stream*'
systemctl status doomsday-smart-backup.timer doomsday-smart-backup-verify.timer --no-pager
journalctl -u doomsday-smart-backup.service -n 60 --no-pager
```

Data-copy and verification jobs use idle I/O and low CPU weights. Periodic jobs run while the machine is awake and can catch up after reboot through `Persistent=true`; they cannot run while it is suspended or powered off.