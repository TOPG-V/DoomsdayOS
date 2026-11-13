# ZimLens vNext — in-repository integration

This update modifies the **existing** DoomsdayOS frontend and gateway. It does **not** remove Kiwix, ZIM downloads, system maintenance, maps, research, PDF import, or the model manager. The original stylesheet remains intact, with a new restrained, warm-dark ZimLens theme layered on top. The original logo is replaced by a new locally bundled book-and-lens SVG.

## Features implemented

- New overview layout and categorized settings, designed for desktop and narrow screens.
- CPU, RAM, GPU utilization when `nvidia-smi` is installed, mounted storage usage, and CPU temperature when a recognizable kernel thermal sensor exists. No fabricated sensor values.
- Context budget usage and explicit **auto-compact when 15% of the token window remains**. The threshold is configurable to 10/15/20/25%. Earlier turns are summarized by your local llama.cpp model; the two latest turns are preserved. Conversation memory is process-local and token counts are estimates.
- Built-in light/dark/system appearance settings; users can save, select, edit or delete separate custom color themes. The built-in themes are not overwritten. Custom theme colors must pass a contrast check.
- MCP server **registration** (stdio/HTTPS), schema validation and explicit approval; no server commands are executed or connected by the registration UI. Actual MCP protocol connections require a separately permissioned broker, which is **not shipped**.
- Local skills: write instructions, request a draft from a configured local LLM, statically validate, and explicitly approve them. Approved skills may be selected for normal chat; generated code is not run.
- Local project metadata and project selection for attachments.
- Chat file attachments, capped at 24 MiB per file and 256 MiB total, are stored inertly. Plain text-like files can be included in model prompts in a capped excerpt; JPEG/PNG/WebP can be forwarded to a configured local vision model. MP4 and other formats are stored, but **not automatically transcribed or decoded** without suitable installed tools.
- Chat format selector and slash commands (`/help`, `/new`, `/compact`, `/stats`, `/themes`, `/mcp`, `/skills`, `/projects`, `/sandbox`, `/zims`, `/plan`, `/table`, `/math`, `/readme`, `/diagram`, `/quiz`). Output quality depends on the local model; charts and diagrams are requested as Markdown/Mermaid text rather than pretending to render arbitrary graphics.
- Sandbox status and an explicit bubblewrap **self-test** with no network or host writes. Arbitrary shell execution and general OS tool access are not enabled; sandbox use needs an approved broker.
- Existing ZIM download queue remains reachable. Automatic LLM-triggered downloads without checksum verification or confirmation are not enabled.

## Install from a repository checkout

```bash
# On your Linux server, in the DoomsdayOS repository checkout:
python3 apply_integration.py .
python3 -m unittest discover -s tests -v

# If this checkout is the installed app, restart your existing gateway:
sudo systemctl restart doomsday-ai-web.service
```

`apply_integration.py` creates `.pre-zimlens-vnext` backups of each original file it changes. Its main integration edits are idempotent. It adds no remote assets or extra background servers. The installer updates **source files**; if your running files reside in `/opt/doomsday-firstboot/app`, deploy your updated checkout there using your existing deployment procedure before restarting. Do not run the OS install script just to change CSS.

**Important:** This repository is an older DoomsdayOS baseline. ZimLens v12's separate package was not available to merge, so the integration targets the connected repository and preserves its existing features. Never overwrite a newer deployment without checking the diff and taking a backup.

## Safety boundaries

All new HTTP endpoints are bound by the existing loopback gateway and require matching localhost Host headers. Settings and attachments POST endpoints retain same-origin checking. User-provided MCP commands are stored as inert metadata, not executed. Skill-generated instructions are opt-in. ZIM archives remain read-only and disk formatting is not part of the UI update.

## Test

```bash
python3 -m unittest discover -s tests -v
node --check app/web/zimlens.js
node --check app/web/zimlens-extras.js
python3 -m py_compile app/zimlens_workspace.py app/zimlens_extensions.py app/zimlens_api.py app/ai_gateway.py
```