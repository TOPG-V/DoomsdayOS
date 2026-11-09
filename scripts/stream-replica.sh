#!/usr/bin/env bash
# EXPERIMENTAL ZFS native streaming backup to an explicitly enrolled, independent pool.
# Never force-imports, never uses receive -F, never rewrites the live system.
set -Eeuo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
source /usr/local/lib/doomsday-vault/common.sh
exec 7>/run/lock/doomsday-stream-replica.lock
flock -n 7 || { echo 'Streaming already in progress'; exit 0; }
if [[ $EXTERNAL_POOL == none || -z ${EXTERNAL_DATASET:-} ]]; then echo 'No enrolled external backup pool; keeping local backups only'; exit 0; fi
external=$(find_external_pool) || { echo 'Enrolled backup not connected; local backup remains'; exit 0; }
check_external_dir "$external"
[[ $external != "$LOCAL_POOL" && $external != "$BOOT_POOL" ]] || fail 'Backup destination must be a separate pool'
[[ $(zpool get -H -o value guid "$external") == "$EXTERNAL_GUID" ]] || fail 'Backup pool identity changed; stop'
root_target="$EXTERNAL_DATASET/dv-replicas"
if ! zfs list -H "$root_target" >/dev/null 2>&1; then zfs create -o mountpoint=none -o canmount=off "$root_target"; fi
stamp="dvstream_$(date -u +%Y%m%dT%H%M%SZ)"
# Streaming is memory-bounded and rate-limited; IO scheduling is controlled by systemd.
RATE=$(python3 -c 'import sys;sys.path.insert(0,"/opt/doomsday-firstboot/app");from backup_policy import settings;print(settings()["stream_mib_per_second"])')
stream_limited() { python3 /opt/doomsday-firstboot/app/throttle_stream.py --mibps "$RATE"; }

# Archive ZIMs are managed individually by the space-aware ZIM backup selector.
# Replicating their whole dataset here would silently defeat the user's per-ZIM choices.
archive_ds=$(python3 -c 'import json;from pathlib import Path;p=Path("/etc/doomsday-vault/firstboot.json");print(json.loads(p.read_text()).get("zim_dataset", "") if p.is_file() else "")' 2>/dev/null || true)
mapfile -t sources < <(list_source_datasets)
((${#sources[@]})) || fail 'No source datasets were selected'
for source_ds in "${sources[@]}"; do
  if [[ -n $archive_ds && ( $source_ds == "$archive_ds" || $source_ds == "$archive_ds"/* ) ]]; then
    log "Skipping ZIM archive $source_ds; individual ZIM backups follow capacity policy"
    continue
  fi
  # Downloads are transient; they can be resumed via HTTP and need not be included.
  [[ $source_ds == */DOOMSDAY_ZIM_STAGING || $source_ds == */DOOMSDAY_ZIM_STAGING/* ]] && continue
  key=$(printf '%s' "$source_ds" | sha256sum | cut -c 1-20)
  target="$root_target/fs-${key}"
  if zfs list -H -o name "$target" >/dev/null 2>&1; then
    token=$(zfs get -H -o value receive_resume_token "$target" 2>/dev/null || echo -)
    if [[ $token != - && -n $token ]]; then
      log "Resuming interrupted receive for $source_ds"
      zfs send -t "$token" | stream_limited | zfs receive -u -s "$target"
      log "Resumed; next scheduled run will create the next incremental edition"
      continue
    fi
  fi
  zfs snapshot "$source_ds@$stamp"
  if zfs list -H -o name "$target" >/dev/null 2>&1; then
    # A pre-existing target must have a recent common snapshot. Refuse a destructive reset.
    common=''
    while IFS= read -r snap; do
      tag=${snap#"$source_ds"@}
      [[ $tag == dvstream_* ]] || continue
      if zfs list -H -t snapshot "$target@$tag" >/dev/null 2>&1; then common=$tag; fi
    done < <(zfs list -H -t snapshot -o name -s creation -r "$source_ds" | grep -F "$source_ds@" || true)
    if [[ -z $common ]]; then log "No common snapshot for $source_ds; manual recovery needed, refusing overwrite"; continue; fi
    log "Incremental stream: $source_ds@$common -> $source_ds@$stamp"
    zfs send -i "$source_ds@$common" "$source_ds@$stamp" | stream_limited | zfs receive -u -s -o readonly=on "$target"
  else
    log "Full stream: $source_ds@$stamp -> $target"
    zfs send -p "$source_ds@$stamp" | stream_limited | zfs receive -u -s -o mountpoint=none -o readonly=on "$target"
  fi
  zfs list -H -t snapshot "$target@$stamp" >/dev/null || fail "Received snapshot missing for $source_ds"
  log "ZFS stream received with integrity validation; snapshot present at $target@$stamp (restore test still required)"
  # Keep the newest 3 common backup snapshots so weekly streams do not grow forever.
  # Never remove the last common snapshot; without it incrementals would require a full copy.
  mapfile -t common_tags < <(zfs list -H -t snapshot -o name -s creation -r "$source_ds" | grep -F "$source_ds@dvstream_" | while IFS= read -r snap; do tag=${snap#"$source_ds"@}; zfs list -H -t snapshot "$target@$tag" >/dev/null 2>&1 && printf '%s\n' "$tag"; done)
  if ((${#common_tags[@]}>3)); then
    for old in "${common_tags[@]:0:${#common_tags[@]}-3}"; do
      zfs destroy "$target@$old" || log "Could not prune external snapshot $target@$old"
      zfs destroy "$source_ds@$old" || log "Could not prune local stream snapshot $source_ds@$old"
    done
  fi
done
# EFI is NOT part of ZFS snapshots. Save it separately only when actually mounted as FAT.
if [[ $(stat -f -c %T /boot/efi 2>/dev/null || true) == msdos && -d /boot/efi/EFI ]]; then
  etmp=$(mktemp -d "$EXTERNAL_DIR/.efi-pending-XXXXXXXX")
  chmod 0700 "$etmp"
  tar -C /boot/efi -cf "$etmp/EFI.tar" EFI
  root_device=$(findmnt -n -o SOURCE /boot/efi)
  if [[ $root_device == /dev/* ]]; then
    disk=$(lsblk -no PKNAME "$root_device" | head -1)
    if [[ -n $disk && -b /dev/$disk ]]; then sfdisk -d "/dev/$disk" > "$etmp/DISK-LAYOUT.sfdisk" || true; fi
  fi
  (cd "$etmp" && sha256sum *.tar > SHA256SUMS && sha256sum -c --quiet SHA256SUMS)
  efinal="$EXTERNAL_DIR/efi-${stamp#dvstream_}"
  [[ ! -e $efinal ]] || fail 'EFI destination already exists'
  mv "$etmp" "$efinal"
  log "Saved checksummed EFI and available partition layout to $efinal"
else
  log 'WARNING: EFI partition not mounted as FAT; snapshot replication succeeded, but EFI backup skipped'
fi
log 'Streaming backup completed. Test restoring onto a spare disk before depending on it.'
