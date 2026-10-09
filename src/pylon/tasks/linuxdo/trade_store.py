"""Atomic trade decisions and seller/offer deduplication across topics."""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal

from .common import JOB_ID, digest, timestamp
from . import trade


def initialize(db) -> None:
    db.executescript('''
        CREATE TABLE IF NOT EXISTS linuxdo_public_reviews (
            message_id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL, status TEXT NOT NULL,
            next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS linuxdo_trade_quotes (
            topic_id TEXT NOT NULL, offer_key TEXT NOT NULL, policy_revision TEXT NOT NULL,
            result TEXT NOT NULL, observed_at REAL NOT NULL, PRIMARY KEY(topic_id,offer_key));
        CREATE TABLE IF NOT EXISTS linuxdo_trade_notifications (
            offer_key TEXT NOT NULL, event_key TEXT NOT NULL, event_id TEXT NOT NULL,
            total TEXT NOT NULL, comparable_price TEXT NOT NULL, created_at REAL NOT NULL,
            PRIMARY KEY(offer_key,event_key));
        CREATE INDEX IF NOT EXISTS linuxdo_trade_notified ON linuxdo_trade_notifications(offer_key,created_at);
    ''')


def identity(snapshot: dict, result: dict) -> str:
    offer = result['offer']
    ref = offer['description']['ref']
    actor = snapshot.get('actors', {}).get(ref)
    if not actor:
        return ''
    # Advertising and warranty prose must not create a fresh listing identity.
    # Changes in benefits need an explicitly evidenced event instead.
    terms = {key: trade.normal(offer[key]['value']) for key in ('participants', 'quota', 'multiplier')}
    terms['days'] = str((trade.decimal(offer['term']['value']) *
                         (30 if offer['term_unit'] == 'month' else 1)).normalize()) if offer['term']['value'] else ''
    spec = dict(result['spec'])
    if offer['delivery'] != 'api':
        for optional in ('provider', 'model', 'quota_unit', 'quota_period'):
            spec.pop(optional, None)
    return digest([actor, spec, terms])


def refresh_policy(store, config: dict, focus: dict, model: str) -> None:
    fingerprint = digest([config, focus, model, trade.VERSION])
    now = time.time()
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute("SELECT value FROM linuxdo_meta WHERE key='trade_policy' ").fetchone()
        if previous and previous[0] != fingerprint:
            db.execute('''UPDATE linuxdo_topics SET status='pending',next_attempt=0,version=version+1
                WHERE historical=0 AND status IN ('skip','hold') AND updated_at>?
                AND topic_id IN (SELECT topic_id FROM linuxdo_trade_quotes)''', (now - 7 * 86400,))
        db.execute("INSERT OR REPLACE INTO linuxdo_meta VALUES('trade_policy',?)", (fingerprint,))
        expired = sorted(rule['id'] for rule in config['rules'] if rule['car_monthly_reference']
                         and (timestamp(rule['reference_until']) or 0) <= now)
        if expired:
            key = digest([config['revision'], expired])
            store.box.enqueue(profile='webops', job_id=JOB_ID, kind='text',
                              payload={'text': 'Linux.do 拼车整号参考价已到期，对应拼车暂停推送，等待重新核对价格。'},
                              key='linuxdo:prices:expired:' + key, connection=db)


