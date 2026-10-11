"""Owner-issued, single-use web login and revocable bounded sessions."""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from urllib.parse import urlsplit
from pylon.config import home

SCHEMA = '''
CREATE TABLE IF NOT EXISTS market_web_tokens (
 hash TEXT PRIMARY KEY, profile TEXT NOT NULL, expires_at REAL NOT NULL, used_at REAL);
CREATE TABLE IF NOT EXISTS market_web_sessions (
 hash TEXT PRIMARY KEY, profile TEXT NOT NULL, csrf TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS market_web_applies (draft TEXT PRIMARY KEY, version INTEGER NOT NULL);
'''
COOKIE='pylon_market'
COOKIE_PATH='/pylon/'


def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def initialize(db):
    # Do not use executescript: callers may already own an atomic transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():db.execute(statement)


def issue_login(db, profile='webops'):
    if profile!='webops':raise ValueError('仅库库干本人会话可打开交易筛选')
    initialize(db)
    token=secrets.token_urlsafe(32);now=time.time()
    db.execute('DELETE FROM market_web_tokens WHERE expires_at<?',(now,))
    db.execute('DELETE FROM market_web_sessions WHERE expires_at<?',(now,))
    db.execute('INSERT INTO market_web_tokens VALUES(?,?,?,NULL)',(digest(token),profile,now+600))
    return token


def redeem(db, token):
    if not isinstance(token,str) or not 32<=len(token)<=100:raise ValueError('登录链接无效或已过期，请重新发送 /market')
    row=db.execute('SELECT * FROM market_web_tokens WHERE hash=?',(digest(token),)).fetchone()
    now=time.time()
    if not row or row['used_at'] is not None or row['expires_at']<=now:
        raise ValueError('登录链接无效或已过期，请重新发送 /market')
    db.execute('UPDATE market_web_tokens SET used_at=? WHERE hash=?',(now,digest(token)))
    session=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
    db.execute('INSERT INTO market_web_sessions VALUES(?,?,?,?)',(digest(session),row['profile'],csrf,now+7*86400))
    return session,csrf


def public_url():
    config=home()/'config/market-web.json'
    value=json.loads(config.read_text()) if config.exists() else {}
    url=value.get('public_url','')
    parsed=urlsplit(url)
    if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('交易筛选网页尚未配置')
    return url.rstrip('/')+'/'


def login_card(profile, *, store=None, workspace="market", day=''):
    if profile!='webops':return {'text':'请在库库干使用 /intel 打开情报筛选。' if workspace=='intel' else '请在库库干使用 /market 打开交易筛选。'}
    from .store import Store
    url=public_url();store=store or Store()
    if workspace in {'intel','review'}:
        parsed=urlsplit(url);url=parsed.scheme+'://'+parsed.netloc+'/pylon/'+workspace+'/'
    elif workspace!='market':raise ValueError('工作区无效')
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        token=issue_login(db,profile)
    if workspace=='review' and day:
        import re
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',day):raise ValueError('日期无效')
        url+='?day='+day
    label={'review':'每日审核','intel':'情报筛选','market':'交易筛选'}[workspace]
    return {'text':label,'reply_markup':{'inline_keyboard':[[{'text':'打开'+label,'url':url+'#login='+token}]]}}
