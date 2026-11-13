#!/usr/bin/env bash
set -Eeuo pipefail
[[ $(id -u) == 0 ]] || { echo 'Run: sudo ./install.sh'; exit 1; }
cd "$(dirname "$(readlink -f "$0")")"
# Core mode keeps all local knowledge, backup, search and map services, without
# enabling any LLM inference. Full mode adds local llama.cpp runtime hooks.
# --bootstrap is a non-interactive first-boot entrypoint, never a mode chooser.
mode=''
stage_only=false
bootstrap=false
ask=false
usage() {
  echo 'Usage: sudo bash install.sh [--core|--ai|--ask] [--bootstrap|--stage-only]'
  echo '  --core / --without-ai   Offline knowledge, Kiwix, PDF search, maps, backups; no AI runtime'
  echo '  --ai   / --with-ai      Same features plus optional llama.cpp inference'
  echo '  --ask                   Interactive mode selection'
}
while (( $# )); do
  case "$1" in
    --core|--without-ai) [[ -z "$mode" || "$mode" == core ]] || { usage; exit 2; }; mode=core ;;
    --ai|--with-ai) [[ -z "$mode" || "$mode" == ai ]] || { usage; exit 2; }; mode=ai ;;
    --ask) ask=true ;;
    --bootstrap) bootstrap=true ;;
    --stage-only) stage_only=true ;;
    --help|-h) usage; exit 0 ;;
    *) usage; exit 2 ;;
  esac
  shift
done
if [[ $ask == true && -n $mode ]]; then echo 'Choose either --ask or a mode flag' >&2; exit 2; fi
mode_file=/etc/doomsday-vault/install-mode
if [[ -z $mode && $ask == false && -r $mode_file ]]; then
  previous=$(tr -d '\r\n' < "$mode_file")
  if [[ $previous == core || $previous == ai ]]; then mode=$previous; fi
fi
if [[ -z $mode && ( $ask == true || ( $bootstrap == false && $stage_only == false && -t 0 ) ) ]]; then
  echo 'DoomsdayOS installation mode:'
  echo '  1) CORE — no AI, offline Kiwix, research PDFs, maps, backups and ZIM downloads'
  echo '  2) FULL — core features plus optional local llama.cpp models'
  echo '  3) Cancel'
  read -r -p 'Choose [1/2/3]: ' choice
  case "$choice" in 1) mode=core ;; 2) mode=ai ;; *) echo 'Installation cancelled'; exit 1 ;; esac
fi
mode=${mode:-core}
if [[ $stage_only == true ]]; then
  # ISO late-command uses this mode; it only places a boot-time safe installer unit.
  install -d -m 0755 /opt/doomsday-firstboot /etc/systemd/system /var/lib/doomsday-vault /etc/doomsday-vault
  if [[ $(pwd -P) != /opt/doomsday-firstboot ]]; then cp -a app systemd scripts vendor install.sh /opt/doomsday-firstboot/; fi
  printf '%s\n' "$mode" > "$mode_file"
  chmod 0644 "$mode_file"
  install -m 0644 systemd/doomsday-bootstrap.service /etc/systemd/system/
  systemctl enable doomsday-bootstrap.service 2>/dev/null || true
  exit 0
