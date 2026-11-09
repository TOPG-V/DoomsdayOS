# Doomsday Vault First-Boot V6 — portable, offline and preservation-oriented

**Status: prototype / reference implementation.** Python unit tests and shell syntax checks have passed. No actual Ubuntu Desktop ISO, ZFS import/export cycle, multi-machine boot, or real ZIM download has been tested in this environment. Do **not** consider this a proven disaster-recovery system until you have restored a complete test system onto a spare disk.

This package upgrades V4 with a long-term archive policy, a root-safe read-only ZFS dataset, direct ZFS streaming to an enrolled backup drive, and a read-only portability report. It carries the standalone Recovery V2 package as a dependency. Nothing formats or partitions an unknown disk. The ISO builder is experimental; it requires a separate official Ubuntu Desktop ISO (not supplied).

## What survives a computer or port change?

The installed Ubuntu instance and its ZFS pools retain metadata, snapshots and local files when moved to another compatible x86_64 computer. USB device names (`/dev/sda`, `/dev/sdb`) can change; the recovery component uses ZFS pool GUIDs and matches the enrolled backup's dataset. A hardware scan checks idle, AP-capable Wi-Fi devices and will not steal an active wireless connection. **There is no guarantee of booting on every machine:** UEFI boot restrictions, Secure Boot keys, missing storage controller drivers and insufficient RAM can still block boot. Run the read-only report before a physical move.

## Backup design and limitations

| Current drives | Behavior |
|---|---|
| Only system drive | Local snapshots and Recovery V2 local system editions, with `copies=2` for newly written recovery data and selected system datasets |
| System drive plus **enrolled separate ZFS backup pool** | Same local protection, plus fortnightly direct ZFS `send`/`receive` streaming of all selected system filesystems; independent EFI TAR and disk-layout metadata are also saved if the FAT32 EFI partition is actually mounted |
| Data drive not connected | External streaming skips; local snapshots and recovery operate normally |
| New, unknown USB drive | Not formatted, enrolled, imported by force, or overwritten |

A **single drive cannot back up its own physical existence**. ZFS snapshots and `copies=2` can fix limited corruption; if the disk's electronics or platters fail, both can be lost. The external backups must be on a physically different device. A 500 GB system drive cannot necessarily store every ZIM from a populated 1 TB drive, so users select important ZIMs for secondary backups. Source, backups and EFI files are not silently rolled back following an arbitrary error.

Recovery V2's local editions are file-based compressed ZFS streams; they involve disk reads and writes. For the **external drive**, V6 additionally uses `zfs send | zfs receive` without intermediate full archive files. ZFS verifies incoming streams. Interrupted receives use ZFS resume tokens when supported. V6 disables Recovery V2's duplicate external-copy timer to avoid sending identical data twice. Native incremental backup snapshots are capped at the newest three successful common snapshots. V6's streaming tool covers ZFS filesystems in the detected root/boot pools, excluding local recovery and active ZIM staging; it does not cover unrelated mounted filesystems or zvols; ZIM archive datasets are intentionally excluded from system streams and handled by per-archive choices. EFI, recovery partition layout and Secure Boot settings may need separate attention during a bare-metal restore.

## Immutable ZIM design

**A verified ZIM archive is made read-only at the ZFS dataset level.** This is stronger than simply running `chmod 444` on a file, but it is **not protection against bit rot, malware running as root, or entire-disk failure**.

