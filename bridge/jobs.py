import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    kind TEXT NOT NULL,          -- 'url' or 'file'
    source TEXT NOT NULL,        -- the link, or the original file name
    title TEXT,
    status TEXT NOT NULL,        -- queued, running, done, failed
    output TEXT,                 -- file name inside the output folder
    error TEXT
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created DESC);
"""

FIELDS = ("id", "created", "updated", "kind", "source", "title", "status", "output", "error")


class JobStore:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def add(self, kind, source, title=None):
        job_id = uuid.uuid4().hex[:12]
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?)",
                            (job_id, now, now, kind, source, title, "queued", None, None))
        return job_id

    def update(self, job_id, **fields):
        fields = {k: v for k, v in fields.items() if k in FIELDS and k != "id"}
        fields["updated"] = time.time()
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.lock, self.db:
            self.db.execute(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id))

    def get(self, job_id):
        with self.lock:
            row = self.db.execute(f"SELECT {','.join(FIELDS)} FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(zip(FIELDS, row)) if row else None

    def recent(self, limit=50):
        with self.lock:
            rows = self.db.execute(f"SELECT {','.join(FIELDS)} FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [dict(zip(FIELDS, r)) for r in rows]

    def recover(self):
        """After a restart: jobs that were mid-conversion are failed, queued ones are returned to run again."""
        with self.lock, self.db:
            self.db.execute("UPDATE jobs SET status='failed', error='Interrupted by a restart', updated=? WHERE status='running'",
                            (time.time(),))
            rows = self.db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY created").fetchall()
        return [r[0] for r in rows]
