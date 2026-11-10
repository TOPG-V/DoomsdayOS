#!/usr/bin/env bash
# One-command DoomsdayOS installer. No disk formatting; uses existing install.sh.
set -Eeuo pipefail
MODE=''
usage(){
  echo 'Usage: bash quick-install.sh [--core|--ai]'
  echo '  --core  no AI: ZIMs, Kiwix, document search, maps, backups'
  echo '  --ai    full: core features plus optional local llama.cpp support'
  echo '  no flag: select a mode interactively (defaults to core without a TTY)'
}
while (( $# )); do
  case "$1" in
    --core|--without-ai) [[ -z $MODE || $MODE == core ]] || { usage; exit 2; };MODE=core ;;
    --ai|--with-ai) [[ -z $MODE || $MODE == ai ]] || { usage; exit 2; };MODE=ai ;;
    -h|--help) usage;exit 0 ;;
    *) usage;exit 2 ;;
  esac
  shift
done
if [[ -z $MODE ]]; then
  if [[ -t 0 ]]; then
    echo 'Choose your DoomsdayOS edition:'
    echo '  1) Core (NO AI): Kiwix, ZIM downloads, PDFs, maps, backup/recovery'
    echo '  2) Full (AI): Core plus local llama.cpp integration (models installed separately)'
    echo '  3) Cancel'
    read -r -p 'Select [1/2/3]: ' choice
    case "$choice" in 1)MODE=core;; 2)MODE=ai;; *)echo 'Cancelled';exit 1;;esac
  else
    echo 'No interactive terminal: defaulting to Core (no AI).'
    MODE=core
  fi
fi
if ! command -v curl >/dev/null;then echo 'curl is required to fetch the installer from GitHub' >&2;exit 1;fi
if ! command -v tar >/dev/null;then echo 'tar is required to unpack the installer' >&2;exit 1;fi
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
URL='https://github.com/TOPG-V/DoomsdayOS/archive/refs/heads/main.tar.gz'
echo "Fetching DoomsdayOS from GitHub ($MODE edition)..."
curl --fail --location --show-error --silent --retry 3 --connect-timeout 15 --max-time 180 "$URL" -o "$work/source.tar.gz"
tar -xzf "$work/source.tar.gz" -C "$work"
source="$work/DoomsdayOS-main/install.sh"
[[ -f $source ]] || { echo 'GitHub archive did not contain the installer' >&2;exit 1; }
if [[ $(id -u) -eq 0 ]]; then
  bash "$source" "--$MODE"
else
  command -v sudo >/dev/null || { echo 'sudo is required to install system services' >&2;exit 1; }
  sudo bash "$source" "--$MODE"
fi
