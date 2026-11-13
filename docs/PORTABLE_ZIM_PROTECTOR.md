# Portable ZIM Protector — Doomsday Vault

A small, self-contained, **standard-library-only Python tool** for auditing `.zim` archives when an external drive has been moved to another USB port, PC, or Linux installation. It can verify files on Windows, macOS, or Linux **if that OS can actually mount the filesystem containing the ZIM files**. Windows does not natively mount ZFS; connecting a ZFS disk to Windows is not itself enough to access the files.

Protection levels:

1. **SHA-256 audit** — reads all listed ZIMs and compares their contents against an existing, trusted JSON manifest; reports missing, size-mismatch, hash-mismatch, and untracked archives. No file repair is attempted. Publisher authenticity depends on obtaining the checksums from a trusted publisher.
2. **Portable baseline** — can create a first-visit `portable-zim-baseline-v1` JSON. This detects subsequent changes, **not** whether the original file was already corrupt. It can be used for later audits but is refused for ZFS lock operations.
3. **ZFS readonly protection (Linux only)** — an explicitly approved `protect --yes` verifies every ZIM, checks the exact mounted ZFS dataset, rejects OS/boot/pool-root and non-archive datasets, and sets only `readonly=on`. No `readonly=off`, formatting, partitioning, import, or restore command exists in this tool.

Existing DoomsdayOS `app/archive_guard.py` and `zim_manager.py` are still the main protected download/publish pipeline. This tool adds a portable **manual** health check, not a second competing downloader or an automatic repair service.

## Examples on Ubuntu

If your data ZFS dataset is mounted at `/tank/zims` and the installed archive manifest is available:

```bash
# Inspect exactly the mounted dataset. The drive might be sda, sdb or another device:
python3 scripts/portable_zim_protector.py status --archive /tank/zims

# Read-only SHA-256 verification. Reports mismatches but does not modify archives:
python3 scripts/portable_zim_protector.py verify --archive /tank/zims \
  --manifest /var/lib/doomsday-vault/zim-manifest.json

# If the manifest tracks multiple archive pools, filter explicitly when needed:
python3 scripts/portable_zim_protector.py verify --archive /tank/zims \
  --manifest /var/lib/doomsday-vault/zim-manifest.json --dataset tank/zims

# Only after reviewing mounts, checking independent backups, and validating all hashes:
sudo python3 scripts/portable_zim_protector.py protect --archive /tank/zims \
  --manifest /var/lib/doomsday-vault/zim-manifest.json --yes
```

To create an **unauthenticated baseline**, only for a later compare (output path must be new and outside the ZIM archive):

```bash
python3 scripts/portable_zim_protector.py baseline --archive /tank/zims \
  --output "$HOME/zim-baseline.json"
```

Your baseline is **not** a replacement for a Kiwix/publisher checksum.

## Windows / macOS and USB portability

If a ZIM filesystem is already mounted and readable (for example, an ordinary filesystem at `E:\\ZIMs`), run:

```powershell
py -3 scripts\portable_zim_protector.py status --archive 'E:\ZIMs'
py -3 scripts\portable_zim_protector.py verify --archive 'E:\ZIMs' --manifest 'E:\zim-manifest.json'
```

The `protect` action intentionally requires Linux with the actual, **exact** ZFS mountpoint. The tool never attempts to install ZFS drivers, import a pool, or treat a Windows drive letter as a ZFS dataset. Moving the disk can change `/dev/sdX`; this tool uses the ZFS dataset name reported by `findmnt` instead.

## Important safety limits

- If a ZIM fails verification, keep the original file for inspection. Recover only from a separate, independently verified backup. ZFS checksums and scrubs can repair defects only when working redundant copies exist.
- This tool does not guarantee data survival across drive failure, power loss, controller faults, malware, or the lack of a trusted source hash.
- Only completed `.zim` files are verified. Partial downloads (`.part`), unknown extensions, and video/container uploads are not silently accepted.
- The tool does not automatically run in a privileged service. Its `protect` command is intentionally separate from the normal GUI and from LLM-controlled actions.
- For the installed DoomsdayOS protection service and policies, see `app/archive_guard.py`, `systemd/doomsday-zim-protect.service`, and the root `README.md`.

## Verification tests

```bash
python3 -m unittest discover -s tests -p 'test_portable_zim_protector.py' -v
```