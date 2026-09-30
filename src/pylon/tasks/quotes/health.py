"""Quote availability and outage episodes, independent of the quote database."""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import time
import uuid
from pathlib import Path
from zoneinfo import ZoneInfo

from pylon import events
from pylon.config import profile_root, redact
from pylon.outbox import Outbox

KEY = 'quotes:health'
JOB_IDS = ('f3ec3ec24210', 'e961da51fe69')
MAX_AGE = 24 * 60 * 60


def quote_path() -> Path:
    return profile_root('webops') / 'data/electronics_quotes/electronics_quotes.sqlite3'


def _timestamp(value: str) -> float:
    try:
        parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        return parsed.timestamp()
    except (ValueError, TypeError):
        return 0


def inspect(path: Path, *, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    result = dict(available=False, reason='', checked_at=now, validated_at=0, count=0, error='')
    try:
        if not path.is_file():
            raise OSError(f'报价数据库不存在：{path}')
        with sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5) as db:
            result['count'] = db.execute('SELECT count(*) FROM quotes WHERE quote_price BETWEEN 50 AND 50000').fetchone()[0]
            meta = dict(db.execute("SELECT key,value FROM meta WHERE key IN ('quotes_validated_at','last_success_slot','last_quote_error')"))
        result['validated_at'] = max(_timestamp(meta.get('quotes_validated_at', '')),
                                     _timestamp(meta.get('last_success_slot', '')))
        result['error'] = redact(meta.get('last_quote_error', ''))[:500]
        if not result['count']:
            result['reason'] = '没有有效报价'
        elif not result['validated_at']:
            result['reason'] = '没有成功校验记录'
        elif now - result['validated_at'] > MAX_AGE:
            result['reason'] = '超过 24 小时未成功校验报价'
        else:
            result['available'] = True
    except (OSError, sqlite3.Error) as exc:
        result.update(reason='报价数据库不可读', error=redact(f'{type(exc).__name__}: {exc}')[:500])
    return result


def check(path: Path | None = None, *, box: Outbox | None = None, now: float | None = None,
          error: str | None = None, notify: bool = True) -> dict:
    """Durably enqueue one alert and update its episode in one transaction."""
    box = box or Outbox()
    health = inspect(path or quote_path(), now=now)
    if error is not None:
        health['error'] = redact(error)[:500]
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT value FROM kv WHERE key=?', (KEY,)).fetchone()
        previous = json.loads(row[0]) if row else {}
        if not health['available']:
            episode = previous.get('episode') if previous.get('episode') and not previous.get('available') else None
            health['episode'] = episode or uuid.uuid4().hex
            if notify:
                event_id = 'quotes-outage-' + health['episode']
                text = 'KDocs 报价不可用：' + health['reason'] + '。重点比价已暂停，后台继续重试。'
                if health['error']:
                    text += '\n原因：' + health['error']
                ctx = events.context()
                stamp = health['checked_at']
                db.execute("INSERT OR IGNORE INTO events(id,profile,job_id,kind,payload,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                           (event_id, 'webops', JOB_IDS[1], 'text', json.dumps({'text': text}, ensure_ascii=False), stamp, stamp))
                if ctx['run_id']:
                    db.execute('INSERT OR IGNORE INTO run_events VALUES(?,?)', (ctx['run_id'], event_id))
        else:
            health['episode'] = ''
        db.execute('INSERT OR REPLACE INTO kv VALUES(?,?)', (KEY, json.dumps(health, ensure_ascii=False)))
    return health


def status_text(*, box: Outbox | None = None, path: Path | None = None) -> str:
    health = inspect(path or quote_path())
    saved = (box or Outbox()).get(KEY, {})
    stamp = health['validated_at']
    validated = dt.datetime.fromtimestamp(stamp, ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S') if stamp else '无'
    lines = ['报价健康：' + ('可用' if health['available'] else '不可用；' + health['reason']),
             '最近成功校验：' + validated]
    error = health.get('error') or saved.get('error', '')
    if error:
        lines.append('最近失败原因：' + error)
    return '\n'.join(lines)


def suppress_legacy_notices(box: Outbox) -> int:
    """Retain receipts/history, but retire only unsent KDocs routine messages."""
    changed = 0
    with box.connect() as db:
        rows = db.execute("SELECT id,payload FROM events WHERE job_id IN (?,?) AND kind='text' AND status IN ('pending','failed','sending')", JOB_IDS).fetchall()
        for row in rows:
            text = json.loads(row['payload']).get('text', '').lstrip('# ').strip()
            if text.startswith(('KDocs 报价品类变更', 'KDocs 报价抓取连续失败')):
                db.execute("UPDATE events SET status='suppressed',error='KDocs 日常通知已关闭',updated_at=? WHERE id=?", (time.time(), row['id']))
                changed += 1
    return changed
