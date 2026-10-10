from __future__ import annotations

import json
import os
import time

from .common import JOB_ID, digest, timestamp
from .helper import PARSER_VERSION, SOURCE, HelperNotice, format_personal, parse_message, post_fingerprint
from .sources import Page
from .store import Store


class HelperStore:
    def __init__(self, store: Store, *, followed: frozenset[str] | None = None):
        self.store, self.box = store, store.box
        self.followed = followed
        self.parser_version = PARSER_VERSION if followed is None else digest([PARSER_VERSION, sorted(followed)])

    def save_page(self, messages: list[dict], checkpoint: dict) -> dict:
        counts = {'collected': 0, 'board': 0, 'personal': 0, 'following': 0, 'unknown': 0, 'queued': 0}
        now = time.time()
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current = db.execute('SELECT checkpoint FROM linuxdo_sources WHERE channel=?', (SOURCE,)).fetchone()
            if current:
                previous = json.loads(current[0])
                if previous.get('identity') not in {None, checkpoint.get('identity')}:
                    raise ValueError('Helper source identity changed')
                if checkpoint['cursor'] < previous.get('cursor', 0):
                    raise ValueError('Helper checkpoint moved backwards')
            for raw in sorted(messages, key=lambda row: row['message_id']):
                if raw['message_id'] <= checkpoint['baseline_id']:
                    continue
                notice = parse_message(raw, followed=self.followed)
                old = db.execute('SELECT * FROM linuxdo_helper_messages WHERE message_id=?',
                                 (notice.message_id,)).fetchone()
                content_hash = digest([self.parser_version, raw, notice.payload()])
                if old and old['content_hash'] == content_hash:
                    continue
                route = notice.route
                # Once private, an edit can never turn the same message into model input.
                if old and old['route'] == 'personal':
                    route = 'personal'
                payload = notice.payload()
                if route == 'silent':
                    payload = {'message_id': notice.message_id, 'kind': notice.kind, 'route': route}
                else:
                    payload.update(_raw=raw, _parser=self.parser_version, _parser_revision=PARSER_VERSION)
                status = 'queued' if route == 'personal' else 'accepted' if route in {'board', 'following'} else route
                if old and old['route'] == 'personal':
                    status = old['status']
                changed_public = (old and old['route'] in {'board', 'following'}
                                  and json.loads(old['payload']).get('_raw') != raw)
                if changed_public:
                    ref = f'tg:{SOURCE}:{notice.message_id}'
                    _invalidate_market(db, old['topic_id'], ref, status='awaiting_helper' if route in {'board','following'} else 'skipped')
                    # A sold-out edit must invalidate a price decision and any
                    # unsent preview before asynchronous reclassification.
                    pending = db.execute('''SELECT n.facts,e.id FROM linuxdo_notifications n
                        JOIN events e ON e.id=n.event_id WHERE n.topic_id=?
                        AND e.status='pending' AND e.attempts=0''', (old['topic_id'],)).fetchall()
                    for item in pending:
                        if any(c.get('ref') == ref for fact in json.loads(item['facts']) for c in fact.get('evidence', [])):
                            db.execute("UPDATE events SET status='suppressed',error='source edited before delivery' WHERE id=?", (item['id'],))
                    for item in _intel_notifications(db, old['topic_id']):
                        if (item['status']=='pending' and item['attempts']==0 and
                            any(c.get('ref')==ref for fact in json.loads(item['facts']) for c in fact.get('evidence',[]))):
                            _suppress_intel(db,item['id'],'source edited before delivery')
                    db.execute('DELETE FROM linuxdo_observations WHERE channel=? AND message_id=?', (SOURCE, notice.message_id))
                    db.execute('DELETE FROM linuxdo_evidence WHERE topic_id=? AND ref=?', (old['topic_id'], ref))
                    db.execute("""UPDATE linuxdo_topics SET version=version+1,
                        status=CASE WHEN EXISTS(SELECT 1 FROM linuxdo_evidence e
                            WHERE e.topic_id=linuxdo_topics.topic_id) THEN 'pending' ELSE 'skip' END,
                        lease_until=0,next_attempt=0 WHERE topic_id=?""", (old['topic_id'],))
                db.execute('''INSERT INTO linuxdo_helper_messages VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(message_id) DO UPDATE SET kind=excluded.kind,route=excluded.route,
                    topic_id=excluded.topic_id,post_number=excluded.post_number,payload=excluded.payload,
                    content_hash=excluded.content_hash,status=excluded.status,observed_at=excluded.observed_at''',
                           (notice.message_id, notice.kind, route, notice.topic_id, notice.post_number,
                            json.dumps(payload, ensure_ascii=False), content_hash, status, now))
                counts['collected'] += 1
                if route in counts:
                    counts[route] += 1
                if old and not changed_public and old['route'] in {'board','following'} and (route not in {'board','following'} or old['topic_id'] != notice.topic_id):
                    _invalidate_market(db, old['topic_id'], f'tg:{SOURCE}:{notice.message_id}',status='skipped')
                    for item in _intel_notifications(db,old['topic_id']):
                        if item['status']=='pending' and item['attempts']==0:
                            _suppress_intel(db,item['id'],'source no longer public')
                    db.execute('DELETE FROM linuxdo_observations WHERE channel=? AND message_id=?', (SOURCE, notice.message_id))
                    db.execute('DELETE FROM linuxdo_evidence WHERE topic_id=? AND ref=?',
                               (old['topic_id'], f'tg:{SOURCE}:{notice.message_id}'))
                    db.execute("""UPDATE linuxdo_topics SET version=version+1,
                        status=CASE WHEN EXISTS(SELECT 1 FROM linuxdo_evidence e
                            WHERE e.topic_id=linuxdo_topics.topic_id) THEN 'pending' ELSE 'skip' END,
                        lease_until=0 WHERE topic_id=?""",
                               (old['topic_id'],))
                if route == 'board':
                    self.store.save_page(SOURCE, Page([notice.observation()], [notice.message_id]), checkpoint, connection=db)
                elif route == 'personal' and not (old and old['route'] == 'personal'):
                    counts['queued'] += self._personal(db, notice, now)
                elif route == 'following':
                    if old and old['route'] == 'board':
                        # A parser rollout must not replay previously evaluated board
                        # content as new followed-author notifications.
                        db.execute("UPDATE linuxdo_helper_messages SET status='historical' WHERE message_id=?", (notice.message_id,))
                        continue
                    db.execute('''INSERT INTO linuxdo_public_reviews(message_id,content_hash,status) VALUES(?,?,'pending')
                        ON CONFLICT(message_id) DO UPDATE SET content_hash=excluded.content_hash,
                        status=CASE WHEN linuxdo_public_reviews.status='direct' THEN 'direct' ELSE 'pending' END,
                        lease_until=0,next_attempt=0''', (notice.message_id, content_hash))
            self.store.save_page(SOURCE, Page(), checkpoint, connection=db)
            unresolved = db.execute("SELECT count(*) FROM linuxdo_helper_messages WHERE route='unknown'").fetchone()[0]
            key = 'helper:format_alert'
            # An unrecognized individual notice is diagnostic data, not a
            # stalled component. It must not generate an immediate alert.
            db.execute('INSERT OR REPLACE INTO linuxdo_meta VALUES(?,?)', (key, json.dumps(bool(unresolved))))
        return counts

    def reparse_pending(self, *, limit: int = 100) -> dict:
        with self.box.connect() as db:
            rows = db.execute('''SELECT payload FROM linuxdo_helper_messages WHERE
                (route='unknown' AND coalesce(json_extract(payload, '$._parser'), '')<>?)
                OR (route='board' AND coalesce(json_extract(payload, '$._parser_revision'), '')<>?)
                OR (route='following' AND coalesce(json_extract(payload, '$._parser'), '')<>?
                    AND message_id IN (SELECT message_id FROM linuxdo_public_reviews WHERE status IN ('pending','hold')))
                ORDER BY message_id LIMIT ?''',
                              (self.parser_version, PARSER_VERSION, self.parser_version, max(1, min(limit, 100)))).fetchall()
        messages = [payload['_raw'] for row in rows if '_raw' in (payload := json.loads(row[0]))]
        if not messages:
            return {'collected': 0, 'queued': 0}
        return self.save_page(messages, self.store.checkpoint(SOURCE))

    def _personal(self, db, notice: HelperNotice, now: float) -> int:
        key = notice.event_key()
        if db.execute('SELECT 1 FROM linuxdo_helper_events WHERE event_key=?', (key,)).fetchone():
            db.execute("UPDATE linuxdo_helper_messages SET status='duplicate' WHERE message_id=?", (notice.message_id,))
            return 0
        event_id = None
        # If a board notice for this exact post has not started sending, the personal
        # notification wins. A confirmed/uncertain send must never be duplicated.
        if notice.topic_id and notice.post_number:
            refs = {row['ref']: row['post_number'] for row in db.execute(
                'SELECT ref,post_number FROM linuxdo_evidence WHERE topic_id=?', (notice.topic_id,))}
            rows = db.execute('''SELECT n.facts,e.id,e.status,e.attempts FROM linuxdo_notifications n
                JOIN events e ON e.id=n.event_id WHERE n.topic_id=? ORDER BY n.created_at DESC''', (notice.topic_id,)).fetchall()
            rows=[*rows,*_intel_notifications(db,notice.topic_id),*_market_notifications(db,notice.topic_id)]
            for row in rows:
                facts = json.loads(row['facts'])
                positions = {c.get('post_number', refs.get(c['ref'], 0))
                             for fact in facts for c in fact.get('evidence', [])}
                if positions != {notice.post_number}:
                    continue
                if row['status'] == 'pending' and row['attempts'] == 0:
                    db.execute("UPDATE events SET status='suppressed',error='covered by personal notification' WHERE id=?",
                               (row['id'],))
                    _suppress_intel(db,row['id'],'covered by personal notification')
                    _suppress_market(db,row['id'],'covered by personal notification',status='skipped')
                elif row['status'] in {'delivered', 'sending', 'uncertain'}:
                    event_id = row['id']
                    break
        queued = event_id is None
        if queued:
            event_id = self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text',
                                        payload={'text': format_personal(notice)}, key='linuxdo:helper:' + key,
                                        run_id=os.environ.get('PYLON_RUN_ID', ''), connection=db)
        source_time = timestamp(notice.edited_at or notice.published_at) or now
        db.execute('''INSERT INTO linuxdo_helper_events
            (event_key,message_id,kind,topic_id,post_number,source_time,event_id,created_at,post_fingerprint)
            VALUES(?,?,?,?,?,?,?,?,?)''',
                   (key, notice.message_id, notice.kind, notice.topic_id, notice.post_number, source_time, event_id, now,
                    notice.post_fingerprint()))
        # Invalidate an in-flight board decision without copying personal contents.
        db.execute("UPDATE linuxdo_topics SET version=version+1,status='pending',next_attempt=?,lease_until=0 WHERE topic_id=?",
                   (now, notice.topic_id))
        return int(queued)

    def result(self, checkpoint: dict, *, error: str = '', retry_at: float = 0, authorization: bool = False) -> None:
        now = time.time()
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM linuxdo_sources WHERE channel=?', (SOURCE,)).fetchone()
            failures = (row['failures'] if row else 0) + 1 if error else 0
            opened = False
            checkpoint = dict(checkpoint, retry_at=retry_at)
            db.execute('''INSERT INTO linuxdo_sources(channel,checkpoint,last_success,failures,error,alert_open)
                VALUES(?,?,?,?,?,?) ON CONFLICT(channel) DO UPDATE SET checkpoint=excluded.checkpoint,
                last_success=CASE WHEN excluded.error='' THEN excluded.last_success ELSE linuxdo_sources.last_success END,
                failures=excluded.failures,error=excluded.error,alert_open=excluded.alert_open''',
                       (SOURCE, json.dumps(checkpoint), now if not error else 0, failures, error[:100], int(opened)))
        from pylon.tasks.intelligence.health import record
        record(self.box,'linuxdo:collection',backlog=int(bool(error)),progress=not error,error=error)


