"""Ask Claude from the Kobo: Claude Code runs headless on the Pi, and documents it writes become ebooks."""
import html
import json
import logging
import os
import queue
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import markdown

from . import config, convert
from .security import safe_filename

log = logging.getLogger("kobo-bridge.claude")

# Web lookups and files inside the conversation folder only: no shell, no MCP servers, no user settings or hooks.
TOOLS = "WebSearch,WebFetch,Read,Write,Edit,Glob,Grep"
ALLOWED = "WebSearch WebFetch Read Write Edit Glob Grep"

SYSTEM = """You are answering questions sent from a Kobo Libra H2O e-reader: a 7" black-and-white e-ink \
touchscreen, where typing is slow. Reply in the language of the question.

Chat answers are shown on the e-reader, so keep them compact and easy to read on e-ink: short paragraphs, \
simple headings, bullet lists, bold for key words. No tables, no emoji, no code unless asked. Don't ask \
clarifying questions unless the request is truly ambiguous; make a sensible choice and say so.

When the person asks for something to read (an article, story, summary, study guide, notes, a guide, a \
book) or when the answer would be longer than about three screens, write it as a Markdown file in ./out/ \
(one file per document, starting with a "# Title" line) and give a short chat reply saying what you made. \
Files in ./out/ are turned into ebooks and delivered to the e-reader's library automatically. Only write \
files inside ./out/. You can search and fetch the web for current information; mention sources briefly.

Books: you can't download books yourself, and don't try to fetch them from any site. When the person asks \
for book recommendations or wants a specific book, recommend titles in your reply and also write them to \
./out/suggestions.txt, one per line as "Title - Author". The e-reader then offers a "Find these books" \
button that searches Standard Ebooks and Project Gutenberg (free, legal, mostly public-domain classics) \
for each, and the person picks what to download. For newer, copyrighted books, say they can borrow them \
from their public library through the Kobo's built-in OverDrive, or buy them. Never point to pirate \
sites such as Anna's Archive, LibGen or Z-Library."""

ANSWER_CSS = """
body { font-family: serif; line-height: 1.4; }
h1, h2, h3 { margin: .6em 0 .3em; line-height: 1.2; }
h1 { font-size: 1.3em; } h2 { font-size: 1.15em; } h3 { font-size: 1.05em; }
p { margin: 0 0 .55em; }
ul, ol { margin: 0 0 .55em; padding-left: 1.3em; }
li { margin-bottom: .2em; }
blockquote { margin: .4em 0 .6em 1em; font-style: italic; }
code { font-family: monospace; }
hr { margin: .8em 0; }
"""

DOC_SUFFIXES = {".md", ".markdown", ".txt", ".html", ".htm"}

FIELDS = ("id", "conversation", "created", "updated", "status", "prompt", "context", "book",
          "answer", "html", "files", "error", "session_id", "cost", "suggestions")
SUGGESTIONS_FILE = "suggestions.txt"

SCHEMA = """
CREATE TABLE IF NOT EXISTS asks (
    id TEXT PRIMARY KEY,
    conversation TEXT NOT NULL,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,       -- queued, running, done, failed
    prompt TEXT NOT NULL,
    context TEXT,               -- passage selected in the book, if any
    book TEXT,                  -- "Title - Author" of the open book, if any
    answer TEXT,                -- Claude's chat reply (Markdown)
    html TEXT,                  -- the reply as XHTML for KOReader's HTML viewer
    files TEXT,                 -- JSON list of ebook names delivered to the library
    error TEXT,
    session_id TEXT,
    cost REAL,
    suggestions TEXT            -- JSON list of "Title - Author" books Claude suggested
);
CREATE INDEX IF NOT EXISTS asks_created ON asks(created DESC);
"""


def load_token():
    """The shared secret the Kobo plugin sends; created on first start."""
    path = config.DATA_DIR / "kobo-token"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(32))
        path.chmod(0o600)
    return path.read_text().strip()


