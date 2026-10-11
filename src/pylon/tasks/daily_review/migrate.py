"""Recover retained public inputs without calling collectors or delivery code."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from pylon.config import home
from pylon.outbox import Outbox
from . import store


def exists(db, table):
    return bool(db.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone())


def first_seen(db, source, source_id, fallback):
    if source == 'linuxdo' and exists(db, 'linuxdo_topics'):
        row = db.execute('SELECT first_seen FROM linuxdo_topics WHERE topic_id=?', (source_id,)).fetchone()
        if row: return row[0], 'source_restored'
    return fallback, 'retained'


def legacy_failure(row):
    return {'reason':row['error'],'phase':row['phase'],
            'legacy_attempts':row['attempts'],'failure_time_unknown':True}


def retained_xianbao(db, native):
    if native is None:return 0
    count=0
    for row in native.execute('SELECT *,min(first_seen_at) OVER (PARTITION BY id) original_at FROM seen_posts ORDER BY last_seen_at'):
        if store.day_of(row['original_at'])<store.START:continue
        article=native.execute('SELECT body FROM article_cache WHERE url=?',(row['url'],)).fetchone() if exists(native,'article_cache') else None
        sid=store.capture(db,'xianbao',row['id'],lane='intel',title=row['title'],body=article[0] if article else '',url=row['url'],
            first_seen=row['original_at'],date_quality='source_restored',restored=True,
            fingerprint='native:'+store.digest([row['id'],row['rules_hash'],row['fingerprint']]),
            metadata={'original_input_version_unknown':True},
            evidence_scope='forum_body' if article and article[0] else 'title_only')
        store.record(db,'xianbao',row['id'],lane='intel',snapshot_id=sid,
            status='excluded' if not row['decision_keep'] else 'unknown',
            result={'reason':row['reason'],'tier':row['tier'],'score':row['score'],
                    'original_delivery_unknown':bool(row['decision_keep']),'original_input_version_unknown':True},
            rule_version=row['rules_hash'],decided_at=row['last_seen_at'],restored=True)
        count+=1
    return count


def retained_linuxdo_decisions(db):
    if not all(exists(db,t) for t in ('intel_items','linuxdo_decisions','linuxdo_topics')):return 0
    count=0
    # Restrict to public inputs already admitted by the Helper pipeline. A cached
    # old decision is retained, but its overwritten input version is not invented.
    rows=db.execute('''SELECT d.*,i.title,i.body,i.url,i.published_at,t.first_seen FROM linuxdo_decisions d
        JOIN intel_items i ON i.source='linuxdo' AND i.source_id=d.topic_id
        JOIN linuxdo_topics t ON t.topic_id=d.topic_id ORDER BY d.created_at''').fetchall()
    for row in rows:
        sid=store.capture(db,'linuxdo',row['topic_id'],lane='intel',title=row['title'],body=row['body'],url=row['url'],
            published_at=row['published_at'],first_seen=row['first_seen'],date_quality='source_restored',
            evidence_scope='helper_excerpt',fingerprint='legacy:'+row['cache_key'],restored=True,
            metadata={'original_input_version_unknown':True})
        if sid is None:continue
        value=json.loads(row['result']);value['original_input_version_unknown']=True
        state={'skip':'excluded','hold':'needs_info'}.get(value.get('decision'),'unknown')
        store.record(db,'linuxdo',row['topic_id'],lane='intel',snapshot_id=sid,status=state,result=value,
            rule_version='legacy:'+row['cache_key'],decided_at=row['created_at'],restored=True)
        count+=1
    return count


def migrate(box=None, *, native_path=None):
    box = box or Outbox(); totals = {'intel':0,'market':0,'xianbao_retained':0,'before_start':0}
    native_path = Path(native_path or home()/'profiles/default/data/xianbao_filter/state.sqlite3')
    native = sqlite3.connect(native_path.resolve().as_uri()+'?mode=ro', uri=True) if native_path.exists() else None
    if native: native.row_factory = sqlite3.Row
    try:
        with box.connect() as db:
            store.initialize(db)
            totals['xianbao_retained']=retained_xianbao(db,native)
            totals['linuxdo_retained_decisions']=retained_linuxdo_decisions(db)
            if exists(db, 'intel_items'):
                for raw in db.execute('SELECT * FROM intel_items').fetchall():
                    row = dict(raw); context = json.loads(row['context']); at, quality = first_seen(db,row['source'],row['source_id'],row['observed_at'])
                    if native and row['source']=='xianbao':
                        original = native.execute('SELECT min(first_seen_at) FROM seen_posts WHERE id=?',(row['source_id'],)).fetchone()[0]
                        if original: at,quality = original,'source_restored'
                    sid = store.capture(db,row['source'],row['source_id'],lane='intel',title=row['title'],body=row['body'],url=row['url'],
                        published_at=row['published_at'],first_seen=at,date_quality=quality,evidence_scope=row['evidence_scope'],
                        fingerprint=row['fingerprint'],restored=True)
                    if sid is None: totals['before_start']+=1; continue
                    result = json.loads(row['result']) if row['result'] else {}
                    if row['error']:
                        result=legacy_failure(row) if row['status']=='failed' and '；旧版本累计重试 ' in row['error'] else {
                            'reason':row['error'],'phase':row['phase'],'attempts':row['attempts'],
                            'failure_time_unknown':True,'last_record_update':row['updated_at']}
                    status = row['status']
                    if not result and status not in {'pending','retry'}:status='unknown'
                    store.record(db,row['source'],row['source_id'],lane='intel',status=status,result=result,
                                 rule_version=row['policy_version'],event_id=row['event_id'] if status in {'queued','delivered'} else None,
                                 snapshot_id=sid,decided_at=row['updated_at'],restored=True)
                    totals['intel']+=1
            if exists(db,'marketplace_posts'):
                for raw in db.execute('SELECT * FROM marketplace_posts').fetchall():
                    row=dict(raw);at,quality=first_seen(db,row['source'],row['source_id'],row['first_seen'])
                    # Historical crawling time cannot prove actual first capture.
                    if row['history'] and store.day_of(at)>=store.START: at,quality=None,'unknown'
                    context=json.loads(row.get('source_context') or '{}')
                    sid=store.capture(db,row['source'],row['source_id'],lane='market',title=row['title'],body=row['body'],url=row['url'],
                        published_at=row['posted_at'],first_seen=at,date_quality=quality,restored=True,
                        evidence_scope=context.get('evidence_scope') or ('forum_body' if row['body_complete'] else 'excerpt'),
                        metadata={'body_complete':bool(row['body_complete']),'platform_category':row.get('platform_category','')})
                    if sid is None:totals['before_start']+=1;continue
                    decision=json.loads(row.get('decision') or '{}');products=json.loads(row['items'])
                    indexed={i.get('index',n):i for n,i in enumerate(products)}
                    decision['items']=[{**d,'item':indexed.get(d.get('index'),{})} for d in decision.get('items',[])] or [{'item':i} for i in products]
                    if row['error']:decision['error']=row['error']
                    status=row['status'] if decision.get('at') or row.get('event_id') or row.get('error') else 'unknown'
                    store.record(db,row['source'],row['source_id'],lane='market',status=status,result=decision,
                        rule_version=row['policy_version'],event_id=row['event_id'],snapshot_id=sid,decided_at=decision.get('at') or row['updated_at'],restored=True)
                    totals['market']+=1
            store.sync_deliveries(db)
            totals['entries']=db.execute('SELECT count(*) FROM review_entries').fetchone()[0]
            totals['days']=[dict(r) for r in db.execute('SELECT day,count(*) count FROM review_entries GROUP BY day')]
    finally:
        if native:native.close()
    return totals


def cap_legacy_retries(box=None):
    """Older unbounded attempts are exhausted, not silently reset by deployment."""
    from pylon.tasks.intelligence import attempts
    box=box or Outbox();count=0
    with box.connect() as db:
        db.execute(attempts.SCHEMA);store.initialize(db)
        for raw in db.execute("SELECT * FROM intel_items WHERE status='retry' AND attempts>=3").fetchall():
            row=dict(raw)
            if attempts.history(db,row,row['phase']):continue
            # Request times/IDs were not recorded by the old code. Do not invent them.
            reason=row['error']+'；旧版本累计重试 '+str(row['attempts'])+' 次，已停止'
            db.execute("UPDATE intel_items SET status='failed',next_attempt=0,lease_until=0,result=NULL,error=? WHERE source=? AND source_id=?",
                       (reason,row['source'],row['source_id']))
            store.record(db,row['source'],row['source_id'],lane='intel',status='failed',
                         result=legacy_failure({**row,'error':reason}),rule_version=row['policy_version'],
                         decided_at=row['updated_at'],restored=True)
            count+=1
        for table,phase,waiting in [('marketplace_analyses','product_extraction','pending_analysis'),
                                    ('market_ai_prices','ai_price','pending_price')]:
            if not exists(db,table):continue
            for row in db.execute(f"SELECT * FROM {table} WHERE attempts>=3 AND status IN ('pending','running')").fetchall():
                db.execute(f"UPDATE {table} SET status='error',next_attempt=0,error=? WHERE fingerprint=?",
                           ('旧版本累计请求已达上限，已停止；'+row['error'],row['fingerprint']))
                post=db.execute('SELECT * FROM marketplace_posts WHERE source=? AND source_id=?',(row['source'],row['source_id'])).fetchone()
                same_version=False
                if post:
                    if table=='marketplace_analyses':
                        from pylon.tasks.marketplace import enrich
                        item=dict(post);item['_required_fields']=json.loads(row['requirements'])
                        same_version=enrich.fingerprint(item,row['model'])==row['fingerprint']
                    else:
                        from pylon.tasks.marketplace import ai_prices
                        same_version=ai_prices.fingerprint(dict(post),ai_prices.config(db))==row['fingerprint']
                if same_version and post['status']==waiting:
                    db.execute("UPDATE marketplace_posts SET status='needs_review',error=? WHERE source=? AND source_id=?",
                               (phase+': 旧版本累计请求已达上限',row['source'],row['source_id']))
                    store.record(db,row['source'],row['source_id'],lane='market',status='failed',
                        result={'reason':row['error'],'phase':phase,'legacy_attempts':row['attempts'],'failure_time_unknown':True},restored=True)
                count+=1
    return count