def _intel_notifications(db,topic_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='intel_items'").fetchone():return []
    rows=db.execute("""SELECT json_extract(i.result,'$.facts') facts,i.context,e.id,e.status,e.attempts
        FROM intel_items i JOIN events e ON e.id=i.event_id
        WHERE i.source='linuxdo' AND i.source_id=? AND json_type(i.result,'$.facts')='array'""",(topic_id,)).fetchall()
    notifications=[]
    for row in rows:
        context=json.loads(row['context'] or '{}')
        positions={r['ref']:r.get('post_number',0) for r in context.get('snapshot',{}).get('evidence',[])}
        facts=json.loads(row['facts'])
        for fact in facts:
            for citation in fact.get('evidence',[]):
                if citation['ref'] in positions:citation['post_number']=positions[citation['ref']]
        notifications.append({**dict(row),'facts':json.dumps(facts)})
    return notifications


def _market_notifications(db,topic_id):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='marketplace_posts'").fetchone():return []
    row=db.execute("""SELECT p.source_context,e.id,e.status,e.attempts
        FROM marketplace_posts p JOIN events e ON e.id=p.event_id
        WHERE p.source='linuxdo' AND p.source_id=?""",(topic_id,)).fetchone()
    if not row:return []
    context=json.loads(row['source_context'] or '{}')
    evidence=context.get('snapshot',{}).get('evidence',[])
    # An edit removes the live evidence before the new snapshot is captured.
    # Delivery identity must continue to use the original, persisted positions.
    return [{**dict(row),'facts':json.dumps([{'evidence':[
        {'ref':r['ref'],'post_number':r.get('post_number',0)} for r in evidence]}])}]


def _suppress_market(db,event_id,reason,*,status='awaiting_helper'):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='marketplace_posts'").fetchone():return
    row=db.execute("SELECT url FROM marketplace_posts WHERE source='linuxdo' AND event_id=?",(event_id,)).fetchone()
    if not row:return
    db.execute("UPDATE events SET status='suppressed',error=? WHERE id=? AND status='pending' AND attempts=0",(reason,event_id))
    if not db.execute("SELECT 1 FROM events WHERE id=? AND status='suppressed' AND attempts=0",(event_id,)).fetchone():return
    db.execute("DELETE FROM market_ai_receipts WHERE event_id=?",(event_id,))
    db.execute("UPDATE marketplace_posts SET event_id=NULL,status=? WHERE source='linuxdo' AND event_id=?",(status,event_id))
    db.execute("""UPDATE events SET status='suppressed',error=? WHERE profile='webops'
        AND job_id='marketplace-poll' AND kind='bark' AND status='pending' AND attempts=0
        AND json_extract(payload,'$.body.url')=?""",(reason,row['url']))


def _invalidate_market(db,topic_id,ref,*,status='awaiting_helper'):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='marketplace_posts'").fetchone():return
    row=db.execute("SELECT * FROM marketplace_posts WHERE source='linuxdo' AND source_id=? AND history=0",(topic_id,)).fetchone()
    if not row:return
    context=json.loads(row['source_context'] or '{}')
    evidence=context.get('snapshot',{}).get('evidence',[])
    if evidence and ref not in {r['ref'] for r in evidence}:return
    if row['event_id']:_suppress_market(db,row['event_id'],'source edited before delivery')
    # A result from an old in-flight analysis cannot make this row ready again.
    # Keep its evidence for inspection until the new Helper revision is judged.
    context['helper_stale']=True
    db.execute("UPDATE marketplace_posts SET status=?,source_context=?,updated_at=? WHERE source='linuxdo' AND source_id=?",(status,json.dumps(context,ensure_ascii=False),time.time(),topic_id))
    db.execute("UPDATE market_reviews SET status='resolved',updated_at=? WHERE source='linuxdo' AND source_id=? AND status='pending'",(time.time(),topic_id))


def _suppress_intel(db,event_id,reason):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='intel_items'").fetchone():return
    db.execute("UPDATE events SET status='suppressed',error=? WHERE id=? AND status='pending' AND attempts=0",(reason,event_id))
    db.execute("DELETE FROM intel_receipts WHERE event_id=? AND EXISTS(SELECT 1 FROM events WHERE id=? AND status='suppressed' AND attempts=0)",(event_id,event_id))
    db.execute("""UPDATE intel_items SET event_id=NULL,status='pending',lease_until=0,next_attempt=0
        WHERE event_id=? AND EXISTS(SELECT 1 FROM events WHERE id=? AND status='suppressed' AND attempts=0)""",(event_id,event_id))


def covered_by_personal(snapshot: dict, facts: list[dict] | None = None) -> bool:
    personal = snapshot.get('personal_posts', [])
    if not personal:
        return False
    rows = snapshot['evidence']
    if facts is not None:
        cited = {c['ref'] for fact in facts for c in fact.get('evidence', [])}
        rows = [row for row in rows if row['ref'] in cited]
    rows = [row for row in rows if row['origin'] != 'official' and row['post_number'] > 0]
    if not rows:
        return False
    for position in {row['post_number'] for row in rows}:
        notices = [item for item in personal if item['post_number'] == position]
        if not notices:
            return False
        # A newer preview of this post may carry a new opportunity. Unchanged
        # previews remain duplicates even when the subscription arrives later.
        previews = [row for row in snapshot['evidence'] if row['origin'] == 'telegram'
                    and row['post_number'] == position]
        for row in previews:
            published = max(timestamp(row.get('source_edited_at', '')) or 0,
                            timestamp(row.get('source_published_at', '')) or 0)
            fingerprint = post_fingerprint(row['title'], row['body'])
            if not any(published <= item['source_time'] or
                       (fingerprint and fingerprint == item.get('post_fingerprint')) for item in notices):
                return False
    return True
