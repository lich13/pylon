"""Owner web sessions shared by independently deployed business modules.

The existing table and cookie names are a compatibility contract. Tokens are
single use; callers own the database transaction and HTTP cookie handling.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlsplit

from . import home, profile_env

COOKIE = 'pylon_market'
COOKIE_PATH = '/pylon/'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS market_web_tokens (
 hash TEXT PRIMARY KEY, profile TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS market_web_sessions (
 hash TEXT PRIMARY KEY, profile TEXT NOT NULL, csrf TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS market_web_applies (draft TEXT PRIMARY KEY, version INTEGER NOT NULL);
'''


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def initialize(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)


def same_owner():
    owner = profile_env('default').get('TELEGRAM_HOME_CHANNEL', '')
    return bool(owner and owner == profile_env('webops').get('TELEGRAM_HOME_CHANNEL'))


def issue_login(db, profile='webops', *, workspace='market'):
    if workspace not in {'market', 'intel', 'review', 'gmail'}:
        raise ValueError('工作区不存在')
    if profile not in {'default', 'webops'} or (workspace != 'gmail' and profile != 'webops'):
        raise ValueError('请从对应机器人的本人会话打开网页')
    if workspace == 'gmail' and profile == 'webops' and not same_owner():
        raise ValueError('请在 Gmail 所属的本人机器人中打开网页')
    # Both configured bots belong to the same person in a shared workspace.
    if workspace == 'gmail' and profile == 'default' and same_owner():
        profile = 'webops'
    initialize(db)
    token, now = secrets.token_urlsafe(32), time.time()
    db.execute('DELETE FROM market_web_tokens WHERE expires_at<?', (now,))
    db.execute('DELETE FROM market_web_sessions WHERE expires_at<?', (now,))
    db.execute('INSERT INTO market_web_tokens VALUES(?,?,?,NULL)', (digest(token), profile, now + 600))
    return token


def redeem(db, token):
    if not isinstance(token, str) or not 32 <= len(token) <= 100:
        raise ValueError('登录链接无效或已过期，请重新从机器人打开网页')
    row = db.execute('SELECT * FROM market_web_tokens WHERE hash=?', (digest(token),)).fetchone()
    now = time.time()
    if not row or row['used_at'] is not None or row['expires_at'] <= now:
        raise ValueError('登录链接无效或已过期，请重新从机器人打开网页')
    changed = db.execute('UPDATE market_web_tokens SET used_at=? WHERE hash=? AND used_at IS NULL',
                         (now, digest(token))).rowcount
    if changed != 1:
        raise ValueError('登录链接已使用，请重新从机器人打开网页')
    session, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    db.execute('INSERT INTO market_web_sessions VALUES(?,?,?,?)', (digest(session), row['profile'], csrf, now + 7 * 86400))
    return session, csrf


def session(db, cookie, workspace='market'):
    row = db.execute('SELECT * FROM market_web_sessions WHERE hash=? AND expires_at>?',
                     (digest(cookie or ''), time.time())).fetchone()
    if not row:
        return None
    if workspace == 'gmail':
        return row if row['profile'] == 'default' or (row['profile'] == 'webops' and same_owner()) else None
    return row if row['profile'] == 'webops' else None


def valid_write(session_row, csrf, origin, expected):
    return origin == expected and hmac.compare_digest(csrf or '', session_row['csrf'])


def public_url(workspace='market'):
    if workspace not in {'market', 'intel', 'review', 'gmail'}:
        raise ValueError('工作区不存在')
    config = home() / 'config/market-web.json'
    value = json.loads(config.read_text()) if config.exists() else {}
    url = value.get('public_url', '') if isinstance(value, dict) else ''
    if not isinstance(url, str) or re.search(r'[\s\\]', url):
        raise ValueError('网页入口尚未配置')
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise ValueError('网页入口尚未配置') from None
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or port == 0:
        raise ValueError('网页入口尚未配置')
    return parsed.scheme + '://' + parsed.netloc + '/pylon/' + workspace + '/'