* `*/zims` — dedicated ZFS archive dataset (`readonly=on`); no partial HTTP downloads; Kiwix reads from here.
* `*/zim-staging` — separate writable ZFS dataset for resumable `.part` files; it can be cleared without touching published archives.
* ZIM selection (individual, filtered, or all catalog results) calculates peak staging-plus-final storage requirements, including the ZFS `copies` overhead and a free-space reserve. Unknown-sized archives cannot be queued through "all" without further inspection.
* Final download SHA-256 is checked against the publisher's sidecar checksum before publishing. The file is copied to a temporary name inside the library, checked again, atomically renamed, and the **library is immediately set back to read-only**. A root-only `flock` protects the short publication window. The verified checksum is saved in `/var/lib/doomsday-vault/zim-manifest.json`.
* Every verified publish takes a ZFS snapshot. A monthly `doomsday-zim-verify.timer` rereads whole ZIMs in idle-I/O mode and compares SHA-256 values. Publisher hashes prove matching bytes, not cryptographic provenance or trustworthy medical content.
* If a verified archive becomes corrupted, ZFS scrubbing and `copies=2` can sometimes transparently repair blocks. **Optional** `auto_restore_verified_zims` permits an archive-only restore from an independently mounted ZFS pool, **only when the backup hashes to the trusted SHA-256**, and it quarantines the damaged version. It never automatically rolls back Ubuntu. If the backup isn't healthy or lacks space, it reports an error without destroying the original.
* Use `zimcheck` as a separate archive-structure check if installed. This complements SHA-256; it is not automatically run by this starter.

`copies=2` is configured for a **new** dedicated ZIM dataset; existing ZIM datasets retain their inherited `copies` setting. Copies are not retroactive: existing data is unaffected. Two copies of compressed ZIM data may consume approximately twice the disk space. If you want to keep an entire large library and cannot afford `copies=2`, use a separate disk replica instead. The system **never silently changes `copies=2` to `copies=1`**.

A power outage while publishing can leave a hidden `.publishing-*.zim` file, and the boot service will reassert readonly. This is deliberate fail-safe behavior: inspect and clean the orphaned staging file manually rather than overwriting it. An interrupted download in the staging dataset is resumed using HTTP ranges when supported; final SHA-256 is always checked.

## Install on an existing Ubuntu Desktop with ZFS

Before running this on an important vault, make a recovery backup and verify you can boot and import the current pools. Existing V4 `*.zim.part` files in the main ZIM dataset must be migrated or removed **manually** before enabling immutable mode. No device formatting is performed by these scripts.

All commands below are **single-line**, ready to paste into a terminal.

1. Download `doomsday-firstboot-v6.zip` into `~/Downloads`.
2. Extract: `cd ~/Downloads && unzip doomsday-firstboot-v6.zip`
3. Install: `cd ~/Downloads/doomsday-firstboot-v6 && sudo ./install.sh`
4. Run configuration wizard (ZIM selection and optional AP-capable Wi-Fi hotspot): `sudo doomsday-setup`
5. Check ZIM archive state: `sudo python3 /opt/doomsday-firstboot/app/archive_guard.py status`
6. Check recovery system: `sudo /usr/local/lib/doomsday-vault/status.sh`
7. View auto-start timers: `systemctl list-timers --all 'doomsday-*.timer'`
8. Check portability: `sudo /opt/doomsday-firstboot/scripts/portability-report.sh`

The installer requires Internet access for downloading missing Ubuntu packages, unless they're already installed. It does not download an AI model. Kiwix runs on the local network when it has at least one verified ZIM file. The separate Doomsday Search web UI is **not bundled in V6** and must be installed later. The native Kiwix service is configured to listen on port `8080` of trusted network interfaces; do not expose it directly to the public Internet.

If your existing second drive has a ZFS dataset `tank/zims` mounted at `/tank/zims`, V6 uses it and creates `/tank/zim-staging`. If there is no such dedicated pool, it creates `DOOMSDAY_ZIMS` and `DOOMSDAY_ZIM_STAGING` child datasets on the *detected system pool* at `/srv/doomsday-zims` and `/srv/doomsday-zim-staging`. It does not automatically migrate archives between drives. Root/boot pools are **never made read-only** by the archive guard.

### Enroll any manufacturer's already-prepared backup disk

`sudo /usr/local/lib/doomsday-vault/enroll-external.sh tank/backups` is an **example**, not a required hardware name. Replace with the existing mounted ZFS backup dataset selected by the user. Enrollment stores its pool GUID and checks that it isn't the system or boot pool. It never formats a new disk. Confirm it is a **different physical drive** before relying on it as a backup destination.

Manually start the native streamed backup: `sudo systemctl start doomsday-stream-replica.service`

Check the service result: `systemctl status doomsday-stream-replica.service --no-pager`

