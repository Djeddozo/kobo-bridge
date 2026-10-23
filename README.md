# Kobo Bridge

A Raspberry Pi service that turns an e-ink Kobo e-reader into a connected device: share an article
or ebook from an Android phone and it arrives on the Kobo as a clean book; ask Claude a question
from the e-reader and the answer (and any document Claude writes) comes back as an ebook.

## Overview for reviewers

**Problem.** Reading long articles and books on a phone is tiring, and getting them onto a Kobo is
fiddly (cables, Calibre, format conversion). Typing on e-ink is slow, so asking an AI something
while reading was impossible.

**What I built.**
- **Share to Kobo** – an installable PWA that appears in Android's share menu. The Pi extracts the
  readable article (trafilatura), builds an EPUB, converts it to Kobo's KEPUB format and drops it in
  a library folder that Syncthing syncs to the Kobo. EPUB/PDF/CBZ/MOBI… files are named from their
  metadata. Jobs run one at a time at low priority and are moved into the library only when complete.
- **Ask Claude from the Kobo** – a KOReader plugin (Lua) sends a question over Tailscale; the Pi runs
  Claude Code headless (`claude -p`) in a per-conversation folder with a fixed tool allow-list
  (web search/fetch and file tools only: no shell, no MCP servers, no user settings or hooks), a
  turn limit and a timeout. Answers are formatted for a 7" e-ink screen; files Claude writes are
  converted to EPUBs. Conversations can be resumed.
- **Discover** – shelves of free, legal books from Standard Ebooks and Project Gutenberg plus the
  user's own article feeds, cached on the Pi with covers shrunk to grayscale JPEGs for e-ink. Claude
  can suggest titles but never downloads books itself (Standard Ebooks asks AI agents not to).

**Security decisions.** Bound to `127.0.0.1`, reachable only through Tailscale Serve; exact Host
check and Origin / custom-header checks against CSRF; **SSRF protection** – shared links may only
resolve to public addresses, re-checked at every redirect; size caps on downloads and uploads;
systemd sandboxing (read-only system and home, writable only at the data and library folders).

**Stack.** Python, FastAPI, httpx, trafilatura, ebooklib, Pillow, kepubify; Lua (KOReader plugin);
vanilla JS PWA with service worker; systemd; Tailscale; Syncthing. 37 pytest tests plus a smoke
test that performs real downloads on a temporary port and library.

**My role.** Idea, requirements, security model, device testing on my own Kobo Libra H2O and
deployment. The code was written together with AI coding agents (Claude Code).

**Status.** In daily use on my Pi (`kobo-bridge.service`).
**Next steps.** Per-device authentication for the Kobo endpoint in addition to the tailnet
boundary; a richer reading-list view; packaging the KOReader plugin for others.

---

Send links and ebooks from your phone to your Kobo, through the Pi. Share anything to **Kobo Bridge** in Android's share menu; the Pi turns it into a book in `~/kobo/library/Kobo Bridge`, and Syncthing carries it to the Kobo next time it's on Wi-Fi.

- **Private URL:** `https://your-pi.your-tailnet.ts.net:8850` (tailnet only, via Tailscale Serve)
- **Service:** `kobo-bridge.service`, code in `/opt/kobo-bridge`, data in `/var/lib/kobo-bridge`
- **Editable source:** your checkout of this repository

## What it does with what you send

| You share | You get |
|---|---|
| A web article | A clean EPUB (text, headings, quotes; no ads or menus), converted to KEPUB |
| A link to an EPUB | The book, converted to KEPUB and named "Title - Author" from its metadata |
| A link to or file of PDF, CBZ, MOBI, FB2, DJVU, TXT… | The file as it is (KOReader reads these) |
| An EPUB file | KEPUB, named from its metadata |

Jobs run one at a time at low priority (`Nice=10`, `CPUQuota=150%`). Files are moved into the library only when finished, so Syncthing never syncs half a book.

## Android installation

1. Connect Tailscale on the phone and open the private URL in Chrome.
2. Tap **Install app** (or **⋮ → Add to Home screen → Install**).
3. Kobo Bridge now appears in the **Share** menu of other apps.

## Security

- Listens on `127.0.0.1:8850` only; reachable through the private Tailscale Serve route.
- Exact Host check; API writes need the `X-Kobo-Bridge: 1` header and a matching Origin. The share endpoint (which Android posts to without a custom header) refuses other sites' Origins and `Sec-Fetch-Site: cross-site`.
- Shared links may only point to public addresses: localhost, LAN and tailnet (100.64.0.0/10) are refused at every redirect. Downloads are capped at 50 MB, uploads at 200 MB.
- The service runs as an unprivileged user account with a read-only system and home, writable only at `/var/lib/kobo-bridge` and `~/kobo/library`.
- Wikipedia refuses clients without contact info: set `KOBO_BRIDGE_CONTACT` (a URL or email) in the unit to enable it.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.lock pytest
.venv/bin/python -m pytest -q
sh deploy/smoke-test.sh          # real downloads, temporary port and library
sudo sh deploy/install.sh        # deploy or update
journalctl -u kobo-bridge -n 50 --no-pager
```
