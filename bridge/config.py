import os
from pathlib import Path

HOST = "127.0.0.1"
PORT = int(os.environ.get("KOBO_BRIDGE_PORT", "8850"))
ORIGIN = os.environ.get("KOBO_BRIDGE_ORIGIN", f"http://127.0.0.1:{PORT}").rstrip("/")

DATA_DIR = Path(os.environ.get("KOBO_BRIDGE_DATA", "/var/lib/kobo-bridge"))
LIBRARY_DIR = Path(os.environ.get("KOBO_BRIDGE_LIBRARY", "/home/djeddozo/kobo/library"))
# Everything the bridge makes goes into one subfolder, so it never touches other library files.
OUTPUT_DIR = LIBRARY_DIR / "Kobo Bridge"

KEPUBIFY = os.environ.get("KOBO_BRIDGE_KEPUBIFY", "/usr/local/bin/kepubify")

# The Kobo reaches the bridge over plain HTTP on the tailnet (WireGuard-encrypted), at these extra Host values.
EXTRA_HOSTS = {h.strip() for h in os.environ.get("KOBO_BRIDGE_EXTRA_HOSTS", "").split(",") if h.strip()}

# Ask Claude: runs the owner's Claude Code login headless, one folder per conversation.
CLAUDE = os.environ.get("KOBO_BRIDGE_CLAUDE", str(Path.home() / ".local/bin/claude"))
CLAUDE_OUTPUT_DIR = LIBRARY_DIR / "Claude"
# Free books picked in "Find a book" (Standard Ebooks, Project Gutenberg).
BOOKS_OUTPUT_DIR = LIBRARY_DIR / "Free books"
# The person's own Discover sources (article feeds); editable, next to the library.
SOURCES_FILE = Path(os.environ.get("KOBO_BRIDGE_SOURCES", str(LIBRARY_DIR.parent / "sources.json")))
CLAUDE_TIMEOUT = 600
CLAUDE_MAX_TURNS = 40

MAX_UPLOAD_BYTES = 200 * 1024 * 1024
MAX_FETCH_BYTES = 50 * 1024 * 1024
FETCH_TIMEOUT = 30
# An honest client name first (Wikipedia and others refuse fake browsers); a browser name only if a site refuses that.
# Wikipedia also wants contact info (a URL or email) in it: set KOBO_BRIDGE_CONTACT in the service unit.
CONTACT = os.environ.get("KOBO_BRIDGE_CONTACT", "").strip()
USER_AGENTS = (
    f"KoboBridge/0.1 (personal e-reader tool{'; ' + CONTACT if CONTACT else ''})",
    "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36",
)