Confirm received datasets and snapshots: `sudo zfs list -r -t filesystem,snapshot tank/backups/dv-replicas` (replace `tank/backups` with your enrolled dataset).

Local recovery editions continue to use Recovery V2's 14-day eligibility timer. Streaming to the external pool is separate, every 14 days when eligible and automatic *after enrollment*. It does **not** imply that the OS can reimage itself while running or recover a physically dead disk without another bootable medium.

### Check ZIM integrity

Force a read-only checksum audit: `sudo python3 /opt/doomsday-firstboot/app/archive_guard.py verify`

Inspect scrub status: `sudo zpool status`

Manual scrub example for an existing pool: `sudo zpool scrub tank` (replace with your verified pool name).

Check the library's actual read-only flag: `sudo zfs get readonly,copies tank/zims` (replace dataset if running on one drive).

### Restore after a failure

* For individual ZIM corruption, verify the independent backup's checksum and recover **only** the affected archive. The optional auto-restorer requires two different mounted ZFS pools and explicit opt-in at setup.
* For ZFS pool corruption, an OpenZFS scrub can repair blocks only when another good redundant copy is present; check `zpool status` first.
* For OS boot failure, boot a trusted recovery environment, import the surviving pools read-only for inspection, and restore from a validated backup to a **spare disk**. You must restore disk layout/EFI boot chain and verify root pool mount settings before rebooting. V6 does **not** automatically overwrite active ZFS roots or EFI partitions on boot.
* After recovery, validate the system and a sample of archived ZIMs. Retain one backup drive offline wherever possible to reduce theft, power, ransomware and correlated-failure risk.

## Hardware switching

Shut down Ubuntu completely before moving its system disk. For a data-only drive, stop applications using it, then `sudo zpool export POOL` before disconnecting. The import helper uses enrolled pool GUIDs and `/dev/disk/by-id`; it does not use `zpool import -f`. The Wi-Fi rechecker runs periodically, starts an SSID only when the user opted in and an *idle* AP-capable interface exists, and doesn't steal an active wireless interface. NetworkManager manages the hotspot's local DHCP. After moving computers, run the portability report. Confirm firmware USB boot, UEFI fallback files, available CPU instructions, and Secure Boot policy. Incompatible firmware or missing drivers still requires human intervention.

## Build an open-source installation ISO (experimental)

`./build-iso.sh /path/to/ubuntu-desktop.iso /path/to/doomsday-desktop.iso` injects this first-boot payload and a sample interactive `autoinstall.yaml` into a **locally supplied** Ubuntu Desktop ISO. `xorriso` must be installed. The script attempts to preserve the base ISO's boot configuration via `-boot_image any replay`.

**A generated ISO is not proven bootable.** Test it in an isolated VM with a disposable virtual disk first. Installers are highly version-dependent; check that the `autoinstall.yaml` is actually recognized and that its late-commands stage `doomsday-bootstrap.service` in the target. The sample installer requires *interactive storage selection* and does not silently erase disks. Test BIOS/UEFI/Secure Boot separately before publicly distributing it.

This kit contains original Python/Bash service code and the MIT-licensed Recovery V2 component. Before redistributing an Ubuntu installer or ZIM archives, check Ubuntu trademark/license policies and each source archive's license and redistribution terms. **No Ubuntu ISO and no ZIM data are bundled here.**

## Verification status

Automated tests cover: archive checksum, refusing to make the running OS root read-only, rejecting corrupt staged files before publishing, restoring the read-only flag after publishing, and URL validation. Shell scripts pass `bash -n`. Real bare-metal and power-interruption tests remain outstanding. Public releases should also undergo static security review and restoration tests. This starter is not a certified medical or safety device.


## V6 — 14-day incremental policy and example screens

![Illustration of the setup wizard](docs/screens/setup.svg)
![Illustration of selecting individual ZIM files](docs/screens/zims.svg)
![Illustration of backups and integrity verification](docs/screens/backups.svg)

These are **illustrative examples**, not actual application screenshots or proof of successful recovery.

