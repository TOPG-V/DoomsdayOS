#!/usr/bin/env bash
set -Eeuo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run: sudo ./install.sh'; exit 1; }
cd "$(dirname "$(readlink -f "$0")")"
if [[ ${1:-} == --stage-only ]]; then
  # ISO late-command uses this mode; it only places a boot-time safe installer unit.
  install -d -m 0755 /opt/doomsday-firstboot /etc/systemd/system /var/lib/doomsday-vault
  if [[ $(pwd -P) != /opt/doomsday-firstboot ]]; then cp -a app systemd scripts vendor install.sh /opt/doomsday-firstboot/; fi
  install -m 0644 systemd/doomsday-bootstrap.service /etc/systemd/system/
  systemctl enable doomsday-bootstrap.service 2>/dev/null || true
  exit 0
fi
[[ $# -eq 0 || ${1:-} == --bootstrap ]] || { echo 'Usage: sudo ./install.sh [--bootstrap|--stage-only]'; exit 2; }
install -d -m 0755 /opt/doomsday-firstboot
if [[ $(pwd -P) != /opt/doomsday-firstboot ]]; then cp -a app systemd scripts vendor install.sh /opt/doomsday-firstboot/; fi
install -d -m 0700 /etc/doomsday-vault /var/lib/doomsday-vault
install -d -m 0755 /usr/share/doomsday-vault
# Reuse the existing V2 backup/recovery system; don't format disks or adopt unknown drives.
if [[ $(findmnt -n -o FSTYPE /) == zfs ]]; then
  if command -v zfs >/dev/null && command -v zpool >/dev/null; then
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    unzip -q -o vendor/recovery-v2.zip -d "$tmp"
    if ! (cd "$tmp/doomsday-vault-recovery-v2" && bash install.sh); then
      echo 'Recovery services not ready. Install can be retried safely after dependencies are available.' >&2
      exit 1
    fi
  else
    echo 'ZFS utilities unavailable; retry later' >&2; exit 1
  fi
else
  echo 'Non-ZFS root. ZFS-specific recovery package not installed; storage remains untouched.'
fi
# Create dedicated archival and temporary filesystems; never make OS root readonly.
if [[ $(findmnt -n -o FSTYPE /) == zfs ]]; then
  bash scripts/prepare-zims.sh || { echo 'Immutable ZIM configuration failed; inspect and retry manually.' >&2; exit 1; }
  # Keep local weekly recovery editions; use native ZFS sends for enrolled external drives.
  systemctl disable --now doomsday-external-sync.timer 2>/dev/null || true
fi
if command -v apt-get >/dev/null; then apt-get install -y --no-install-recommends iw kiwix-tools aria2 python3-pil python3-libzim || echo 'Kiwix/iw/aria2 optional packages unavailable (install later when Internet is available)' >&2; fi
for f in systemd/doomsday-zim-index.* systemd/doomsday-recovery-watch-*.service systemd/doomsday-recovery-watch-*.timer systemd/doomsday-osrm.service systemd/doomsday-osrm-walk.service systemd/doomsday-ai-web.service systemd/doomsday-llama-*.service systemd/doomsday-image-index.* systemd/doomsday-zim-download.* systemd/doomsday-hardware-recheck.* systemd/doomsday-kiwix.service systemd/doomsday-zim-protect.service systemd/doomsday-zim-verify.service systemd/doomsday-zim-verify.timer systemd/doomsday-stream-replica.service systemd/doomsday-stream-replica.timer; do install -m 0644 "$f" /etc/systemd/system/; done
# Fortnightly backup policy: preserve admin overrides and use systemd low-I/O priority.
if [[ ! -f /etc/doomsday-vault/backup-policy.json ]]; then
  install -m 0600 /dev/null /etc/doomsday-vault/backup-policy.json
  printf '%s\n' '{"interval_days":14,"stream_mib_per_second":16,"max_load_per_core":1.5,"min_system_free_gib":20,"zim_backups":"selected"}' > /etc/doomsday-vault/backup-policy.json
fi
install -d -m 0755 /etc/systemd/system/doomsday-backup.service.d
install -m 0644 systemd/doomsday-backup-policy.conf /etc/systemd/system/doomsday-backup.service.d/policy.conf
install -m 0644 systemd/doomsday-backup.timer /etc/systemd/system/doomsday-backup.timer
install -m 0755 app/doomsday-firstboot-launch /usr/local/bin/doomsday-firstboot-launch
install -m 0644 app/doomsday-firstboot.desktop /etc/xdg/autostart/doomsday-firstboot.desktop
cat > /usr/local/bin/doomsday-setup <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 firstboot.py
LAUNCH
cat > /usr/local/bin/doomsday-zims <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 zim_manager.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-wifi <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 wifi_manager.py "$@"
LAUNCH
chmod 0755 /usr/local/bin/doomsday-setup /usr/local/bin/doomsday-zims /usr/local/bin/doomsday-wifi
cat > /usr/local/bin/doomsday-index-text <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 local_index.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-image-index <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 media_index.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-ai <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 llama_runtime.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-recovery <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 recovery_advisor.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-index-zims <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 zim_index.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-enrich-media <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 media_enrich.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-maps <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 offline_maps.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-trip <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 trip_planner.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-recovery-watch <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 recovery_watch.py "$@"
LAUNCH
chmod 0755 /usr/local/bin/doomsday-index-zims /usr/local/bin/doomsday-enrich-media /usr/local/bin/doomsday-maps /usr/local/bin/doomsday-trip /usr/local/bin/doomsday-recovery-watch
chmod 0755 /usr/local/bin/doomsday-ai /usr/local/bin/doomsday-recovery /usr/local/bin/doomsday-image-index /usr/local/bin/doomsday-index-text
systemctl daemon-reload
if command -v zfs >/dev/null && zfs list -H tank/zims >/dev/null 2>&1; then
  if systemctl list-unit-files 'zfs-scrub-monthly@.timer' --no-legend 2>/dev/null | grep -q 'zfs-scrub-monthly@.timer'; then systemctl enable --now zfs-scrub-monthly@tank.timer || true; fi
fi
systemctl enable --now doomsday-backup.timer doomsday-zim-download.timer doomsday-hardware-recheck.timer doomsday-zim-verify.timer doomsday-stream-replica.timer doomsday-zim-protect.service doomsday-kiwix.service doomsday-ai-web.service doomsday-image-index.timer doomsday-zim-index.timer doomsday-recovery-watch-start.timer doomsday-recovery-watch-healthy.timer
systemctl disable doomsday-bootstrap.service 2>/dev/null || true
touch /var/lib/doomsday-vault/bootstrap-complete
printf '\nDoomsday first-boot services installed. No disk formatting performed.\n'
echo 'Interactive ZIM/Wi-Fi wizard: sudo doomsday-setup'
echo 'ZIM queue: sudo doomsday-zims status'
echo 'Change selected ZIM backups: sudo doomsday-zims backups'
echo 'Optional separate ZFS pool: sudo doomsday-zims dual-setup && sudo doomsday-zims optimize'
echo 'Offline knowledge browser: http://localhost:8080 from Ubuntu'
echo 'Local AI web UI: http://localhost:8765 (requires local llama.cpp models for AI generation)'
echo 'AI model setup: sudo doomsday-ai register text /path/to/model.gguf'
echo 'Optional idle image indexing: sudo doomsday-image-index enroll /path/to/pictures'
echo 'Optional custom torrent: sudo doomsday-zims import-torrent --torrent-file /path/to/file.torrent --sha256 EXPECTED_SHA256'
echo 'Read-only OS recovery advisor: sudo doomsday-recovery report'
echo 'Immutable archive: sudo python3 /opt/doomsday-firstboot/app/archive_guard.py status'
echo 'Drive backup enrollment: sudo /usr/local/lib/doomsday-vault/enroll-external.sh POOL/DATASET'
echo 'Safe portability report: sudo /opt/doomsday-firstboot/scripts/portability-report.sh'
echo 'External direct-stream backup: sudo systemctl start doomsday-stream-replica.service'
echo 'Backup scheduling: local and external backups become eligible every 14 days, and run at idle I/O priority'

echo 'ZIM article + media index: sudo doomsday-index-zims run / status (incremental at idle)'
echo 'Optional media captions/transcripts: doomsday-enrich-media --help'
echo 'Offline map enrolment: sudo doomsday-maps tiles /absolute/path/region.mbtiles'
echo 'Offline road routes: sudo doomsday-maps router /absolute/path/roads.osrm && sudo systemctl enable --now doomsday-osrm.service'
echo 'Offline walk routes: sudo doomsday-maps walk-router /absolute/path/walking.osrm && sudo systemctl enable --now doomsday-osrm-walk.service'
echo 'Strict offroad trails: sudo doomsday-maps offroad /absolute/path/trails-and-roads.geojson'
echo 'Optional MapLibre vector renderer: sudo /opt/doomsday-firstboot/scripts/setup-maplibre.sh (online once)'
echo 'Road/trail planner: sudo doomsday-trip status'
echo 'Boot failure watcher: sudo doomsday-recovery-watch status (does NOT overwrite OS/EFI)'
