# DoomsdayOS v7.5 — Wi-Fi, Lens, RAG, drives and Always On

This release extends v7.4 without reinstalling/formatting any physical drive or modifying existing ZIM archives. It works in Core/no-AI mode; the optional semantic embedding and vision features require local model weights and compatible inference services.

## Install/update (Ubuntu)

```bash
curl -fsSL https://raw.githubusercontent.com/TOPG-V/DoomsdayOS/main/scripts/quick-install.sh -o /tmp/doomsday-quick-install.sh && bash /tmp/doomsday-quick-install.sh
```

It prompts for **Core** (Kiwix, local research, PDF OCR, image hashes, maps, archives) or **Full AI**. Existing settings are preserved. After installation visit `http://127.0.0.1:8765` in Ubuntu. The UI remains localhost-only, including System Health; never port-forward the unrestricted AI/repair dashboard publicly.

## Local hotspot

```bash
sudo doomsday-wifi scan
sudo doomsday-wifi setup
```

Requires a real AP-capable idle Wi-Fi device with NetworkManager and `iw`. A VirtualBox guest cannot create an AP from a simulated Ethernet/NAT card; pass through a supported USB Wi-Fi adapter or configure a hotspot on the host. Setup generates a WPA2 password and activates an isolated profile using NetworkManager shared IPv4. Existing Wi-Fi associations are not dropped. If an upstream Ethernet link exists, NM may share it; do not assume offline Internet access. The read-only portal is bound exclusively to the private hotspot IP on TCP 8780. Open `http://<hotspot-ip>:8780` on connected devices for a link to the verified Kiwix library on TCP 8080. Privileged maintenance and PDF uploads are NOT offered through the portal.

## Optional AC-aware Always On

```bash
sudo doomsday-power status
sudo doomsday-power enable
sudo doomsday-power disable
```

On AC this prevents local sleep/idle only while enabled; on battery the inhibitor stops. For a VM with no detectable power telemetry, opt in explicitly via `sudo doomsday-power enable --allow-unknown-power`. It does **not** override a Windows host's sleep or firmware power-off; for a VirtualBox deployment also configure Windows host power settings and USB passthrough. This service is disabled by default and never masks reboot/shutdown permanently.

## One to fifty independently imported ZFS pools

```bash
sudo doomsday-fleet scan
sudo doomsday-fleet enroll NEW_IMPORTED_POOL
sudo doomsday-fleet plan --replicas 2
sudo doomsday-fleet apply --replicas 2
```

- `scan` displays imported pools and enrolled archive paths. `enroll` checks the pool isn't the boot/root pool, then **only with typed approval** creates three empty named child datasets (ZIM archive, staging, backup). No `mkfs`, `wipefs`, `zpool create`, or raw-disk operations are issued; **it does not format a raw disk**. Import/prepare physical disks separately after backing up any existing data.
- The fleet allocator accounts for ZFS `copies`, a temporary staging peak and minimum free-space reserve. For each **user-selected backup ZIM**, it chooses up to **two additional distinct pool backups when space permits**. If an independent pool cannot fit a copy, the plan warns, rather than inventing redundancy.
- `apply` only operates on an entirely **pending** queue with no completed original or `.part` file on any enrolled drive. Already-downloading ZIMs are pinned to their existing pools; stop and finish or manage separately. There are no automatic cross-pool migrations. OS recovery image duplication is **not** included in fleet apply; retain independently tested root snapshots and backups.
- Re-run the plan after any drive disconnect. Never expose unmounted mountpoints as storage targets. All downloaded ZIMs still require publisher SHA-256 verification before publication.
- Direct Kiwix HTTPS links are preferred; use `sudo doomsday-zims direct-check --source bundle` and `sudo doomsday-zims direct-apply --source queue` to verify publisher SHA-256 and safely change pending URLs without clearing resumable `.part` files.

## Drag and drop Lens

In **Visual Lens**, drop PNG/JPEG/WEBP (max 12 MiB), PDFs (40 MiB) or text/Markdown/HTML files (2 MiB). PDFs/articles are stored in the local Research & PDFs index. Images have two modes:

- **Quick:** classic aHash/dHash similarity against indexed ZIM article images, plus optional native Tesseract OCR and article title matching. No LLM is required.
- **Thinking:** Quick results plus optional llama.cpp vision model analysis of this exact uploaded image. When the model isn't loaded, quick results remain available. Image links are scoped to the indexed source articles; an LLM-generated observation isn't itself a citation.

## EmbeddingGemma 2 semantic RAG

The RAG backend accepts an **already-running, compatible local `/v1/embeddings` endpoint** on `127.0.0.1:8082`. It does **not** install or automatically download EmbeddingGemma 2 weights. Register a compatible GGUF decision/embedding model only if your llama.cpp build supports it, or use a compatible local adapter exposing the same API. Start with `sudo doomsday-ai status`. If you are using Core/no-AI mode or no compatible embedding server, article-title search and OCR continue to work.

```bash
sudo doomsday-semantic status
sudo doomsday-semantic index
sudo doomsday-semantic search "renewable energy storage"
```

Optional idle indexing after you have installed and started a compatible local model: `sudo systemctl enable --now doomsday-semantic-index.timer` (disabled by default; do not enable if low on RAM).

`index` processes at most 150 unique ZIM article titles/passages per invocation; rerun when idle. Vectors are normalized and truncated to 256 dimensions for a compact index; no ZIM files are edited. The index is in `/var/lib/doomsday-vault/research/semantic-rag.sqlite`. Query-time semantic retrieval is optional and uses localhost only. The AI agent has extra **read-only** tools for semantic article search, article-specific images, ZIM queue summaries, fleet capacity and recovery watcher status.

## Recovery and preservation

Read-only ZFS health analysis now includes all enrolled fleet datasets, and reports critically low available pool space. Always verify your independent backups before a high-risk repair. Single-copy ZFS checksum detection cannot reconstruct absent blocks without a healthy independent copy. No live-root rollback, root repair, or unattended formatting runs as part of this release.

## Validation

Automated tests mock disk/GPU/network/large archive IO. Real AP USB hardware, real ZFS multi-pool enrollment and dual-backup writes, GGUF server compatibility, high-volume image indexing, and actual disaster recovery must be tested separately before relying on the setup for emergencies.