* **Install once; run without Downloads.** The installer copies executable code to `/opt/doomsday-firstboot` and Recovery V2 tools to `/usr/local/lib/doomsday-vault`. After a successful installation, removing the extracted `Downloads` folder will not remove installed services. Keep the downloaded ZIP only as an offline installer copy if desired.
* **Fortnightly local recovery.** The Recovery V2 local full-edition backup runs when eligible (default 14 days), under `Nice=19` and `IOSchedulingClass=idle`. These local compressed backups **do read and write the same disk**; a ZFS snapshot is more efficient, but the local edition provides an independent file copy on surviving sectors.
* **Fortnightly incremental external replication.** First success streams the full set of selected system ZFS filesystems. Later runs transmit changed ZFS blocks only, using `zfs send -i`, with `stream_mib_per_second=16` by default and 1 MiB bounded chunks. Backups are only marked successful when an enrolled external drive is mounted. `zfs receive -s` allows interrupted-stream continuation when OpenZFS provides a resume token. The system does **not** overwrite external datasets with `receive -F`.
* **14-day ZIM audits.** SHA-256 revalidation checks existing manifest entries and can perform an **opt-in** restore of an individual damaged ZIM only from an independent, verified backup. ZFS scrub timers are separate and should be checked with `systemctl list-timers 'zfs-scrub*'`.
* **ZIM backups are optional per archive.** Select which ZIMs to replicate at setup after the free-space check. There is no universal guarantee that all ZIMs will fit in both drives. A copy on the same pool is not an independent backup, and neither drive can back up its own complete mechanical failure.
* **The ISO builder is experimental** and requires an official Ubuntu installer ISO. No bootable ISO or AI model is included. **Validate first-boot and restores before adding AI or making public releases.**

### One-line commands after installation

```bash
sudo doomsday-setup
sudo doomsday-zims status
sudo python3 /opt/doomsday-firstboot/app/archive_guard.py status
sudo systemctl start doomsday-stream-replica.service
sudo /usr/local/lib/doomsday-vault/status.sh
systemctl list-timers --all 'doomsday-*.timer'
sudo journalctl -u doomsday-stream-replica.service -n 50 --no-pager
```

### 14-day rate limit

The administrator can change `stream_mib_per_second` in `/etc/doomsday-vault/backup-policy.json`; a value of `0` removes the byte-rate limit. For an HDD, start with 8–16 MiB/s to limit contention. The scheduler checks daily; if the 14-day period has passed but CPU load is too high or the external drive is absent, it defers the job. The next successful run marks the interval. **The fallback does not guarantee an exact 14-day time under indefinite high load or disconnection.**

### What isn't complete

* Automatic full bare-metal rollback on every detected error would be dangerous and is **not enabled**. EFI recovery is captured as verified files but actual EFI repair must be invoked from a recovery environment after diagnosis.
* The backup policy covers system/boot ZFS filesystems and selected ZIM copies; it is not a forensic disk image of every partition or attached drive. Data stored only on an external ZFS pool requires a third independent destination for full-drive failure protection.
* Archive catalog compatibility depends on Kiwix's live feed and SHA-256 sidecars; test the first real download. Mocked tests cannot guarantee publisher endpoints stay unchanged.

## Three first-boot download packages (original ZIM collection)

The **first-boot** `sudo doomsday-setup` wizard and `sudo doomsday-zims wizard` now offer **exactly three download choices**. This is a fixed collection based on the original `Zim(1).zip` torrent list, not a download of the entire Kiwix catalog:

| Choice | Contents | Source archive size (decimal GB) |
|---|---|---:|
| `1` **Full** | All 39 original ZIM archives, including TED Talks | **575.0 GB** (approximately the requested 577 GB) |
| `2` **Partial** | The same original collection **except TED Talks**, with all 38 other ZIMs unchanged | **483.8 GB** |
| `3` **Wikipedia Only** | Only full English Wikipedia (`wikipedia_en_all_maxi_2026-08.zim`) | **127.4 GB** |

