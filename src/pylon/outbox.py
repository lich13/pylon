from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import home


class Outbox:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or os.environ.get("PYLON_OUTBOX", str(home() / "data/outbox.sqlite3")))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, profile TEXT NOT NULL, job_id TEXT NOT NULL,
                    kind TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    updated_at REAL NOT NULL, receipt TEXT, error TEXT
                );
                CREATE INDEX IF NOT EXISTS ready_events ON events(status,next_attempt,created_at);
                CREATE TABLE IF NOT EXISTS run_events (
                    run_id TEXT NOT NULL, event_id TEXT NOT NULL,
                    PRIMARY KEY(run_id,event_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, started_at REAL NOT NULL,
                    finished_at REAL, status TEXT NOT NULL, detail TEXT
                );
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS updates (
                    profile TEXT NOT NULL, update_id INTEGER NOT NULL, payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', error TEXT,
                    PRIMARY KEY(profile,update_id)
                );
            """)
        os.chmod(self.path, 0o600)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, *, profile: str, job_id: str, kind: str, payload: dict,
                key: str, run_id: str = "", delay: float = 0) -> str:
        event_id = hashlib.sha256(f"{profile}:{kind}:{key}".encode()).hexdigest()[:32]
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,next_attempt,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                       (event_id, profile, job_id, kind, json.dumps(payload, ensure_ascii=False), now + delay, now, now))
            if run_id:
                db.execute("INSERT OR IGNORE INTO run_events VALUES(?,?)", (run_id, event_id))
        return event_id

    def document(self, path: Path, **kwargs) -> str:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        folder = self.path.parent / "attachments" / digest
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / path.name
        if not target.exists():
            temp = target.with_suffix(target.suffix + f".{os.getpid()}.tmp")
            shutil.copyfile(path, temp)
            os.chmod(temp, 0o600)
            os.replace(temp, target)
        payload = kwargs.pop("payload", {}) | {"path": str(target), "sha256": digest}
        return self.enqueue(kind="document", payload=payload, **kwargs)

    def claim(self) -> dict | None:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM events WHERE status='pending' AND next_attempt<=? ORDER BY created_at,id LIMIT 1", (now,)).fetchone()
            if not row:
                return None
            db.execute("UPDATE events SET status='sending',attempts=attempts+1,updated_at=? WHERE id=?", (now, row["id"]))
            return dict(row) | {"attempts": row["attempts"] + 1}

    def finish(self, event_id: str, status: str, *, receipt: Any = None,
               error: str | None = None, delay: float = 0):
        with self.connect() as db:
            db.execute("UPDATE events SET status=?,receipt=COALESCE(?,receipt),error=?,next_attempt=?,updated_at=? WHERE id=?",
                       (status, json.dumps(receipt) if receipt is not None else None, error, time.time() + delay, time.time(), event_id))

    def recover_sending(self):
        with self.connect() as db:
            db.execute("UPDATE events SET status='uncertain',error='sender restarted before acknowledgement',updated_at=? WHERE status='sending'", (time.time(),))

    def run_events(self, run_id: str, kind: str | None = None) -> list[dict]:
        with self.connect() as db:
            query = "SELECT e.* FROM events e JOIN run_events r ON r.event_id=e.id WHERE r.run_id=?"
            return [dict(r) for r in db.execute(query + (" AND e.kind=?" if kind else ""), (run_id, kind) if kind else (run_id,))]

    def counts(self) -> dict[str, int]:
        with self.connect() as db:
            return dict(db.execute("SELECT status,count(*) FROM events GROUP BY status").fetchall())

    def get(self, key: str, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key: str, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def operation(self, operation_id: str, kind: str, payload: dict, status: str):
        now = time.time()
        with self.connect() as db:
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload,updated_at=excluded.updated_at",
                       (operation_id, kind, json.dumps(payload, ensure_ascii=False), status, now, now))
