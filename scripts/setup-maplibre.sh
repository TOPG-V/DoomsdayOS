#!/usr/bin/env bash
# Fetch *optional*, fixed-version MapLibre GL JS assets for fully offline use.
# Downloads are only performed on explicit invocation and never at boot.
set -Eeuo pipefail
VERSION=5.15.0
WEB=/opt/doomsday-firstboot/app/web/vendor
[[ $(id -u) -eq 0 ]] || { echo 'Run with sudo'; exit 1; }
command -v curl >/dev/null || { echo 'Install curl first'; exit 1; }
install -d -m 0755 "$WEB"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
for ext in js css; do
  src="https://cdn.jsdelivr.net/npm/maplibre-gl@${VERSION}/dist/maplibre-gl.${ext}"
  curl --fail --location --silent --show-error --retry 2 --max-time 120 "$src" -o "$tmp/maplibre-gl.$ext"
  [[ -s "$tmp/maplibre-gl.$ext" ]] || { echo "Empty $ext download" >&2; exit 1; }
done
# Never overwrite the OS or touch other archives.
install -m 0644 "$tmp/maplibre-gl.js" "$WEB/maplibre-gl.js"
install -m 0644 "$tmp/maplibre-gl.css" "$WEB/maplibre-gl.css"
echo "MapLibre $VERSION installed locally for offline vector MBTiles."
echo "Nothing will be downloaded at runtime. Refresh http://127.0.0.1:8765"
