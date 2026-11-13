# Doomsday Vault: Verified installation, timer checks and Wayland

This release provides a **verified and reversible code deployment** from a downloaded
source folder into `/opt/doomsday-firstboot`. Archives and personal data are not
moved or deleted. The one-command installer securely removes only its own temporary
extraction directory after setup returns; a folder you downloaded or unpacked
manually is **never removed automatically**.

## Installation

From the latest checked-out/expanded source on Ubuntu:

```bash
sudo bash install.sh --ai
# or for library/search without local inference:
sudo bash install.sh --core
```

For optional GNOME Wayland portal packages on a desktop installation, add
`--desktop`, for example `sudo bash install.sh --ai --desktop`. That flag may
install packages through apt. It does not silently grant remote-control or
screen-sharing permissions.

The installer stages the selected source files in a private sibling directory
under `/opt`, checks SHA-256 for the copied files, moves aside the previous
program tree, promotes the verified copy, runs the installed copy of the
installer, creates an installation-integrity baseline, and removes its temporary
staging and prior program-tree directory *only after successful setup*. On a
failure in installed setup it restores the prior **program files**; it cannot
roll back previously completed operating-system package/service operations.
Runtime state stays in `/var/lib/doomsday-vault` and configuration in
`/etc/doomsday-vault`.

The manually downloaded source folder is deliberately left alone. Never treat a
second copy in the same ZFS pool as an independent physical-disk backup.

## Services and timers

The installer enables the existing local Kiwix and web workspace services, boot
ZIM read-only protection and verified-download restart queue. Existing archive
SHA-256 verification is eligible every 14 days (the timer checks daily and
`backup_policy.py` prevents premature audits), backups are similarly checked for
eligibility, and the added **installed-program integrity** timer runs weekly.
All integrity checks avoid self-repair or deletion of corrupted files.

```bash
systemctl list-timers --all 'doomsday-*'
systemctl status doomsday-ai-web.service doomsday-kiwix.service --no-pager
systemctl status doomsday-zim-protect.service --no-pager
sudo doomsday-zim-portable status --archive /tank/zims
sudo python3 /opt/doomsday-firstboot/scripts/runtime_integrity.py verify
journalctl -u doomsday-runtime-integrity.service -n 80 --no-pager
```

The system-integrity manifest is stored in
`/var/lib/doomsday-vault/installation-manifest.json`, separate from the
publisher-trusted ZIM manifest. The SHA-256 manifest detects unexpected code
changes but isn't a cryptographic *signature* of the upstream release; a
privileged attacker who can modify code and manifest can still defeat it.

## Wayland desktop access

```bash
doomsday-desktop-check
doomsday-desktop-check --json
# Optional refresh from your logged-in GNOME session, WITHOUT sudo:
doomsday-desktop-check --repair-user-portal
```

Run diagnostics inside your normal graphical session. Avoid running user portal
operations as root or over SSH. On a desktop machine, install portal support
with the explicit `--desktop` installer option, or individually review packages
`xdg-desktop-portal`, `xdg-desktop-portal-gnome`, `pipewire`, `wireplumber`.

**Wayland blocks unsanctioned input/screen access by design.** The doctor only
checks availability and can request a restart of an already running user portal.
A real desktop-control broker still needs a supported backend and **explicit
per-session GNOME authorization**. Do not use root Xorg injection as a workaround.

The Kiwix HTTP UI uses `127.0.0.1:8080` locally; the ZimLens workspace uses
`127.0.0.1:8765`. These services do not depend on a live Wayland session.

## Limitations

Hardware validation on your actual laptop, ZFS pools and USB ports still needs
local testing. ZFS checksums catch corruption, but repair is only possible with
redundant valid blocks or an independently verified backup. Nothing here makes
a disconnected sleeping PC run timer jobs; missed calendar jobs are processed
on the next active boot where supported.