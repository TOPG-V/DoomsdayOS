#!/usr/bin/env bash
# Update only the ZIM dashboard and queue files; preserve local vault state.
set -Eeuo pipefail
[[ ${EUID} -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
APP=/opt/doomsday-firstboot/app
UNIT=/etc/systemd/system/doomsday-ai-web.service
BASE=https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main
[[ -d "$APP/web" && -f "$APP/ai_gateway.py" && -f "$APP/zim_manager.py" ]] || {
 echo "Expected DoomsdayOS app not found at $APP" >&2; exit 1;
}
command -v curl >/dev/null || { echo "curl required" >&2; exit 1; }
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
for file in app/ai_gateway.py app/zim_manager.py app/web/index.html app/web/app.js app/web/style.css systemd/doomsday-ai-web.service; do
 mkdir -p "$temp/$(dirname "$file")"
 curl --fail --location --silent --show-error --retry 3 "$BASE/$file" -o "$temp/$file"
done
python3 -m py_compile "$temp/app/ai_gateway.py" "$temp/app/zim_manager.py"
if command -v node >/dev/null; then node --check "$temp/app/web/app.js"; fi
backup="/var/lib/doomsday-vault/update-backups/zim-dashboard-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
for file in app/ai_gateway.py app/zim_manager.py app/web/index.html app/web/app.js app/web/style.css; do
 dest="/opt/doomsday-firstboot/$file"
 cp -a "$dest" "$backup/$(basename "$file").before"
 install -m 0644 "$temp/$file" "$dest"
done
if [[ -f "$UNIT" ]]; then cp -a "$UNIT" "$backup/doomsday-ai-web.service.before"; fi
install -m 0644 "$temp/systemd/doomsday-ai-web.service" "$UNIT"
systemctl daemon-reload
systemctl restart doomsday-ai-web.service
systemctl status doomsday-ai-web.service --no-pager
echo "Dashboard: http://127.0.0.1:8765/#downloads"
echo "Original installed configs, ZIM files and download queue were left untouched."
echo "Old launcher files backed up under $backup"