def to_xhtml(text):
    rendered = markdown.markdown(text or "", extensions=["sane_lists", "nl2br"], output_format="xhtml")
    return convert.clean_xhtml(rendered)


def answer_page(text, files):
    """The chat reply as a small XHTML page for KOReader's ScrollHtmlWidget."""
    body = to_xhtml(text)
    if files:
        items = "".join(f"<li>{html.escape(f)}</li>" for f in files)
        body += f"<hr/><p><b>Sent to your library</b> (Books › Claude):</p><ul>{items}</ul>"
    return body


class AskStore:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(asks)")}
        if "suggestions" not in columns:  # databases from before book suggestions
            self.db.execute("ALTER TABLE asks ADD COLUMN suggestions TEXT")

    def add(self, prompt, context=None, book=None, conversation=None):
        ask_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO asks (id, conversation, created, updated, status, prompt, context, book) "
                            "VALUES (?,?,?,?,?,?,?,?)",
                            (ask_id, conversation or ask_id, now, now, "queued", prompt, context, book))
        return ask_id

    def update(self, ask_id, **fields):
        fields = {k: v for k, v in fields.items() if k in FIELDS and k != "id"}
        fields["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock, self.db:
            self.db.execute(f"UPDATE asks SET {sets} WHERE id=?", (*fields.values(), ask_id))

    def _rows(self, sql, args):
        with self.lock:
            rows = self.db.execute(f"SELECT {','.join(FIELDS)} FROM asks {sql}", args).fetchall()
        out = []
        for row in rows:
            item = dict(zip(FIELDS, row))
            item["files"] = json.loads(item["files"] or "[]")
            item["suggestions"] = json.loads(item["suggestions"] or "[]")
            out.append(item)
        return out

    def get(self, ask_id):
        rows = self._rows("WHERE id=?", (ask_id,))
        return rows[0] if rows else None

    def recent(self, limit=30):
        return self._rows("ORDER BY created DESC LIMIT ?", (limit,))

    def last_session(self, conversation):
        rows = self._rows("WHERE conversation=? AND session_id IS NOT NULL ORDER BY created DESC LIMIT 1",
                          (conversation,))
        return rows[0]["session_id"] if rows else None

    def recover(self):
        with self.lock, self.db:
            self.db.execute("UPDATE asks SET status='failed', error='Interrupted by a restart', updated=? "
                            "WHERE status IN ('queued','running')", (time.time(),))


def build_prompt(ask):
    parts = []
    if ask.get("book"):
        parts.append(f'I\'m reading "{ask["book"]}" on my e-reader.')
    if ask.get("context"):
        parts.append(f'Selected passage:\n"""\n{ask["context"]}\n"""')
    parts.append(ask["prompt"])
    return "\n\n".join(parts)


def snapshot(folder):
    return {p.relative_to(folder).as_posix(): p.stat().st_mtime for p in folder.rglob("*") if p.is_file()}


def run_claude(prompt, workspace, session_id=None):
    """One headless Claude Code run in the conversation folder; returns the parsed JSON result."""
    exe = config.CLAUDE
    if not (os.path.isfile(exe) and os.access(exe, os.X_OK)):
        raise RuntimeError("Claude Code isn't installed for this account")
    cmd = [exe, "-p", "--output-format", "json", "--tools", TOOLS, "--allowedTools", ALLOWED,
           "--permission-mode", "acceptEdits", "--strict-mcp-config", "--setting-sources", "",
           "--max-turns", str(config.CLAUDE_MAX_TURNS), "--append-system-prompt", SYSTEM]
    if session_id:
        cmd += ["--resume", session_id]
    home = str(Path.home())
    env = {"PATH": f"{Path(exe).parent}:/usr/bin:/bin", "HOME": home, "LANG": "C.UTF-8",
           "USER": os.environ.get("USER", Path(home).name), "DISABLE_AUTOUPDATER": "1"}
    try:
        result = subprocess.run(cmd, input=prompt, cwd=workspace, env=env, capture_output=True, text=True,
                                timeout=config.CLAUDE_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Claude took too long (over 10 minutes)") from exc
    try:
        out = json.loads(result.stdout or "{}")
    except ValueError as exc:
        log.error("Unreadable Claude output: %s", (result.stderr or "")[:300])
        raise RuntimeError("Claude Code gave an unreadable answer. Is it still logged in on the Pi?") from exc
    if result.returncode or out.get("is_error"):
        raise RuntimeError(f"Claude Code: {str(out.get('result') or result.stderr or 'failed')[:300]}")
    return out


def deliver(path, workdir):
    """A file Claude wrote → an ebook in the library's Claude folder; returns its name or None."""
    suffix = path.suffix.lower()
    if suffix in DOC_SUFFIXES:
        text = path.read_text(encoding="utf-8", errors="replace")
        if suffix in (".html", ".htm"):
            body, title = convert.clean_xhtml(text), None
        else:
            lines = text.splitlines()
            title = next((l[2:].strip() for l in lines if l.startswith("# ")), None)
            if title:  # make_epub adds the title itself
                first = next(i for i, l in enumerate(lines) if l.startswith("# "))
                text = "\n".join(lines[:first] + lines[first + 1:])
            body = to_xhtml(text)
        title = title or path.stem.replace("-", " ").replace("_", " ").strip().capitalize()
        epub_path = convert.make_epub(title, "Claude", "en", body, workdir)
        return convert.process_file(epub_path, f"{safe_filename(title, 'Claude')}.epub", workdir,
                                    config.CLAUDE_OUTPUT_DIR, generated=True)[1]
    if suffix in convert.SUPPORTED:
        copy = Path(tempfile.mkdtemp(dir=workdir)) / path.name
        shutil.copy2(path, copy)
        return convert.process_file(copy, path.name, workdir, config.CLAUDE_OUTPUT_DIR)[1]
    return None


def process(store, ask_id):
    ask = store.get(ask_id)
    workspace = config.DATA_DIR / "claude" / ask["conversation"]
    out_dir = workspace / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    before = snapshot(out_dir)
    result = run_claude(build_prompt(ask), workspace, store.last_session(ask["conversation"]))
    after = snapshot(out_dir)
    changed = [name for name, mtime in after.items() if before.get(name) != mtime]

    suggestions = []
    if SUGGESTIONS_FILE in changed:
        changed.remove(SUGGESTIONS_FILE)
        lines = (out_dir / SUGGESTIONS_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
        suggestions = [" ".join(l.strip(" -*•\t").split())[:200] for l in lines if l.strip(" -*•\t")][:20]

    files = []
    work_root = config.DATA_DIR / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work_root) as tmp:
        for name in sorted(changed):
            try:
                made = deliver(out_dir / name, Path(tmp))
                if made:
                    files.append(made)
            except Exception:
                log.exception("Couldn't turn %s into an ebook", name)

    answer = str(result.get("result") or "").strip() or "(Claude didn't write a reply.)"
    store.update(ask_id, status="done", answer=answer, html=answer_page(answer, files), files=json.dumps(files),
                 suggestions=json.dumps(suggestions), session_id=result.get("session_id"),
                 cost=result.get("total_cost_usd"), error=None)


class ClaudeWorker:
    """One Claude run at a time: each takes a few hundred MB of RAM on the Pi."""

    def __init__(self, store):
        self.store = store
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self.loop, name="claude", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.queue.put(None)
        self.thread.join(timeout=10)

    def submit(self, ask_id):
        self.queue.put(ask_id)

    def loop(self):
        while (ask_id := self.queue.get()) is not None:
            self.store.update(ask_id, status="running")
            try:
                process(self.store, ask_id)
            except RuntimeError as exc:
                self.store.update(ask_id, status="failed", error=str(exc))
            except Exception:
                log.exception("Ask %s crashed", ask_id)
                self.store.update(ask_id, status="failed", error="Something went wrong on the Pi")
