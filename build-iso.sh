#!/usr/bin/env bash
# EXPERIMENTAL; requires real Ubuntu Desktop ISO and xorriso, may need boot/BIOS validation.
set -Eeuo pipefail
[[ $# -eq 2 ]] || { echo 'Usage: ./build-iso.sh ubuntu-desktop.iso doomsday-desktop.iso' >&2; exit 2; }
input=$(readlink -f "$1")
output=$(realpath -m "$2")
[[ -f "$input" ]] || { echo 'Input ISO missing' >&2; exit 2; }
[[ "$input" != "$output" ]] || { echo 'Refusing overwrite of source ISO' >&2; exit 2; }
[[ ! -e "$output" ]] || { echo 'Output already exists; select new filename' >&2; exit 2; }
command -v xorriso >/dev/null || { echo 'Install xorriso first: sudo apt install xorriso' >&2; exit 1; }
cd "$(dirname "$(readlink -f "$0")")"
# Preserve original UEFI/BIOS boot structures using xorriso's boot image replay.
xorriso -indev "$input" -outdev "$output" -map app /doomsday-vault/app -map scripts /doomsday-vault/scripts -map systemd /doomsday-vault/systemd -map vendor /doomsday-vault/vendor -map install.sh /doomsday-vault/install.sh -map iso/autoinstall.yaml /autoinstall.yaml -boot_image any replay
printf '\nCreated %s\nValidate boot in a VM before distributing; desktop autoinstall still requires user storage confirmation.\n' "$output"
