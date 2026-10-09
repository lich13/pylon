"""Review public followed-author posts without exposing personal notifications."""
from __future__ import annotations

import json
import time
from dataclasses import replace

from . import trade
from .common import clean, digest, timestamp
from .helper import HelperNotice, SOURCE, FOLLOWING
from .helper_store import HelperStore
from .sources import Page


def snapshot_for(notice: HelperNotice, version: str) -> dict:
    ref = f'tg:{SOURCE}:{notice.message_id}'
    evidence = {'ref': ref, 'origin': 'telegram', 'post_number': notice.post_number,
                'url': notice.url, 'title': clean(notice.title, 300), 'body': clean(notice.text),
                'links': notice.links, 'complete': False, 'observed_at': 0,
                'source_published_at': notice.published_at, 'source_edited_at': notice.edited_at}
    return {'topic_id': notice.topic_id, 'title': notice.title, 'evidence': [evidence],
            'fingerprint': digest([version, evidence]), 'actors': {ref: digest(notice.actor.casefold())}}


def review(store, *, limit: int = 2, request=trade.request, config: dict, model: str,
           focus: dict | None = None) -> dict:
    now = time.time()
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = [dict(row) for row in db.execute('''SELECT r.*,m.payload,m.route,m.kind FROM linuxdo_public_reviews r
            JOIN linuxdo_helper_messages m USING(message_id)
            WHERE r.status IN ('pending','hold') AND r.next_attempt<=? AND r.lease_until<=?
                AND m.route='following' ORDER BY r.message_id LIMIT ?''', (now, now, limit))]
        for row in rows:
            db.execute('UPDATE linuxdo_public_reviews SET lease_until=? WHERE message_id=?', (now + 300, row['message_id']))
    counts = {'processed': len(rows), 'errors': 0, 'queued': 0}
    for row in rows:
        try:
            if row['kind'] not in FOLLOWING:
                raise trade.TradeError('personal notification cannot enter public review')
            data = json.loads(row['payload'])
            notice = HelperNotice(**{k: v for k, v in data.items() if not k.startswith('_')})
            published = timestamp(notice.published_at)
            if published is not None and published < now - 7 * 86400:
                with store.box.connect() as db:
                    db.execute("UPDATE linuxdo_public_reviews SET status='skip',lease_until=0 WHERE message_id=? AND content_hash=?",
                               (notice.message_id, row['content_hash']))
                continue
            snapshot = snapshot_for(notice, row['content_hash'])
            key = trade.cache_key(snapshot, config, model, focus=focus)
            extracted = store.cached(key)
            if extracted is None:
                extracted = trade.validate(request(snapshot), snapshot)
                store.save_decision(key, notice.topic_id, extracted)
            else:
                extracted = trade.validate(extracted, snapshot)
            with store.box.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT content_hash,route FROM linuxdo_helper_messages WHERE message_id=?', (notice.message_id,)).fetchone()
                if current['content_hash'] != row['content_hash'] or current['route'] != 'following':
                    continue
                state = 'hold'
                if extracted['intent'] == 'non_trade':
                    counts['queued'] += HelperStore(store)._personal(db, notice, time.time())
                    state = 'direct'
                    db.execute("UPDATE linuxdo_helper_messages SET status='queued' WHERE message_id=?", (notice.message_id,))
                elif extracted['intent'] == 'wanted':
                    from pylon.tasks.marketplace.pipeline import enabled
                    if enabled():
                        observation=replace(notice,route='board').observation()
                        store.save_page(SOURCE,Page([observation],[notice.message_id]),store_checkpoint(db),connection=db)
                        state='trade'
                    else:
                        state='skip'
                        db.execute("UPDATE linuxdo_helper_messages SET status='skip' WHERE message_id=?",(notice.message_id,))
                elif extracted['intent'] in {'offer', 'cloud_offer'}:
                    # Only now may the public preview join topic evidence.
                    observation = replace(notice, route='board').observation()
                    store.save_page(SOURCE, Page([observation], [notice.message_id]), store_checkpoint(db), connection=db)
                    state = 'trade'
                db.execute('''UPDATE linuxdo_public_reviews SET status=?,lease_until=0,error='',next_attempt=?,
                    attempts=attempts+1 WHERE message_id=?''',
                           (state, time.time() + min(1800, 60 * 2 ** min(row['attempts'], 5)), notice.message_id))
            if state == 'trade':
                # Reuse extraction only when the aggregate contains exactly the
                # public preview just reviewed. New evidence gets a new request.
                merged = store.snapshot(notice.topic_id)
                if (len(merged['evidence']) == 1 and merged['evidence'][0]['ref'] == snapshot['evidence'][0]['ref']
                        and merged['evidence'][0]['body'] == snapshot['evidence'][0]['body']):
                    store.save_decision(trade.cache_key(merged, config, model, focus=focus), notice.topic_id,
                                        trade.validate(extracted, merged))
        except Exception as exc:
            counts['errors'] += 1
            with store.box.connect() as db:
                db.execute('''UPDATE linuxdo_public_reviews SET status='hold',lease_until=0,attempts=attempts+1,
                    next_attempt=?,error=? WHERE message_id=? AND content_hash=?''',
                           (time.time() + min(1800, 60 * 2 ** min(row['attempts'], 5)), type(exc).__name__, row['message_id'], row['content_hash']))
    return counts


def store_checkpoint(db) -> dict:
    row = db.execute('SELECT checkpoint FROM linuxdo_sources WHERE channel=?', (SOURCE,)).fetchone()
    return json.loads(row[0]) if row else {}
