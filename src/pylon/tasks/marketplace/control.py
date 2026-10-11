"""Transactional versions, drafts, and audit state for marketplace controls."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from pylon.config import home

SCHEMA = '''
CREATE TABLE IF NOT EXISTS market_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, parent INTEGER, content TEXT NOT NULL,
 created_at REAL NOT NULL, source TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_active (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS market_drafts (
 id TEXT PRIMARY KEY, profile TEXT NOT NULL, base INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 content TEXT NOT NULL, preview TEXT, status TEXT NOT NULL DEFAULT 'draft', updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS market_buttons (
 id TEXT PRIMARY KEY, profile TEXT NOT NULL, action TEXT NOT NULL, payload TEXT NOT NULL,
 version INTEGER NOT NULL, result TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS market_inputs (
 profile TEXT PRIMARY KEY, button TEXT NOT NULL, draft TEXT NOT NULL, field TEXT NOT NULL, category TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_novelty (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, parent TEXT NOT NULL, suggestion TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', announced INTEGER NOT NULL DEFAULT 0,
 first_seen REAL NOT NULL, last_seen REAL NOT NULL, posts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_reviews (
 source TEXT NOT NULL, source_id TEXT NOT NULL, item_index INTEGER NOT NULL,
 reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', updated_at REAL NOT NULL,
 PRIMARY KEY(source,source_id,item_index));
'''


def dumps(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def current(db=None):
    if db is None:
        path=Path(os.environ.get('PYLON_OUTBOX',home()/'data/outbox.sqlite3'))
        if not path.exists():return None
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as connection:
            return current(connection)
    try:
        row=db.execute('SELECT v.id,v.content FROM market_versions v JOIN market_active a ON v.id=a.version WHERE a.id=1').fetchone()
    except sqlite3.OperationalError:
        return None
    return (row[0],json.loads(row[1])) if row else None


def kind_vocabulary():
    """Only reviewed product nouns, never raw posts, go into deduplication context."""
    path=Path(os.environ.get('PYLON_OUTBOX',home()/'data/outbox.sqlite3'))
    if not path.exists():return []
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            rows=db.execute('SELECT kind,parent,suggestion FROM market_novelty ORDER BY last_seen DESC LIMIT 200').fetchall()
            return [{'kind':k,'parent':p,'terms':json.loads(s).get('terms',[])} for k,p,s in rows]
    except sqlite3.OperationalError:
        return []


def baseline(db):
    active=current(db)
    if active:return active
    from . import policy,wanted
    value={'sale':policy.load(home()/'config/marketplace.json'),
           'wanted':wanted.load(home()/'config/marketplace-wanted.json'),'taxonomy':{},'review_actions':{}}
    cursor=db.execute('INSERT INTO market_versions(parent,content,created_at,source) VALUES(NULL,?,?,?)',
                      (dumps(value),time.time(),'migration'))
    db.execute('INSERT INTO market_active VALUES(1,?)',(cursor.lastrowid,))
    return cursor.lastrowid,value


def activate(db,content,base,source):
    active=baseline(db)
    if active[0]!=base:raise ValueError('关注策略已变化，请重新打开草稿并预览。')
    value=json.loads(dumps(content))
    value['sale']['version']=int(active[1]['sale']['version'])+1
    value['wanted']['version']=int(active[1]['wanted']['version'])+1
    cursor=db.execute('INSERT INTO market_versions(parent,content,created_at,source) VALUES(?,?,?,?)',
                      (base,dumps(value),time.time(),source))
    db.execute('UPDATE market_active SET version=? WHERE id=1',(cursor.lastrowid,))
    for key,action in value.get('review_actions',{}).items():
        db.execute('UPDATE market_novelty SET status=? WHERE id=?',(action,key))
    return cursor.lastrowid


def review(db,post,index,reason):
    if post.get('history'):return
    db.execute('''INSERT INTO market_reviews VALUES(?,?,?,?,'pending',?)
        ON CONFLICT(source,source_id,item_index) DO UPDATE SET reason=excluded.reason,
        status='pending',updated_at=excluded.updated_at''',
        (post['source'],post['source_id'],index,reason,time.time()))


def audit(db,post,decisions,*,status,event_id=None,policy_version=None):
    from .classifier import VERSION
    value={'parser_version':VERSION,'at':time.time(),'policy_version':policy_version,
           'body_complete':bool(post['body_complete']),'items':decisions,
           'status':status,'event_id':event_id}
    db.execute('UPDATE marketplace_posts SET decision=? WHERE source=? AND source_id=?',
               (dumps(value),post['source'],post['source_id']))
    from pylon.tasks.daily_review.store import record
    products=db.execute('SELECT items FROM marketplace_posts WHERE source=? AND source_id=?',(post['source'],post['source_id'])).fetchone()
    by_index={i.get('index',n):i for n,i in enumerate(json.loads(products[0]))} if products else {}
    review_value={**value,'items':[{**d,'item':by_index.get(d['index'],{})} for d in decisions]}
    record(db,post['source'],post['source_id'],lane='market',status=status,result=review_value,
           event_id=event_id,rule_version=policy_version)
