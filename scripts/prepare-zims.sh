#!/usr/bin/env bash
# Idempotent and safe: ONLY create dedicated ZFS child filesystems; never repartition disks.
set -Eeuo pipefail
[[ $(id -u) -eq 0 ]] || { echo 'Run with sudo' >&2; exit 1; }
command -v zfs >/dev/null || { echo 'OpenZFS utilities not installed' >&2; exit 1; }
root=$(findmnt -n -o SOURCE /)
[[ $(findmnt -n -o FSTYPE /) == zfs ]] || { echo 'No ZFS system root; immutable ZIM setup requires ZFS' >&2; exit 1; }
rootpool=${root%%/*}
if zfs list -H -o name tank/zims >/dev/null 2>&1 && [[ $(findmnt -M /tank/zims -n -o SOURCE 2>/dev/null || true) == tank/zims ]]; then
  library=tank/zims
  stage=tank/zim-staging
  libdir=/tank/zims
  stagedir=/tank/zim-staging
else
  library=${rootpool}/DOOMSDAY_ZIMS
  stage=${rootpool}/DOOMSDAY_ZIM_STAGING
  libdir=/srv/doomsday-zims
  stagedir=/srv/doomsday-zim-staging
fi
[[ $library != "$root" && $library != "$rootpool" && $library != tank && $library != bpool ]] || { echo 'Refusing protected system dataset' >&2; exit 1; }
if ! zfs list -H -o name "$library" >/dev/null 2>&1; then
  zfs create -o mountpoint="$libdir" -o recordsize=128K -o compression=lz4 -o atime=off -o copies=2 "$library"
fi
if ! zfs list -H -o name "$stage" >/dev/null 2>&1; then
  zfs create -o mountpoint="$stagedir" -o compression=lz4 -o atime=off "$stage"
fi
[[ $(findmnt -M "$libdir" -n -o SOURCE 2>/dev/null || true) == "$library" ]] || { echo "Archive filesystem not mounted at expected $libdir" >&2; exit 1; }
[[ $(findmnt -M "$stagedir" -n -o SOURCE 2>/dev/null || true) == "$stage" ]] || { echo "Staging filesystem not mounted at expected $stagedir" >&2; exit 1; }
# Do not hide old .part files under readonly=on before the owner can migrate them.
if compgen -G "$libdir/*.part" >/dev/null || compgen -G "$libdir/*.part.json" >/dev/null; then
  echo 'Old partial downloads are in the library dataset. Move/recover these safely before enabling immutable mode.' >&2
  exit 1
fi
if [[ $(zfs get -H -o value readonly "$library") != on ]]; then chmod 0755 "$libdir"; fi
chmod 0700 "$stagedir"
zfs set readonly=on "$library"
install -d -m 0700 /etc/doomsday-vault /var/lib/doomsday-vault
export DOOMSDAY_SELECTED_LIB="$libdir" DOOMSDAY_SELECTED_STAGE="$stagedir" DOOMSDAY_SELECTED_LIB_DS="$library" DOOMSDAY_SELECTED_STAGE_DS="$stage"
python3 - <<'PY'
import json,os,tempfile
from pathlib import Path
p=Path('/etc/doomsday-vault/firstboot.json')
try: cfg=json.loads(p.read_text())
except (OSError,ValueError): cfg={}
cfg.update({'zim_dir':os.environ['DOOMSDAY_SELECTED_LIB'], 'staging_dir':os.environ['DOOMSDAY_SELECTED_STAGE'], 'zim_dataset':os.environ['DOOMSDAY_SELECTED_LIB_DS'], 'staging_dataset':os.environ['DOOMSDAY_SELECTED_STAGE_DS'], 'immutable_zims':True, 'require_sha256':True})
cfg.setdefault('backup_dir','')
cfg.setdefault('min_free_gib',3)
fd,tmp=tempfile.mkstemp(dir=p.parent, prefix='.firstboot.')
with os.fdopen(fd,'w') as f:
  json.dump(cfg,f,indent=2,sort_keys=True);f.write('\n');f.flush();os.fsync(f.fileno())
os.chmod(tmp,0o600)
os.replace(tmp,p)
PY
printf 'Immutable ZIM library: %s (%s)\nWritable staging: %s (%s)\n' "$libdir" "$library" "$stagedir" "$stage"