def finish(store, snapshot: dict, extraction: dict, config: dict, *, run_id: str = '') -> dict:
    evaluated = [trade.evaluate(offer, config, snapshot) for offer in extraction['offers']] if extraction['intent'] == 'offer' else []
    topic_id, now = snapshot['topic_id'], time.time()
    ids = []
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = db.execute('SELECT * FROM linuxdo_topics WHERE topic_id=?', (topic_id,)).fetchone()
        if current['version'] != snapshot['version']:
            db.execute("UPDATE linuxdo_topics SET status='pending',lease_until=0,next_attempt=0 WHERE topic_id=?", (topic_id,))
            return {'decision': 'hold', 'event_id': None, 'error': ''}
        legacy_notified = bool(db.execute('SELECT 1 FROM linuxdo_legacy_topics WHERE topic_id=?', (topic_id,)).fetchone()
            or db.execute('''SELECT 1 FROM linuxdo_notifications n JOIN events e ON e.id=n.event_id
                WHERE n.topic_id=? AND e.status<>'suppressed'
                AND NOT EXISTS(SELECT 1 FROM linuxdo_trade_notifications t WHERE t.event_id=n.event_id) LIMIT 1''',
                (topic_id,)).fetchone())
        for result in evaluated:
            offer = result['offer']
            key = identity(snapshot, result)
            if not key:
                if result['decision'] == 'push':
                    result.update(decision='hold', reason='交易作者身份尚不明确')
                key = digest([topic_id, result['spec'], offer['description']])
            if result['decision'] == 'push' and not current['historical']:
                comparable = result.get('unit_price') or result['monthly']
                prior = db.execute('''SELECT n.* FROM linuxdo_trade_notifications n JOIN events e ON e.id=n.event_id
                    WHERE n.offer_key=? AND e.status<>'suppressed' ORDER BY n.created_at DESC LIMIT 1''', (key,)).fetchone()
                event = offer['event']
                # A bump is not a new restock. The same evidenced change is stable across reposts.
                fresh_change = (offer['change'] != 'initial' and event['value']
                                and re.search(r'补货|恢复|重新开放|新增|增加|再次|更正|修正|纠正|restock|reopen|correct', event['quote'], re.I))
                if prior and fresh_change:
                    previous_change = db.execute('''SELECT result FROM linuxdo_trade_quotes WHERE offer_key=?
                        ORDER BY observed_at DESC LIMIT 1''', (key,)).fetchone()
                    if previous_change:
                        old_offer = json.loads(previous_change[0])['offer']
                        fresh_change = old_offer['event']['value'] != event['value']
                event_key = digest([key, comparable, result['total'], event['value'] if fresh_change else ''])
                duplicate = db.execute('''SELECT n.event_id,e.status FROM linuxdo_trade_notifications n
                    JOIN events e ON e.id=n.event_id WHERE n.offer_key=? AND n.event_key=?''', (key, event_key)).fetchone()
                if duplicate and duplicate['status'] == 'suppressed':
                    # An unsent quotation cancelled by an edit is not a delivered
                    # event. A corrected source may become eligible again once.
                    event_key = digest([event_key, snapshot['fingerprint']])
                    duplicate = db.execute('SELECT event_id FROM linuxdo_trade_notifications WHERE offer_key=? AND event_key=?',
                                           (key, event_key)).fetchone()
                legacy_duplicate = legacy_notified and not prior and not fresh_change
                if duplicate or legacy_duplicate or (prior and not fresh_change and Decimal(comparable) > Decimal(prior['comparable_price']) * Decimal('0.90')):
                    result.update(decision='skip', reason='沿用旧通知记录，未发现明确新权益' if legacy_duplicate else '同一卖家报价已通知，降价不足 10%')
                else:
                    message = trade.format_offer(snapshot, result)
                    payload = {'text': message, 'expires_at': min(
                        result['expires_at'] or float('inf'), current['updated_at'] + 7 * 86400)}
                    event_id = store.box.enqueue(profile='webops', job_id=JOB_ID, kind='text', payload=payload,
                                                 key='linuxdo:trade:' + event_key, run_id=run_id, connection=db)
                    db.execute('INSERT INTO linuxdo_trade_notifications VALUES(?,?,?,?,?,?)',
                               (key, event_key, event_id, result['total'], comparable, now))
                    facts = [{'kind': 'price', 'subject': offer['description']['value'], 'value': offer['price']['value'],
                              'category': 'B1' if offer['access'] == 'codex' else 'A2',
                              'evidence': [{'ref': offer['price']['ref'], 'quote': offer['price']['quote']}]}]
                    db.execute('INSERT OR IGNORE INTO linuxdo_notifications VALUES(?,?,?,?,?)',
                               (topic_id, event_key, json.dumps(facts, ensure_ascii=False), event_id, now))
                    ids.append(event_id)
            db.execute('INSERT OR REPLACE INTO linuxdo_trade_quotes VALUES(?,?,?,?,?)',
                       (topic_id, key, config['revision'], json.dumps(result, ensure_ascii=False), now))
        held = (extraction['intent'] == 'unknown' or (extraction['intent'] == 'offer' and not evaluated)
                or any(r['decision'] == 'hold' for r in evaluated))
        state = 'hold' if held else 'queued' if ids else 'skip'
        db.execute('''UPDATE linuxdo_topics SET status=?,lease_until=0,error='',next_attempt=?,
            attempts=CASE WHEN ?='hold' THEN attempts+1 ELSE 0 END WHERE topic_id=?''',
                   (state, now + min(21600, 300 * 2 ** min(current['attempts'], 6)), state, topic_id))
    return {'decision': 'push' if ids else state, 'event_id': ids[0] if ids else None, 'error': ''}
