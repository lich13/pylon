"""Live ownership for model queues. Elapsed time never revokes a running request."""
from __future__ import annotations

import fcntl
import hashlib
import os
import threading
import time
import uuid
from contextvars import ContextVar

current = ContextVar('pylon_model_execution', default=None)
SCHEMA = '''CREATE TABLE IF NOT EXISTS model_executions (
 component TEXT PRIMARY KEY,owner TEXT NOT NULL,pid INTEGER NOT NULL,status TEXT NOT NULL,
 started_at REAL NOT NULL,heartbeat_at REAL NOT NULL,request_id TEXT NOT NULL DEFAULT '',
 source TEXT NOT NULL DEFAULT '',source_id TEXT NOT NULL DEFAULT '',fingerprint TEXT NOT NULL DEFAULT '');'''


def lock_path(box, component):
    folder = box.path.resolve().parent / 'model-locks'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder / (hashlib.sha256(component.encode()).hexdigest() + '.lock')


def active(box, component):
    with lock_path(box, component).open('a') as handle:
        try: fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


class Execution:
    def __init__(self, box, component):
        self.box, self.component = box, component
        self.owner = uuid.uuid4().hex
        self.stop = threading.Event()
        self.handle = self.thread = self.token = None

    def __enter__(self):
        self.handle = lock_path(self.box, self.component).open('a')
        try: fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.handle.close(); self.handle = None
            return None
        try:
            now = time.time()
            with self.box.connect() as db:
                db.execute(SCHEMA)
                db.execute('INSERT OR REPLACE INTO model_executions VALUES(?,?,?,?,?,?,\'\',\'\',\'\',\'\')',
                           (self.component, self.owner, os.getpid(), 'running', now, now))
            self.token = current.set(self)
            self.thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.handle.close(); self.handle = None
            raise

    def _heartbeat(self):
        while not self.stop.wait(15):
            try:
                with self.box.connect() as db:
                    db.execute('UPDATE model_executions SET heartbeat_at=? WHERE component=? AND owner=?',
                               (time.time(), self.component, self.owner))
            except Exception:
                # Database contention cannot revoke the OS-held lock or restart a request.
                pass

    def track(self, source, source_id, fingerprint=''):
        with self.box.connect() as db:
            db.execute("UPDATE model_executions SET source=?,source_id=?,fingerprint=?,request_id='',started_at=?,heartbeat_at=? WHERE component=? AND owner=?",
                       (source, str(source_id), fingerprint, time.time(), time.time(), self.component, self.owner))

    def observe(self, event, fields):
        with self.box.connect() as db:
            db.execute('UPDATE model_executions SET request_id=?,heartbeat_at=? WHERE component=? AND owner=?',
                       (fields.get('request_id', ''), time.time(), self.component, self.owner))

    def __exit__(self, kind, value, traceback):
        if not self.handle: return
        self.stop.set()
        try:
            if self.thread: self.thread.join(timeout=1)
            with self.box.connect() as db:
                db.execute('UPDATE model_executions SET status=?,heartbeat_at=? WHERE component=? AND owner=?',
                           ('failed' if kind else 'complete', time.time(), self.component, self.owner))
        finally:
            if self.token is not None: current.reset(self.token)
            self.handle.close(); self.handle = None