fi
install -d -m 0755 /opt/doomsday-firstboot
if [[ $(pwd -P) != /opt/doomsday-firstboot ]]; then cp -a app systemd scripts vendor install.sh /opt/doomsday-firstboot/; fi
install -d -m 0700 /etc/doomsday-vault /var/lib/doomsday-vault /var/lib/doomsday-vault/research
install -d -m 0755 /usr/share/doomsday-vault
# GitHub text-file uploads may not preserve executable bits on optional scripts.
install -m 0755 scripts/setup-maplibre.sh /opt/doomsday-firstboot/scripts/setup-maplibre.sh
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
if command -v apt-get >/dev/null; then apt-get install -y --no-install-recommends iw kiwix-tools aria2 python3-pil python3-libzim poppler-utils tesseract-ocr tesseract-ocr-eng || echo 'Kiwix/iw/aria2 optional packages unavailable (install later when Internet is available)' >&2; fi
for f in systemd/doomsday-semantic-index.* systemd/doomsday-always-on.service systemd/doomsday-hotspot-portal.service systemd/doomsday-maintenance-auto.* systemd/doomsday-zim-index.* systemd/doomsday-recovery-watch-*.service systemd/doomsday-recovery-watch-*.timer systemd/doomsday-osrm.service systemd/doomsday-osrm-walk.service systemd/doomsday-ai-web.service systemd/doomsday-image-index.* systemd/doomsday-zim-download.* systemd/doomsday-hardware-recheck.* systemd/doomsday-kiwix.service systemd/doomsday-zim-protect.service systemd/doomsday-zim-verify.service systemd/doomsday-zim-verify.timer systemd/doomsday-stream-replica.service systemd/doomsday-stream-replica.timer; do install -m 0644 "$f" /etc/systemd/system/; done
# Fortnightly backup policy: preserve admin overrides and use systemd low-I/O priority.
if [[ ! -f /etc/doomsday-vault/backup-policy.json ]]; then
  install -m 0600 /dev/null /etc/doomsday-vault/backup-policy.json
  printf '%s\n' '{"interval_days":14,"stream_mib_per_second":16,"max_load_per_core":1.5,"min_system_free_gib":20,"zim_backups":"selected"}' > /etc/doomsday-vault/backup-policy.json
fi
install -d -m 0755 /etc/systemd/system/doomsday-backup.service.d
install -m 0644 systemd/doomsday-backup-policy.conf /etc/systemd/system/doomsday-backup.service.d/policy.conf
install -m 0644 systemd/doomsday-backup.timer /etc/systemd/system/doomsday-backup.timer
# Persist mode before launching the web service so core disables inference even
# when a model server from a prior full install is still present.
printf '%s\n' "$mode" > "$mode_file"
chmod 0644 "$mode_file"
if [[ $mode == ai ]]; then
  for f in systemd/doomsday-llama-*.service; do install -m 0644 "$f" /etc/systemd/system/; done
  install -m 0644 systemd/doomsday-model-manager.service /etc/systemd/system/
  if [[ ! -e /etc/doomsday-vault/ai-models.json ]]; then
    printf '%s\n' '{"models":{}}' > /etc/doomsday-vault/ai-models.json
    chmod 0600 /etc/doomsday-vault/ai-models.json
  fi
  systemctl disable --now doomsday-llama-text.service doomsday-llama-decision.service doomsday-llama-vision.service 2>/dev/null || true
else
  rm -f /usr/local/bin/doomsday-ai
  # Downgrading from full never deletes models or archive data, but stops local
  # inference and prevents model servers starting automatically after reboot.
  systemctl disable --now doomsday-llama-text.service doomsday-llama-decision.service doomsday-llama-vision.service doomsday-model-manager.service 2>/dev/null || true
fi
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
# Portable ZIM Protector is opt-in. It never imports or formats disks.
cat > /usr/local/bin/doomsday-zim-portable <<'LAUNCH'
#!/bin/sh
exec /usr/bin/python3 /opt/doomsday-firstboot/scripts/portable_zim_protector.py "$@"
LAUNCH
chmod 0755 /usr/local/bin/doomsday-zim-portable
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
if [[ $mode == ai ]]; then
cat > /usr/local/bin/doomsday-ai <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 llama_runtime.py "$@"
LAUNCH
fi
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
cat > /usr/local/bin/doomsday-research <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 research_library.py "$@"
LAUNCH
chmod 0755 /usr/local/bin/doomsday-research
cat > /usr/local/bin/doomsday-maintenance <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 system_maintenance.py "$@"
LAUNCH
chmod 0755 /usr/local/bin/doomsday-maintenance
cat > /usr/local/bin/doomsday-fleet <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 storage_fleet.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-power <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 always_on.py "$@"
LAUNCH
cat > /usr/local/bin/doomsday-semantic <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 semantic_rag.py "$@"
LAUNCH
chmod 0755 /usr/local/bin/doomsday-fleet /usr/local/bin/doomsday-power /usr/local/bin/doomsday-semantic
cat > /usr/local/bin/doomsday-ocr <<'LAUNCH'
#!/bin/sh
cd /opt/doomsday-firstboot/app
exec /usr/bin/python3 native_ocr.py "$@"
LAUNCH
chmod 0755 /usr/local/bin/doomsday-ocr
chmod 0755 /usr/local/bin/doomsday-index-zims /usr/local/bin/doomsday-enrich-media /usr/local/bin/doomsday-maps /usr/local/bin/doomsday-trip /usr/local/bin/doomsday-recovery-watch
if [[ $mode == ai ]]; then chmod 0755 /usr/local/bin/doomsday-ai; fi
chmod 0755 /usr/local/bin/doomsday-recovery /usr/local/bin/doomsday-image-index /usr/local/bin/doomsday-index-text
# The hotspot portal is bound to an enrolled AP address. Always-on is
# intentionally opt-in; preserve existing enablement across upgrades.
systemctl daemon-reload
if [[ $mode == ai ]]; then
  systemctl enable --now doomsday-model-manager.service
  systemctl try-restart doomsday-model-manager.service
