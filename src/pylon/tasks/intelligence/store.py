from __future__ import annotations

import json
import time
from urllib.parse import urlsplit
from pylon.outbox import Outbox
from pylon.tasks.marketplace.control import digest,dumps
from . import policy

SCHEMA='''
CREATE TABLE IF NOT EXISTS intel_items (
 source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL,
 published_at TEXT NOT NULL, observed_at REAL NOT NULL, updated_at REAL NOT NULL, fingerprint TEXT NOT NULL,
 evidence_scope TEXT NOT NULL, context TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 result TEXT, event_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 lease_until REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT '',
 historical INTEGER NOT NULL DEFAULT 0, policy_version INTEGER,
 semantic_attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source,source_id));
CREATE INDEX IF NOT EXISTS intel_ready ON intel_items(source,status,next_attempt,lease_until);
CREATE TABLE IF NOT EXISTS intel_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, parent INTEGER, content TEXT NOT NULL, created_at REAL NOT NULL, origin TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intel_drafts (
 id TEXT PRIMARY KEY, base INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL,
 preview TEXT, status TEXT NOT NULL DEFAULT 'draft', applied_version INTEGER);
CREATE TABLE IF NOT EXISTS intel_receipts (
 fact_key TEXT PRIMARY KEY, event_id TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS intel_model_cache (key TEXT PRIMARY KEY,result TEXT NOT NULL,created_at REAL NOT NULL);
'''


def initialize(db):
    for statement in SCHEMA.split(';'):
        if statement.strip():db.execute(statement)
    if 'semantic_attempts' not in {r[1] for r in db.execute('PRAGMA table_info(intel_items)')}:
        db.execute('ALTER TABLE intel_items ADD COLUMN semantic_attempts INTEGER NOT NULL DEFAULT 0')
    from .attempts import SCHEMA as ATTEMPTS
    db.execute(ATTEMPTS)


def active(db):
    row=db.execute('SELECT * FROM intel_versions ORDER BY id DESC LIMIT 1').fetchone()
    if not row:
        value=policy.default()
        db.execute('INSERT INTO intel_versions(content,created_at,origin) VALUES(?,?,?)',(dumps(value),time.time(),'migration'))
        return active(db)
    return row['id'],json.loads(row['content'])


class Store:
    def __init__(self,box=None):
        self.box=box or Outbox()
        with self.box.connect() as db:initialize(db)

    def ingest(self,source,source_id,*,title,body,url,published_at='',evidence_scope='excerpt',context=None,historical=False):
        if source not in policy.SOURCES:raise ValueError('unknown intelligence source')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('invalid intelligence source link')
        context=context or {};now=time.time()
        stable=context.get('snapshot',{}).get('fingerprint',{k:v for k,v in context.items() if k!='first_seen'})
        fp=digest([title,body,published_at,evidence_scope,stable])
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM intel_items WHERE source=? AND source_id=?',(source,str(source_id))).fetchone()
            from pylon.tasks.daily_review.migrate import first_seen as origin_time
            observed,_=origin_time(db,source,str(source_id),old['observed_at'] if old else now)
            from pylon.tasks.daily_review.store import capture
            capture(db,source,str(source_id),lane='intel',title=title,body=body,url=url,published_at=published_at,
                    evidence_scope=evidence_scope,fingerprint=fp,
                    first_seen=context.get('first_seen') or context.get('snapshot',{}).get('first_seen') or observed,
                    restored=historical,metadata={'historical':historical})
            if old and old['fingerprint']==fp:
                # Refresh the source-version CAS token, without reprocessing an
                # unchanged summary just because it was observed again.
                db.execute('UPDATE intel_items SET context=? WHERE source=? AND source_id=?',(dumps(context),source,str(source_id)))
                return False
            if old:
                # Never mutate an immutable queued event; changes form new facts.
                db.execute("UPDATE intel_items SET title=?,body=?,url=?,published_at=?,updated_at=?,fingerprint=?,evidence_scope=?,context=?,historical=?,status=?,attempts=0,semantic_attempts=0,next_attempt=0,lease_until=0,error='',phase='',result=NULL WHERE source=? AND source_id=?",
                    (title,body,url,published_at,now,fp,evidence_scope,dumps(context),int(historical),'historical' if historical else 'pending',source,str(source_id)))
            else:
                db.execute('INSERT INTO intel_items(source,source_id,title,body,url,published_at,observed_at,updated_at,fingerprint,evidence_scope,context,historical,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (source,str(source_id),title,body,url,published_at,now,now,fp,evidence_scope,dumps(context),int(historical),'historical' if historical else 'pending'))
        return True

    def cached(self,key):
        with self.box.connect() as db:row=db.execute('SELECT result FROM intel_model_cache WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None

    def cache(self,key,result):
        with self.box.connect() as db:db.execute('INSERT OR REPLACE INTO intel_model_cache VALUES(?,?,?)',(key,dumps(result),time.time()))

    def claim(self,source,limit=2):
        now=time.time()
        limit=max(1,min(200,limit))
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            sql="SELECT * FROM intel_items WHERE source=? AND historical=0 AND status IN ('pending','retry') AND next_attempt<=? AND lease_until<=?"
            rows=[dict(r) for r in db.execute(sql+' ORDER BY observed_at LIMIT ?',(source,now,now,max(1,limit//2)))]
            # The source execution lock owns these claims until its process
            # ends. A long model response is not evidence of a dead owner.
            lease=1e30
            for row in rows:db.execute('UPDATE intel_items SET lease_until=? WHERE source=? AND source_id=?',(now+lease,source,row['source_id']))
            fresh=[dict(r) for r in db.execute(sql+' ORDER BY updated_at DESC LIMIT ?',(source,now,now,limit-len(rows)))]
            for row in fresh:db.execute('UPDATE intel_items SET lease_until=? WHERE source=? AND source_id=?',(now+lease,source,row['source_id']))
            rows+=fresh
        return rows
