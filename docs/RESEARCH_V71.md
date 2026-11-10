# DoomsdayOS v7.1 — local research, ZIM sources, HTTPS pages and PDF analysis

## Browser workflow

Open **http://127.0.0.1:8765** inside Ubuntu and select **Research & PDFs**.

1. **Search sources**: type an article name, a technical topic, or a phrase from an imported PDF. The local engine searches indexed ZIM article titles and indexed PDF/webpage passages. Results include clickable original-article URLs and PDF-page links; you can open extracted text before asking AI anything.
2. **Import PDF**: upload a local `.pdf` (40 MiB max). Page text is extracted with `pdftotext` from `poppler-utils`, or optional `python3-pypdf` fallback; every indexed text passage keeps its source PDF page number. The uploaded PDF stays in `/var/lib/doomsday-vault/research/pdf/` and is deduplicated by SHA-256.
3. **Import webpage**: paste ONE public HTTPS article or PDF URL and check the one-time Internet consent checkbox. This feature is **off by default**, does not search arbitrary Internet sites, never follows HTTP redirects, rejects credentials and non-public/loopback IPs, and downloads only bounded text/html/plain/PDF content. It does not run page scripts. A source must be explicitly imported before offline use.
4. **Analyze with local AI**: type a question; optionally select specific indexed sources and enable the *selected only* checkbox. The question and numbered source excerpts are sent only to the locally configured llama.cpp server (`127.0.0.1:8081`). The model is instructed to use only those excerpts and cite `[1]`, `[2]`, etc. Source links appear below the answer. If llama.cpp is not running, the index and reading interfaces still work.

Standalone **Local Chat** also has a **Research PDFs + articles** mode.

## Terminal use

```bash
sudo doomsday-research status
sudo doomsday-research add-pdf /path/to/paper.pdf
sudo doomsday-research search "solar charge controllers"
sudo doomsday-research add-url https://example.org/article --online
```

Keep archive indexing running with `sudo doomsday-index-zims run` so the library contains the ZIM articles you want to research. New PDFs and imported URLs are indexed immediately.

## Privacy, limits and failure cases

- All research state is local under `/var/lib/doomsday-vault/research/`. The UI binds to `127.0.0.1`, and cross-origin browser requests are rejected. Do not expose its port to the open Internet or forward it publicly.
- PDFs: maximum file size 40 MiB, first 200 pages, at most 1,300 passages. Large research publications may be truncated. **Image-only/scanned PDFs are not OCRed**. Rich charts and pictures are not automatically understood from extracted text. PDF metadata and links are supporting information, not independent validation of a document's claims.
- Direct remote import: single public HTTPS URL, no redirects, size cap of 8 MiB for HTML or 40 MiB for PDF. The importer pins a validated public DNS address for its TLS connection and does not permit HTTP, IP literals, `.local` domains or private destinations. It performs **no Internet-wide search** and cannot use pages that need JavaScript or authentication.
- This is text-first RAG with SQLite FTS5. The ZIM side uses the existing name-first article index. It is not an autonomous general-purpose browser agent, does not crawl arbitrary links, does not have an embeddings/vector store, and cannot guarantee that generated answers are correct.
- Excerpts are *untrusted source data*. Even with explicit instructions to ignore embedded commands, a local LLM can still be misled; independently verify high-stakes conclusions. The original PDF or Kiwix page always remains accessible.
- Installed PDF and web state resides on your **OS drive**, not in the read-only ZIM dataset. It is covered by your OS backups only to the extent your configured system backup includes that path. For disaster recovery, verify a real restore onto a spare disk.

## Troubleshooting

```bash
systemctl status doomsday-ai-web.service --no-pager
sudo journalctl -u doomsday-ai-web.service -n 60 --no-pager
command -v pdftotext
sudo apt-get install poppler-utils
```

If `sudo doomsday-research search` finds nothing from Kiwix, confirm that the individual ZIM is completed and run `sudo doomsday-index-zims run` to index it. If AI answers are unavailable, register a compatible GGUF and start your local llama.cpp text server; search works without it.
