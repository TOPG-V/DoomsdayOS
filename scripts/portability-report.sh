#!/usr/bin/env bash
# Read-only portability report; never repairs bootloader or force-imports ZFS.
set -uo pipefail
printf '=== Doomsday Vault portability report ===\n'
printf 'OS root: '; findmnt -n -o SOURCE,FSTYPE / || true
printf 'Current boot: '; findmnt -n -o SOURCE,FSTYPE /boot || true
printf 'EFI mount: '; findmnt -M /boot/efi -n -o SOURCE,FSTYPE || true
printf 'EFI filesystem visible now: '; stat -f -c %T /boot/efi || true
if [[ -f /boot/efi/EFI/BOOT/BOOTX64.EFI ]]; then echo 'UEFI removable fallback: present'; else echo 'UEFI removable fallback: missing; manual inspection needed'; fi
printf 'System ZFS pools:\n'; zpool list -o name,guid,health || true
printf 'Current detected disks:\n'; lsblk -d -o NAME,SIZE,MODEL,TRAN || true
printf 'Boot parameter retry: '; (cat /proc/cmdline | grep -o 'rd.retry=[0-9]*' || true)
printf 'Available AP Wi-Fi hardware:\n'; if command -v iw >/dev/null; then iw dev || true; else echo 'iw unavailable'; fi
printf '\nPortable boot is not guaranteed across Secure Boot and firmware variations. No settings changed.\n'