**Why Partial isn't ~400 GB:** TED Talks is 91.18 GB in the provided torrent metadata, so 575.0 GB minus 91.18 GB = 483.8 GB. Removing more material to hit 400 GB would contradict keeping the rest of the full collection. These figures exclude ZFS `copies=2` overhead, the largest temporary staging file, free-space reserve and optional independent backups. The installer checks actual disk capacity before enqueueing and will refuse oversized jobs rather than fill the system disk. It never formats a disk to make room.

The bundled manifest contains exact official Kiwix URLs, byte sizes and the 38 SHA-256 hashes supplied alongside the torrents. **One archive (`devdocs_en_python_2026-10.zim`) had no supplied SHA-256 sidecar:** it will require fetching a SHA-256 sidecar from the official server at download time; if unavailable, it is kept for retry rather than installed unverified. Preset contents are frozen at these 2025–2026 archive versions; the files may be removed or moved by their publisher. No large content is bundled in GitHub.

Press **1**, **2** or **3**, review the contents and space estimate, select which ZIMs to back up if a separate drive is enrolled, then type `YES` to queue. The download system uses the existing timer, SHA-256 check, resumable staging, and read-only published library. It does not begin the transfer merely by choosing a number.

To search the broader live Kiwix catalog and choose individual ZIMs, use `sudo doomsday-zims browse`. This keeps per-item selection available without adding a fourth option to the first-boot package menu.

### Upgrade an already-installed V6 / V6.1 without reinstalling or changing ZFS

Run the following one-line command while connected to the Internet (uses both new files from this repository, checks Python and JSON syntax before installing them):

```bash
t="$(mktemp -d)" && curl -fsSL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/app/zim_manager.py -o "$t/zim_manager.py" && curl -fsSL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/app/bundle_manifest.json -o "$t/bundle_manifest.json" && python3 -m py_compile "$t/zim_manager.py" && python3 -m json.tool "$t/bundle_manifest.json" >/dev/null && sudo install -m 0644 "$t/zim_manager.py" /opt/doomsday-firstboot/app/zim_manager.py && sudo install -m 0644 "$t/bundle_manifest.json" /opt/doomsday-firstboot/app/bundle_manifest.json && rm -rf "$t" && sudo doomsday-zims wizard
```

A fresh clone and `sudo ./install.sh` also include the new manifest automatically. Downloaded ZIM files and previously queued entries are not deleted by this update.

![Illustration of the three preset menu](docs/screens/zims.svg)

## Hotfix 2026-11-09 — Empty ZIM catalog in V6

**Symptom:** After `sudo doomsday-setup`, pressing Enter at the catalog filter prints `No matching downloadable ZIMs`. V6's parser recognized direct `.zim` acquisition links only. Some Kiwix OPDS entries use `.zim.meta4` URLs even when advertising the MIME type `application/x-zim`. This meant supported books were skipped. The hotfix normalizes official `.zim.meta4` acquisition references into direct `.zim` download URLs, improves paginated filtering, and treats `all` as an alias for browsing at the initial title-filter prompt. Selecting *all* downloads is still a separate confirmation and storage-capacity check.

To update **an already installed** V6 without touching ZFS pools, paste this **one line** into Ubuntu Terminal when connected to the Internet:

```bash
f="$(mktemp --suffix=.py)" && curl -fsSL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/app/zim_manager.py -o "$f" && python3 -m py_compile "$f" && sudo install -m 0644 "$f" /opt/doomsday-firstboot/app/zim_manager.py && rm -f "$f" && sudo doomsday-zims wizard
```

When you reach `Search titles`, press Enter to browse up to the first 100 ZIMs, or type a keyword such as `Wikipedia`. Then pick the listed numbers or type `all` at the **selection** prompt. No download begins before the explicit `YES` confirmation. The hotfix is locally tested against synthetic OPDS samples, but a successful live catalog fetch still depends on network and Kiwix endpoint availability.

## V6.3 — clear storage estimates and optional ZIM-only space-saving mode

The three download presets remain **Full** (575.0 GB, 39 archives), **Partial** (483.8 GB, 38 archives; only TED Talks removed), and **Wikipedia Only** (127.4 GB, one archive). This is the *download payload*, **not** required disk capacity.

