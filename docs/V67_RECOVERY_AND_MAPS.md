# V6.7 operator checklist

This version operates on a portable, existing Ubuntu/ZFS installation. It is **not** an A/B OS boot manager.

## Recovery checks

- Check `sudo doomsday-recovery report` and verify the pool GUID and existing snapshots.
- Record a user-selected last-known-good snapshot and validate a restore onto a spare device.
- Confirm the separate recovery environment can import source data read-only.
- Verify root/boot ZFS datasets, EFI partition, UEFI and secure-boot files independently.
- If a new computer refuses to boot, use trusted rescue media; the watchdog can only run after Ubuntu starts.
- Never use a ZFS rollback or `zfs receive -F` against the active root in this project.

## ZIM index checks

- Only archive names present in `/var/lib/doomsday-vault/zim-manifest.json` are auto-indexed.
- The FTS database lives at `/var/lib/doomsday-vault/zim-fts.sqlite` and can be deleted/rebuilt without damaging ZIM archives.
- The state of each archive shows its cursor and whether indexing is complete.
- Images may have perceptual hashes, titles, and links to the parent article; speech is not available until an explicit Whisper run.
- In the browser, the original Kiwix article is always the primary factual source.

## Offline map checks

- The MBTiles archive must contain raster PNG/JPG/WebP data, not raw vector PBF.
- Large tiles and routing graphs are not automatically downloaded.
- `doomsday-maps places` expects header `name,lat,lon` and indexes only local data.
- OSRM is pinned to `127.0.0.1:5000` and your UI to `127.0.0.1:8765`.
- A route is valid only for the prepared region; GPS requires browser/user permission.
- If you open the AI interface from Windows, localhost refers to Windows, not Ubuntu; use the Ubuntu browser or explicitly configured NAT port forwarding.