fi
if command -v zfs >/dev/null && zfs list -H tank/zims >/dev/null 2>&1; then
  if systemctl list-unit-files 'zfs-scrub-monthly@.timer' --no-legend 2>/dev/null | grep -q 'zfs-scrub-monthly@.timer'; then systemctl enable --now zfs-scrub-monthly@tank.timer || true; fi
fi
systemctl enable --now doomsday-maintenance-auto.timer doomsday-backup.timer doomsday-zim-download.timer doomsday-hardware-recheck.timer doomsday-zim-verify.timer doomsday-stream-replica.timer doomsday-zim-protect.service doomsday-kiwix.service doomsday-ai-web.service doomsday-image-index.timer doomsday-zim-index.timer doomsday-recovery-watch-start.timer doomsday-recovery-watch-healthy.timer
# An upgrade must reload the new web server code; enabling an already active
# service by itself would continue running the old Python process.
systemctl try-restart doomsday-ai-web.service
systemctl disable doomsday-bootstrap.service 2>/dev/null || true
touch /var/lib/doomsday-vault/bootstrap-complete
printf '\nDoomsday first-boot services installed. No disk formatting performed.\n'
echo 'Interactive ZIM/Wi-Fi wizard: sudo doomsday-setup'
echo 'ZIM queue: sudo doomsday-zims status'
echo 'Change selected ZIM backups: sudo doomsday-zims backups'
echo 'Optional separate ZFS pool: sudo doomsday-zims dual-setup && sudo doomsday-zims optimize'
echo 'Offline knowledge browser: http://localhost:8080 from Ubuntu'
echo "Offline workspace: http://localhost:8765 (installation mode: $mode)"
if [[ $mode == ai ]]; then
  echo 'AI mode enabled. To add a local model: sudo doomsday-ai register text /path/to/model.gguf'
else
  echo 'Core mode: AI chat, generation, vision inference and translation are disabled.'
  echo 'To enable AI later: sudo bash /opt/doomsday-firstboot/install.sh --ai'
fi
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

echo 'Research & PDF indexing: sudo doomsday-research --help (browser: http://localhost:8765 → Research & PDFs)'
echo 'Safe OS diagnostics and repair planning: sudo doomsday-maintenance analyze'
echo 'Reviewed repairs: sudo doomsday-maintenance apply ACTION --confirm APPLY-ACTION'
echo 'Native OCR: tesseract-ocr is optional; language packs can be installed locally (tesseract-ocr-pol etc.)' 

echo 'Wi-Fi hotspot setup: sudo doomsday-wifi setup (uses an idle AP-capable adapter)'
echo 'Hotspot landing page after setup: see sudo doomsday-wifi scan, usually http://10.42.0.1:8780'
echo 'Opt-in AC-aware Always On: sudo doomsday-power enable (not effective while a VM host sleeps)'
echo 'Storage fleet 1..50 pools: sudo doomsday-fleet scan / plan / enroll POOL / apply'
echo 'Local EmbeddingGemma RAG (model/runtime separate): sudo doomsday-semantic status / index / search QUERY'
