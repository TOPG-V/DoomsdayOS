# DoomsdayOS v7.3: offline OCR and protected maintenance

## Native OCR (works in Core and Full)

Local dependencies: `tesseract-ocr`, `tesseract-ocr-eng`, `python3-pil`, `poppler-utils`.
To add another installed OCR language, e.g. Polish: `sudo apt install tesseract-ocr-pol` while connected to a package repository; you may alternatively stage the Ubuntu packages offline.

Open `http://127.0.0.1:8765` inside the Ubuntu VM → **Visual Lens** → choose **Read text (native OCR)** and the installed language. OCR uses the local Tesseract binary, not llama.cpp, the cloud, or any API. It never writes changes back to the source image.

CLI:

```bash
sudo doomsday-ocr languages
sudo doomsday-ocr image /path/to/photo.png --language eng
sudo doomsday-ocr pdf-page /path/to/manual.pdf --page 3 --language pol
```

**Research & PDFs** automatically applies OCR only to text-sparse scanned pages, to a maximum of **12 pages per import**; remaining pages still get ordinary PDF text extraction. The input PDF is retained unchanged in the research library. For large scanned manuals, split them into smaller documents for a more complete index. Recognized text may contain errors; inspect the original image for high-stakes uses.

## System Health analysis

Open **System Health** → **Analyze System**. This runs read-only commands for disk headroom, failed services, package status, mounted ZIM datasets, and ZFS pool warnings. The report includes a human-reviewable plan. The LLM can call the `system_diagnostics` read-only tool, but it **cannot execute repair commands or custom shell scripts**.

Choose **Run read-only terminal script** for the preapproved `all`, `boot`, `storage`, `integrity`, or `packages` command set. The `packages` script runs a bounded `dpkg --verify`; modified configuration files are not necessarily corrupted.

CLI diagnostics:

```bash
sudo doomsday-maintenance analyze
sudo doomsday-maintenance run-script integrity
sudo doomsday-maintenance run-script packages
```

**Repairs require exact approval in your Ubuntu terminal**, for example:

```bash
sudo doomsday-maintenance apply restart-doomsday-kiwix --confirm APPLY-restart-doomsday-kiwix
sudo doomsday-maintenance apply protect-zims --confirm APPLY-protect-zims
sudo doomsday-maintenance apply scrub-enrolled --confirm APPLY-scrub-enrolled
```

The last operation starts a ZFS scrub on the **verified, enrolled ZIM pool** and refuses while the Doomsday ZIM download service is active. A scrub can repair errors only if another healthy copy exists (mirror, RAIDZ, `copies=2`, etc.). **Checksums with `copies=1` provide detection, not independent repair**.

A rate-limited systemd timer tries to restart only failed local reader services (Kiwix and the browser) at most twice per 24 hours. It cannot alter the OS root or EFI, run `fsck`, reinstall packages, delete ZIMs, rewrite ZFS data, or accept model-generated code. Logs live in `/var/lib/doomsday-vault/maintenance/actions.jsonl`.

Disable automated service recovery, if desired:

```bash
sudo systemctl disable --now doomsday-maintenance-auto.timer
```

## Data protection rules

1. Keep originals immutable after verified SHA-256 publication. Never repair a bad archive by silently overwriting the only copy.
2. Keep at least one separately verified backup on another drive when feasible.
3. Use ZFS scrubs on redundant pools and test your restore path before relying on it.
4. Do not run `fsck` against a mounted filesystem, wipe drives, or automatically reinstall broken packages using an LLM-generated command.
5. Privileged repair plans must be reviewed and applied from an administrator terminal; the web UI remains sandboxed.