For the Full preset, the V6 preservation default of ZFS `copies=2` requires about 1,071 GiB for the final archives alone. The largest source archive (Gutenberg, 221.25 GB) needs up to another 412 GiB of two-copy staging while being verified and published. Including the 3 GiB reserve gives approximately **1,486 GiB of peak pool usage (1.60 TB)**. This is why a VM with 888 GiB of free pool space refuses to queue Full.

**New behavior:** The wizard prints actual download bytes, archive duplication, temporary staging, peak capacity, and free capacity separately. If two-copy storage does not fit but single-copy storage does, the wizard optionally offers **SINGLE COPY** mode. It requires the literal `SINGLE COPY` consent and the usual final `YES`. Unless BOTH are provided, no ZFS copies property is changed and nothing is queued. It validates the two mounted dedicated datasets and explicitly refuses to alter the running OS root, boot datasets, or pool roots. Existing blocks are **not** converted; `copies=1` affects only newly written data.

For Full, `copies=1` on the dedicated archive and staging datasets lowers the estimated peak to around **745 GiB**, without changing download bytes. This is **less redundancy against corruption**, not a free space compression trick. Do not rely on it as protection against hardware failure; an independent, checksum-verified second-device backup is strongly recommended. This estimate is not a guarantee if other applications, snapshots or ZFS metadata use additional space.

To use the new wizard on an existing V6 machine, install the updated V6.3 ZIP or replace `app/zim_manager.py` and `app/bundle_manifest.json` in `/opt/doomsday-firstboot/app` with the files from GitHub, then run `sudo doomsday-zims wizard`. Do **not** rerun the installer from an older, extracted V6 folder, which would replace the new code with old code. The status remains available through `sudo doomsday-zims status`.


## V6.4 — OS-folder ZIM backups and per-ZIM exclusions

Use `sudo doomsday-zims wizard` (or `sudo doomsday-setup` on first boot). The three fixed Full/Partial/Wikipedia download presets are unchanged. After a preset and the original ZIM capacity check, choose where to keep **optional backup copies**:

* `1` — keep the existing directory (or none).
* `2` — choose a folder on the OS drive; press Enter to use `/var/lib/doomsday-vault/zim-backups`, or type another absolute path on the OS filesystem/pool. The folder is created **only after** final `YES` confirmation.
* `3` — choose an existing mounted directory on another storage volume.
* `4` — no extra ZIM backups.

For a selected destination, the wizard lists every selected ZIM with its number. At **Back up which ZIMs?**, enter `all`, `none`, comma-separated numbers such as `1,3,34`, or `exclude 13,29,33` to back up all except those entries. If there is **INSUFFICIENT BACKUP SPACE**, it shows needed space, space available for additional backups, and shortage, then prompts for numbers to exclude. It does **not** silently drop backups. Every excluded ZIM **still downloads to the main protected archive**; only its optional backup copy is omitted. Previous backup copies are never deleted automatically by exclusions.

The planner accounts for original archive and staging space plus requested additional ZIM backups on the **same** ZFS pool, and reserves an extra 20 GiB for an OS-folder destination to reduce the chance of filling the boot drive. For already published ZIM files matching catalog sizes, it estimates only the remaining new downloads (their SHA-256 is still checked by the worker). If original storage is insufficient at ZFS `copies=2`, the separate opt-in `SINGLE COPY` flow from V6.3 remains; excluding additional backups does **not** turn off `copies=2` in the original archive.

**Critical tradeoff:** A backup folder on the OS drive is not independent if the OS disk fails; snapshots and copies on the same physical disk do not prevent total disk-loss. Files in OS folders may also be included in ordinary root filesystem snapshots, increasing space requirements. For disaster recovery, retain at least one physically separate, verified copy. Selecting the OS folder disables unattended ZIM auto-repair from that folder. The software never formats a disk or deletes excluded ZIMs.

Existing installed archives can be reselected for backup without downloading them again. A changed backup selection updates queued archive metadata, and selecting an already-completed archive for backup schedules it for another checksum check and copy if necessary. View status and the backup selection with `sudo doomsday-zims status`. File paths and mount identifiers are verified again before copying; an unplugged disk is never silently replaced by writing into its empty mount directory.
