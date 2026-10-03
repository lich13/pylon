"""Transactional Gmail review documents, with one-time legacy JSON import."""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
from pathlib import Path
import sqlite3

_current = contextvars.ContextVar('gmail_store', default=None)


@contextlib.contextmanager
def transaction(directory: Path):
    current = _current.get()
    if current is not None:
        if current[0] != directory:
            raise ValueError('Gmail transaction directory mismatch')
        yield current[1]
        return
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / 'review.sqlite3'
    db = sqlite3.connect(path, timeout=60)
    token = _current.set((directory, db))
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        os.chmod(path, 0o600)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        _current.reset(token)
        db.close()


def read(path: Path, default):
    with transaction(path.parent) as db:
        row = db.execute('SELECT value FROM documents WHERE name=?', (path.name,)).fetchone()
        if row:
            return json.loads(row[0])
        # Fail closed on malformed legacy state; never replace rules with an
        # empty fallback during migration.
        value = json.loads(path.read_text()) if path.exists() else default
        db.execute('INSERT INTO documents VALUES(?,?)', (path.name, json.dumps(value, ensure_ascii=False)))
        return value


def write(path: Path, value):
    with transaction(path.parent) as db:
        db.execute('INSERT OR REPLACE INTO documents VALUES(?,?)',
                   (path.name, json.dumps(value, ensure_ascii=False)))


def migrate(paths):
    for path, default in paths:
        read(path, default)
