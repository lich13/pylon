"""Root-owned, durable state for explicitly requested agent tasks."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

from ..config import redact

ROOT = Path('/var/lib/pylon-agents')
CONFIG = Path('/etc/pylon/agents.json')
TERMINAL = {'succeeded', 'failed', 'uncertain', 'interrupted', 'cancelled', 'timed_out'}
PROVIDERS = {'codex', 'grok'}
PROFILES = {'default', 'webops'}
GMAIL_INPUT_LIMIT = 4 * 1024 * 1024
GMAIL_INPUT_PREFIX = 'pylon-gmail-input:'


def clean_text(value, limit=12000):
    text = redact(str(value))
    text = re.sub(r'(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '[PRIVATE_KEY]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [REDACTED]', text)
    text = re.sub(r'(?i)([?&](?:token|key|secret|password)=)[^&\s]+', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', text)
    return text[-limit:]


def clean_data(value):
    if isinstance(value, dict):
        return {key: clean_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_data(item) for item in value]
    return clean_text(value) if isinstance(value, str) else value


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise PermissionError('unsafe agent directory')
    return path


def limits():
    values = {'execution_seconds': 3600, 'waiting_seconds': 86400, 'retention_days': 30}
    if CONFIG.exists():
        stat = CONFIG.lstat()
        if CONFIG.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o022:
            raise PermissionError('unsafe agent configuration')
        data = json.loads(CONFIG.read_text())
        if set(data) - set(values):
            raise ValueError('unknown agent configuration')
        values.update(data)
    for key, maximum in [('execution_seconds', 86400), ('waiting_seconds', 604800), ('retention_days', 365)]:
        if type(values[key]) is not int or not 1 <= values[key] <= maximum:
            raise ValueError('invalid agent limit: ' + key)
    return values


@contextlib.contextmanager
def database():
    private_dir(ROOT)
    path = ROOT / 'tasks.sqlite3'
    for candidate in (path, ROOT / 'state.lock'):
        if candidate.is_symlink():
            raise PermissionError('unsafe agent state path')
    with (ROOT / 'state.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db = sqlite3.connect(path, timeout=30)
        os.chmod(path, 0o600)
        db.row_factory = sqlite3.Row
        db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            PRAGMA secure_delete=ON;
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, provider TEXT NOT NULL, profile TEXT NOT NULL,
              status TEXT NOT NULL, session_id TEXT NOT NULL DEFAULT '',
              settings TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL,
              updated_at REAL NOT NULL, started_at REAL, waiting_since REAL,
              runtime REAL NOT NULL DEFAULT 0, turns INTEGER NOT NULL DEFAULT 0,
              cancelled INTEGER NOT NULL DEFAULT 0, progress TEXT NOT NULL DEFAULT '',
              result TEXT NOT NULL DEFAULT '{}', cleanup TEXT NOT NULL DEFAULT 'pending',
              cleanup_error TEXT NOT NULL DEFAULT '', cleanup_attempts INTEGER NOT NULL DEFAULT 0,
              cleanup_after REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS requests (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inputs (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
              text TEXT NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, profile TEXT NOT NULL,
              payload TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL);
        ''')
        if 'purpose' not in {r[1] for r in db.execute('PRAGMA table_info(tasks)')}:
            db.execute("ALTER TABLE tasks ADD COLUMN purpose TEXT NOT NULL DEFAULT 'general'")
        try:
            with db:
                yield db
            # Purged prompts must not remain in the WAL after task cleanup.
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            db.close()


def public(row):
    row = dict(row)
    result = {k: row[k] for k in ('id', 'provider', 'profile', 'status', 'created_at', 'updated_at',
                                 'runtime', 'turns', 'progress', 'cleanup', 'cleanup_error')}
    result['result'] = json.loads(row['result'])
    result['purpose'] = row.get('purpose', 'general')
    result['settings'] = json.loads(row['settings'])
    if row['status'] == 'running' and row['started_at']:
        result['runtime'] += max(0, time.time() - row['started_at'])
    return result


def task(db, key):
    if not re.fullmatch(r'[0-9a-f]{8,32}', key):
        raise ValueError('任务编号格式不正确；用 /agent_status 查看编号')
    rows = db.execute('SELECT * FROM tasks WHERE id LIKE ?', (key + '%',)).fetchall()
    if len(rows) != 1:
        raise ValueError('任务不存在或编号不唯一；请使用完整编号')
    return rows[0]


def request_key(key, profile):
    if profile not in PROFILES or not re.fullmatch(r'(?:telegram:' + profile + r':\d+|cli:[0-9a-f]{32})', key):
        raise ValueError('invalid agent request identity')


def prompt_text(value):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 16000 or '\0' in value:
        raise ValueError('任务内容不能为空，且不能超过 16000 字节')
    return value.strip()


def notice(db, task_id):
    row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
    db.execute('INSERT INTO events(id,task_id,profile,payload,created_at) VALUES(?,?,?,?,?)',
               (uuid.uuid4().hex, task_id, row['profile'], json.dumps(public(row), ensure_ascii=False), time.time()))


def submit(provider, profile, key, text):
    request_key(key, profile)
    text = prompt_text(text)
    if provider not in PROVIDERS:
        raise ValueError('unsupported agent provider')
    with database() as db:
        previous = db.execute('SELECT task_id FROM requests WHERE id=?', (key,)).fetchone()
        if previous:
            return public(task(db, previous[0]))
        task_id, now = uuid.uuid4().hex, time.time()
        db.execute('INSERT INTO tasks(id,provider,profile,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                   (task_id, provider, profile, 'queued', now, now))
        db.execute('INSERT INTO requests VALUES(?,?)', (key, task_id))
        db.execute('INSERT INTO inputs(task_id,text) VALUES(?,?)', (task_id, text))
        return public(task(db, task_id))


def reply(task_id, profile, key, text):
    request_key(key, profile)
    text = prompt_text(text)
    with database() as db:
        previous = db.execute('SELECT task_id FROM requests WHERE id=?', (key,)).fetchone()
        if previous:
            return public(task(db, previous[0]))
        row = task(db, task_id)
        if row['purpose'] == 'gmail_review':
            raise ValueError('Gmail 审核已移到网页，请使用 /gmail 打开工作台')
        if row['status'] in TERMINAL or row['cancelled']:
            raise ValueError('任务已结束；线程将自动清理，请用 /agent_codex 或 /agent_grok 发起新任务')
        if row['status'] == 'waiting' and time.time() - row['waiting_since'] >= limits()['waiting_seconds']:
            raise ValueError('等待回答已到期，请发起新任务')
        db.execute('INSERT INTO requests VALUES(?,?)', (key, row['id']))
        db.execute('INSERT INTO inputs(task_id,text) VALUES(?,?)', (row['id'], text))
        db.execute("UPDATE tasks SET status=CASE WHEN status='waiting' THEN 'queued' ELSE status END, waiting_since=NULL,updated_at=? WHERE id=?",
                   (time.time(), row['id']))
        return public(task(db, row['id']))


def gmail_input(target, profile, key, value, *, resume=False):
    """Keep originals only in the private, task-owned workspace, never SQLite."""
    request_key(key, profile)
    if not isinstance(value, str) or len(value.encode()) > GMAIL_INPUT_LIMIT or '\0' in value:
        raise ValueError('完整样本过大，请减少选定样本后重试；未截断原文')
    bundle = json.loads(value)
    if not isinstance(bundle, dict) or not {'prompt', 'examples'} <= set(bundle) or set(bundle) - {'prompt', 'examples', 'context'} or not isinstance(bundle.get('examples'), list):
        raise ValueError('invalid Gmail review input')
    if 'context' in bundle and (not isinstance(bundle['context'], dict) or set(bundle['context']) != {'current_rules'} or not isinstance(bundle['context']['current_rules'], list)):
        raise ValueError('invalid Gmail rule context')
    prompt_text(bundle['prompt'])
    if len(bundle['examples']) > 20:
        raise ValueError('一次最多分析 20 个完整样本')
    with database() as db:
        previous = db.execute('SELECT task_id FROM requests WHERE id=?', (key,)).fetchone()
        if previous:
            return public(task(db, previous[0]))
        now = time.time()
        if resume:
            row = task(db, target)
            if row['purpose'] != 'gmail_review' or row['profile'] != profile or row['status'] in TERMINAL or row['cancelled']:
                raise ValueError('Gmail 审查任务已结束或不属于此会话')
            if row['status'] == 'waiting' and now - row['waiting_since'] >= limits()['waiting_seconds']:
                raise ValueError('Gmail 审查等待已到期，请重新分析')
            task_id = row['id']
        else:
            if target != 'grok':
                raise ValueError('Gmail 审查只使用 Grok')
            task_id = uuid.uuid4().hex
            db.execute("INSERT INTO tasks(id,provider,profile,status,created_at,updated_at,purpose) VALUES(?,?,?,'queued',?,?,'gmail_review')",
                       (task_id, 'grok', profile, now, now))
        parent = private_dir(ROOT / 'work')
        folder = private_dir(parent / task_id)
        if folder.resolve() != folder:
            raise PermissionError('unsafe Gmail workspace')
        name = 'gmail-' + hashlib.sha256(key.encode()).hexdigest()[:24] + '.json'
        fd = os.open(folder / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(bundle, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        db.execute('INSERT INTO requests VALUES(?,?)', (key, task_id))
        db.execute('INSERT INTO inputs(task_id,text) VALUES(?,?)', (task_id, GMAIL_INPUT_PREFIX + name))
        db.execute("UPDATE tasks SET status=CASE WHEN status='waiting' THEN 'queued' ELSE status END,waiting_since=NULL,updated_at=? WHERE id=?", (now, task_id))
        return public(task(db, task_id))


def finish(db, task_id, status, result, *, elapsed=0):
    now = time.time()
    db.execute('UPDATE tasks SET status=?,result=?,updated_at=?,runtime=runtime+?,started_at=NULL,waiting_since=? WHERE id=?',
               (status, json.dumps(result, ensure_ascii=False), now, elapsed, now if status == 'waiting' else None, task_id))
    notice(db, task_id)


def cancel(task_id, profile, key):
    request_key(key, profile)
    with database() as db:
        previous = db.execute('SELECT task_id FROM requests WHERE id=?', (key,)).fetchone()
        if previous:
            return public(task(db, previous[0]))
        row = task(db, task_id)
        db.execute('INSERT INTO requests VALUES(?,?)', (key, row['id']))
        if row['status'] not in TERMINAL:
            db.execute('UPDATE tasks SET cancelled=1,updated_at=? WHERE id=?', (time.time(), row['id']))
            if row['status'] != 'running':
                finish(db, row['id'], 'cancelled', {'summary': '任务已取消。', 'limitations': ['取消不会撤销已完成的修改。']})
        return public(task(db, row['id']))


def rows(task_id=''):
    with database() as db:
        if task_id:
            return [public(task(db, task_id))]
        return [public(r) for r in db.execute("SELECT * FROM tasks ORDER BY CASE WHEN status IN ('queued','running','waiting') THEN 0 ELSE 1 END, created_at DESC LIMIT 10")]


def results():
    with database() as db:
        return [dict(r) for r in db.execute('SELECT * FROM events WHERE acknowledged=0 ORDER BY created_at LIMIT 100')]


def acknowledge(event_id):
    if not re.fullmatch('[0-9a-f]{32}', event_id):
        raise ValueError('invalid event id')
    with database() as db:
        db.execute('UPDATE events SET acknowledged=1 WHERE id=?', (event_id,))


def need_worker():
    with database() as db:
        now = time.time()
        for row in db.execute('SELECT * FROM tasks'):
            if row['status'] in {'queued', 'running'}:
                return True
            if row['status'] == 'waiting' and now - row['waiting_since'] >= limits()['waiting_seconds']:
                return True
            if row['status'] in TERMINAL and row['cleanup'] != 'done' and row['cleanup_after'] <= now:
                if not db.execute('SELECT 1 FROM events WHERE task_id=? AND acknowledged=0', (row['id'],)).fetchone():
                    return True
            if row['cleanup'] == 'done' and now - row['updated_at'] > limits()['retention_days'] * 86400:
                return True
    return False
