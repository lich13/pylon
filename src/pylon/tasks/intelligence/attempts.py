"""Durable request budgets per input version and processing stage."""
from __future__ import annotations

import re
import time
import uuid
from pylon.config import redact
from pylon.shared.model_requests import observer

SCHEMA = '''CREATE TABLE IF NOT EXISTS intel_attempts (
 id TEXT PRIMARY KEY,source TEXT NOT NULL,source_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
 phase TEXT NOT NULL,number INTEGER NOT NULL,status TEXT NOT NULL,started_at REAL NOT NULL,
 finished_at REAL,request_id TEXT NOT NULL DEFAULT '',elapsed REAL NOT NULL DEFAULT 0,
 error TEXT NOT NULL DEFAULT '',semantic INTEGER NOT NULL DEFAULT 0,
 round INTEGER NOT NULL DEFAULT 0,UNIQUE(source,source_id,fingerprint,phase,round,number));'''
ROUNDS='''CREATE TABLE IF NOT EXISTS intel_retry_rounds (
 source TEXT NOT NULL,source_id TEXT NOT NULL,fingerprint TEXT NOT NULL,phase TEXT NOT NULL,
 round INTEGER NOT NULL DEFAULT 0,requested_at REAL NOT NULL,PRIMARY KEY(source,source_id,fingerprint,phase));'''


class BudgetError(Exception):
    def __init__(self, message, *, next_attempt=0):
        super().__init__(message); self.next_attempt=next_attempt


def safe_error(error):
    # Transport exceptions can contain response text, request URLs or headers.
    # Keep their class at the call site and only store a fixed diagnosis here.
    import httpx
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return '请求超时'
    if isinstance(error, (ConnectionError, httpx.NetworkError)):
        return '连接失败'
    return re.sub(r'https?://\S+', '[url]', redact(str(error)))[:300]


def semantic(error):
    from pylon.tasks.linuxdo import judge, trade
    from pylon.tasks.software.monitor import GrokDigestError
    return (isinstance(error, (judge.JudgeError, trade.TradeError, ValueError)) or
            isinstance(error, GrokDigestError) and bool(re.search(r'returned (?:an? )?(?:unexpected|invalid)|text is not', str(error))))


def history(db, item, phase):
    db.execute(SCHEMA);db.execute(ROUNDS)
    return [dict(r) for r in db.execute('''SELECT a.* FROM intel_attempts a
        WHERE source=? AND source_id=? AND fingerprint=? AND phase=?
        AND round=coalesce((SELECT r.round FROM intel_retry_rounds r WHERE r.source=a.source AND r.source_id=a.source_id
        AND r.fingerprint=a.fingerprint AND r.phase=a.phase),0) ORDER BY number''',
        (item['source'], item['source_id'], item['fingerprint'], phase))]


def exhausted(rows):
    return len(rows) >= 3 or sum(r['semantic'] for r in rows) >= 2


def call(store, item, phase, request, validate):
    now = time.time()
    with store.box.connect() as db:
        db.execute(SCHEMA); db.execute('BEGIN IMMEDIATE')
        rows = history(db, item, phase)
        if exhausted(rows): raise BudgetError('本内容在此阶段已用尽请求次数')
        if rows:
            previous = rows[-1]
            due = (previous['finished_at'] or previous['started_at']) + (60 if len(rows) == 1 else 300)
            if due > now: raise BudgetError('等待重试间隔', next_attempt=due)
        aid = uuid.uuid4().hex
        cycle=db.execute('SELECT round FROM intel_retry_rounds WHERE source=? AND source_id=? AND fingerprint=? AND phase=?',
                         (item['source'],item['source_id'],item['fingerprint'],phase)).fetchone()
        db.execute('INSERT INTO intel_attempts(id,source,source_id,fingerprint,phase,number,status,started_at,round) VALUES(?,?,?,?,?,?,\'started\',?,?)',
                   (aid, item['source'], item['source_id'], item['fingerprint'], phase, len(rows)+1, now,cycle[0] if cycle else 0))
    metadata = {}; begun = time.monotonic()
    def observe(event, fields):
        metadata.update(fields)
        if event == 'start':
            with store.box.connect() as db: db.execute('UPDATE intel_attempts SET request_id=? WHERE id=?', (fields['request_id'], aid))
    token = observer.set(observe)
    try:
        result = validate(request())
        with store.box.connect() as db: db.execute('UPDATE intel_attempts SET status=\'success\',finished_at=?,elapsed=? WHERE id=?',
                                                  (time.time(), time.monotonic()-begun, aid))
        return result
    except Exception as error:
        with store.box.connect() as db:
            db.execute('UPDATE intel_attempts SET status=\'failed\',finished_at=?,elapsed=?,request_id=?,error=?,semantic=? WHERE id=?',
                       (time.time(), getattr(error, 'elapsed', time.monotonic()-begun), getattr(error, 'request_id', metadata.get('request_id', '')),
                        type(error).__name__+': '+safe_error(error), int(semantic(error)), aid))
        raise
    finally: observer.reset(token)


def correction(store, item, phase):
    with store.box.connect() as db: rows = history(db, item, phase)
    return next((r['error'] for r in reversed(rows) if r['semantic']), '')


def retry(db, source, source_id):
    db.execute(SCHEMA);db.execute(ROUNDS)
    row=db.execute('SELECT * FROM intel_items WHERE source=? AND source_id=?',(source,source_id)).fetchone()
    if not row or row['status']!='failed':raise ValueError('仅可明确重试已耗尽的失败记录')
    captured=row['observed_at']
    if source=='linuxdo' and db.execute("SELECT 1 FROM sqlite_master WHERE name='linuxdo_topics'").fetchone():
        original=db.execute('SELECT first_seen FROM linuxdo_topics WHERE topic_id=?',(source_id,)).fetchone()
        if original and original[0]:captured=min(captured,original[0])
    if row['historical'] or row['event_id'] or captured<time.time()-86400:
        raise ValueError('历史或已有关联投递的记录仅可隔离回放')
    db.execute('''INSERT INTO intel_retry_rounds VALUES(?,?,?,?,1,?) ON CONFLICT(source,source_id,fingerprint,phase)
        DO UPDATE SET round=round+1,requested_at=excluded.requested_at''',
        (source,source_id,row['fingerprint'],row['phase'],time.time()))
    db.execute("UPDATE intel_items SET status='pending',attempts=0,semantic_attempts=0,next_attempt=0,lease_until=0,error='' WHERE source=? AND source_id=?",(source,source_id))
