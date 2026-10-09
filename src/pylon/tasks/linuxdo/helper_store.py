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
        counts = {'collected': 0, 'board': 0, 'personal': 0, 'unknown': 0, 'queued': 0}
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
                status = 'queued' if route == 'personal' else 'accepted' if route == 'board' else route
                if old and old['route'] == 'personal':
                    status = old['status']
                db.execute('''INSERT INTO linuxdo_helper_messages VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(message_id) DO UPDATE SET kind=excluded.kind,route=excluded.route,
                    topic_id=excluded.topic_id,post_number=excluded.post_number,payload=excluded.payload,
                    content_hash=excluded.content_hash,status=excluded.status,observed_at=excluded.observed_at''',
                           (notice.message_id, notice.kind, route, notice.topic_id, notice.post_number,
                            json.dumps(payload, ensure_ascii=False), content_hash, status, now))
                counts['collected'] += 1
                if route in counts:
                    counts[route] += 1
                if old and old['route'] == 'board' and (route != 'board' or old['topic_id'] != notice.topic_id):
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
            self.store.save_page(SOURCE, Page(), checkpoint, connection=db)
            unresolved = db.execute("SELECT count(*) FROM linuxdo_helper_messages WHERE route='unknown'").fetchone()[0]
            key = 'helper:format_alert'
            row = db.execute('SELECT value FROM linuxdo_meta WHERE key=?', (key,)).fetchone()
            opened = bool(json.loads(row[0])) if row else False
            if unresolved and not opened:
                self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text', connection=db,
                                 payload={'text': 'Linux.do Helper 出现未识别通知，已保留等待解析，请在 Helper 原对话查看。'},
                                 key=f'linuxdo:helper:format:{time.time_ns()}')
            db.execute('INSERT OR REPLACE INTO linuxdo_meta VALUES(?,?)', (key, json.dumps(bool(unresolved))))
        return counts

    def reparse_pending(self, *, limit: int = 100) -> dict:
        with self.box.connect() as db:
            rows = db.execute('''SELECT payload FROM linuxdo_helper_messages WHERE
                (route='unknown' AND coalesce(json_extract(payload, '$._parser'), '')<>?)
                OR (route='board' AND coalesce(json_extract(payload, '$._parser_revision'), '')<>?)
                ORDER BY message_id LIMIT ?''',
                              (self.parser_version, PARSER_VERSION, max(1, min(limit, 100)))).fetchall()
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
            for row in rows:
                facts = json.loads(row['facts'])
                positions = {refs.get(c['ref'], 0) for fact in facts for c in fact.get('evidence', [])}
                if positions != {notice.post_number}:
                    continue
                if row['status'] == 'pending' and row['attempts'] == 0:
                    db.execute("UPDATE events SET status='suppressed',error='covered by personal notification' WHERE id=?",
                               (row['id'],))
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
            opened = bool(row['alert_open']) if row else False
            if error and (authorization or failures >= 3) and not opened:
                text = ('Linux.do Helper 登录已失效，请通过终端重新授权。采集起点已保留。' if authorization else
                        'Linux.do Helper 连续采集失败，消息起点已保留，等待恢复。')
                self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text', payload={'text': text},
                                 key=f'linuxdo:helper:failed:{time.time_ns()}', connection=db)
                opened = True
            elif not error and opened:
                self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text',
                                 payload={'text': 'Linux.do Helper 采集已恢复，继续补采未处理消息。'},
                                 key=f'linuxdo:helper:recovered:{time.time_ns()}', connection=db)
                opened = False
            checkpoint = dict(checkpoint, retry_at=retry_at)
            db.execute('''INSERT INTO linuxdo_sources(channel,checkpoint,last_success,failures,error,alert_open)
                VALUES(?,?,?,?,?,?) ON CONFLICT(channel) DO UPDATE SET checkpoint=excluded.checkpoint,
                last_success=CASE WHEN excluded.error='' THEN excluded.last_success ELSE linuxdo_sources.last_success END,
                failures=excluded.failures,error=excluded.error,alert_open=excluded.alert_open''',
                       (SOURCE, json.dumps(checkpoint), now if not error else 0, failures, error[:100], int(opened)))


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
