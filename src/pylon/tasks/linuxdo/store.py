from __future__ import annotations

import json
import re
import time
from dataclasses import asdict

from pylon.events import text_key
from pylon.outbox import Outbox
from .common import JOB_ID, TOPIC_RE, clean, digest, timestamp
from .sources import Page


class Store:
    def __init__(self, box: Outbox | None = None):
        self.box = box or Outbox()
        with self.box.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS linuxdo_sources (
                    channel TEXT PRIMARY KEY, checkpoint TEXT NOT NULL,
                    last_success REAL NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', alert_open INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS linuxdo_observations (
                    channel TEXT NOT NULL, message_id INTEGER NOT NULL, topic_id TEXT NOT NULL,
                    payload TEXT NOT NULL, content_hash TEXT NOT NULL, observed_at REAL NOT NULL,
                    PRIMARY KEY(channel,message_id));
                CREATE TABLE IF NOT EXISTS linuxdo_topics (
                    topic_id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 0, first_seen REAL NOT NULL, updated_at REAL NOT NULL,
                    next_attempt REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    lease_until REAL NOT NULL DEFAULT 0, body_attempted_at REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', expires_at REAL, historical INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS linuxdo_ready ON linuxdo_topics(status,next_attempt,lease_until);
                CREATE TABLE IF NOT EXISTS linuxdo_evidence (
                    topic_id TEXT NOT NULL, ref TEXT NOT NULL, origin TEXT NOT NULL,
                    post_number INTEGER NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
                    body TEXT NOT NULL, links TEXT NOT NULL, complete INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, observed_at REAL NOT NULL,
                    PRIMARY KEY(topic_id,ref));
                CREATE TABLE IF NOT EXISTS linuxdo_decisions (
                    cache_key TEXT PRIMARY KEY, topic_id TEXT NOT NULL, result TEXT NOT NULL,
                    created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS linuxdo_notifications (
                    topic_id TEXT NOT NULL, fact_key TEXT NOT NULL, facts TEXT NOT NULL,
                    event_id TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(topic_id,fact_key));
                CREATE TABLE IF NOT EXISTS linuxdo_legacy_topics (
                    topic_id TEXT PRIMARY KEY, delivered_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS linuxdo_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
            ''')

    def checkpoint(self, channel: str) -> dict | None:
        with self.box.connect() as db:
            row = db.execute('SELECT checkpoint FROM linuxdo_sources WHERE channel=?', (channel,)).fetchone()
        return json.loads(row[0]) if row else None

    def import_legacy(self, state: dict) -> dict:
        """One transaction; old JSON remains available for explicit rollback export."""
        summary = {'channels': 0, 'delivered': 0, 'queued': 0}
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM linuxdo_meta WHERE key='legacy_import_v1'").fetchone():
                return summary
            for channel, value in state.get('channels', {}).items():
                if not isinstance(value, dict) or not isinstance(value.get('last_seen_id'), int):
                    continue
                checkpoint = {'cursor': value['last_seen_id'], 'baseline': True,
                              'backfill_before': None, 'backfill_until': None}
                db.execute('INSERT OR IGNORE INTO linuxdo_sources(channel,checkpoint) VALUES(?,?)',
                           (channel, json.dumps(checkpoint)))
                summary['channels'] += 1
            for topic_id, value in state.get('delivered_topics', {}).items():
                if not str(topic_id).isdigit():
                    continue
                stamp = value.get('delivered_at', '') if isinstance(value, dict) else ''
                db.execute('INSERT OR IGNORE INTO linuxdo_legacy_topics VALUES(?,?)', (str(topic_id), stamp))
                summary['delivered'] += 1
            for entry in state.get('delivery_queue', []):
                if not isinstance(entry, dict) or not isinstance(entry.get('message'), str):
                    continue
                message = entry['message'].strip()
                match = TOPIC_RE.search(message)
                if not message or not match:
                    continue
                # Use the legacy sender's exact key, including already persisted outbox rows.
                event_id = self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text',
                                            payload={'text': message}, key=text_key(message, JOB_ID), connection=db)
                db.execute('INSERT OR IGNORE INTO linuxdo_notifications VALUES(?,?,?,?,?)',
                           (match[1], 'legacy', '[]', event_id, time.time()))
                summary['queued'] += 1
            db.execute("INSERT INTO linuxdo_meta VALUES('legacy_import_v1',?)", (json.dumps(summary),))
        return summary

    @staticmethod
    def _evidence(db, topic_id: str, item: dict, now: float) -> bool:
        content_hash = digest({key: item[key] for key in ('post_number', 'title', 'body', 'links', 'complete')})
        old = db.execute('SELECT content_hash FROM linuxdo_evidence WHERE topic_id=? AND ref=?',
                         (topic_id, item['ref'])).fetchone()
        if old and old[0] == content_hash:
            return False
        db.execute('''INSERT INTO linuxdo_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(topic_id,ref) DO UPDATE SET origin=excluded.origin,post_number=excluded.post_number,
            url=excluded.url,title=excluded.title,body=excluded.body,links=excluded.links,
            complete=excluded.complete,content_hash=excluded.content_hash,observed_at=excluded.observed_at''',
                   (topic_id, item['ref'], item['origin'], item['post_number'], item['url'], item['title'],
                    item['body'], json.dumps(item['links']), int(item['complete']), content_hash, now))
        return True

    def save_page(self, channel: str, page: Page, checkpoint: dict, *, baseline: bool = False) -> int:
        now, changed = time.time(), 0
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for post in page.observations:
                if post.channel != channel:
                    raise ValueError('observation channel mismatch')
                payload = asdict(post)
                content_hash = digest(payload)
                old = db.execute('SELECT content_hash FROM linuxdo_observations WHERE channel=? AND message_id=?',
                                 (channel, post.message_id)).fetchone()
                if old and old[0] == content_hash:
                    continue
                db.execute('''INSERT INTO linuxdo_observations VALUES(?,?,?,?,?,?)
                    ON CONFLICT(channel,message_id) DO UPDATE SET payload=excluded.payload,
                    content_hash=excluded.content_hash,observed_at=excluded.observed_at''',
                           (channel, post.message_id, post.topic_id, json.dumps(payload, ensure_ascii=False), content_hash, now))
                db.execute('''INSERT OR IGNORE INTO linuxdo_topics
                    (topic_id,title,status,first_seen,updated_at,historical) VALUES(?,?,?,?,?,?)''',
                           (post.topic_id, post.title, 'historical' if baseline else 'pending', now, now, int(baseline)))
                # A later live reply revives a baseline topic, never the baseline observations alone.
                if not baseline:
                    db.execute('UPDATE linuxdo_topics SET historical=0 WHERE topic_id=?', (post.topic_id,))
                item = {'ref': f'tg:{channel}:{post.message_id}', 'origin': 'telegram',
                        'post_number': post.post_number, 'url': post.source_url,
                        'title': post.title, 'body': post.body, 'links': post.links, 'complete': False}
                if self._evidence(db, post.topic_id, item, now):
                    db.execute('''UPDATE linuxdo_topics SET version=version+1,updated_at=?,
                        title=CASE WHEN ?=1 THEN ? ELSE title END,
                        status=CASE WHEN historical=1 THEN 'historical' ELSE 'pending' END,
                        next_attempt=?,attempts=0,error='',expires_at=NULL WHERE topic_id=?''',
                               (now, post.post_number, post.title, now + 30, post.topic_id))
                    changed += 1
            db.execute('''INSERT INTO linuxdo_sources(channel,checkpoint,last_success) VALUES(?,?,?)
                ON CONFLICT(channel) DO UPDATE SET checkpoint=excluded.checkpoint,last_success=excluded.last_success''',
                       (channel, json.dumps(checkpoint), now))
        return changed

    def source_result(self, channel: str, error: str = '') -> None:
        now = time.time()
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM linuxdo_sources WHERE channel=?', (channel,)).fetchone()
            if not row:
                db.execute('INSERT INTO linuxdo_sources(channel,checkpoint) VALUES(?,?)', (channel, '{}'))
                row = db.execute('SELECT * FROM linuxdo_sources WHERE channel=?', (channel,)).fetchone()
            failures = row['failures'] + 1 if error else 0
            opened = bool(row['alert_open'])
            if error and failures >= 3 and not opened:
                self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text', connection=db,
                                 payload={'text': f'Linux.do 来源 {channel} 连续采集失败，其他来源继续运行。'},
                                 key=f'linuxdo:source:{channel}:failed:{int(now)}')
                opened = True
            elif not error and opened:
                self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text', connection=db,
                                 payload={'text': f'Linux.do 来源 {channel} 已恢复。'},
                                 key=f'linuxdo:source:{channel}:recovered:{int(now)}')
                opened = False
            db.execute('UPDATE linuxdo_sources SET failures=?,error=?,alert_open=? WHERE channel=?',
                       (failures, error[:100], int(opened), channel))

    def claim(self, *, limit: int = 4, now: float | None = None) -> list[str]:
        now = time.time() if now is None else now
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('''SELECT topic_id FROM linuxdo_topics WHERE historical=0
                AND status IN ('pending','hold') AND next_attempt<=? AND lease_until<=?
                ORDER BY first_seen,topic_id LIMIT ?''', (now, now, limit)).fetchall()
            for row in rows:
                db.execute('UPDATE linuxdo_topics SET lease_until=? WHERE topic_id=?', (now + 300, row[0]))
        return [row[0] for row in rows]

    def snapshot(self, topic_id: str) -> dict:
        with self.box.connect() as db:
            topic = db.execute('SELECT * FROM linuxdo_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if not topic:
                raise ValueError('unknown Linux.do topic')
            evidence = [dict(row) for row in db.execute('''SELECT * FROM linuxdo_evidence WHERE topic_id=?
                ORDER BY CASE WHEN post_number=1 THEN 0 ELSE 1 END,observed_at DESC LIMIT 40''', (topic_id,))]
            notifications = [dict(row) for row in db.execute('''SELECT n.*,e.status AS delivery_status
                FROM linuxdo_notifications n JOIN events e ON e.id=n.event_id WHERE n.topic_id=?
                ORDER BY n.created_at''', (topic_id,))]
            legacy = db.execute('SELECT 1 FROM linuxdo_legacy_topics WHERE topic_id=?', (topic_id,)).fetchone()
        for row in evidence:
            row['links'] = json.loads(row['links'])
        for row in notifications:
            row['facts'] = json.loads(row['facts'])
        result = dict(topic)
        result.update(evidence=evidence, notifications=notifications, previously_notified=bool(legacy or notifications))
        # Channel labels, observation times, counters and display wording are not evidence changes.
        facts = {(r['post_number'], re.sub(r'\s+', ' ', r['title']), re.sub(r'\s+', ' ', r['body'])) for r in evidence}
        reduced = [item for item in facts if not any(item != other and item[0] == other[0]
                   and item[1] == other[1] and item[2] and item[2] in other[2] for other in facts)]
        result['fingerprint'] = digest({'text': sorted(reduced), 'links': sorted({u for r in evidence for u in r['links']})})
        return result

    def add_evidence(self, topic_id: str, items: list[dict], *, attempted: bool = True) -> None:
        now = time.time()
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changes = sum(self._evidence(db, topic_id, item, now) for item in items)
            db.execute('''UPDATE linuxdo_topics SET version=version+?,updated_at=CASE WHEN ?>0 THEN ? ELSE updated_at END,
                body_attempted_at=CASE WHEN ? THEN ? ELSE body_attempted_at END WHERE topic_id=?''',
                       (changes, changes, now, attempted, now, topic_id))

    def cached(self, cache_key: str) -> dict | None:
        with self.box.connect() as db:
            row = db.execute('SELECT result FROM linuxdo_decisions WHERE cache_key=?', (cache_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_decision(self, cache_key: str, topic_id: str, decision: dict) -> None:
        with self.box.connect() as db:
            db.execute('INSERT OR REPLACE INTO linuxdo_decisions VALUES(?,?,?,?)',
                       (cache_key, topic_id, json.dumps(decision, ensure_ascii=False), time.time()))

    def defer(self, topic_id: str, reason: str) -> None:
        with self.box.connect() as db:
            row = db.execute('SELECT attempts FROM linuxdo_topics WHERE topic_id=?', (topic_id,)).fetchone()
            attempt = row[0] + 1
            delay = min(1800, 60 * 2 ** min(attempt - 1, 5))
            db.execute('''UPDATE linuxdo_topics SET status='hold',attempts=?,next_attempt=?,lease_until=0,error=?
                WHERE topic_id=?''', (attempt, time.time() + delay, reason[:100], topic_id))

    def finish(self, snapshot: dict, decision: dict, *, message: str = '', run_id: str = '') -> str | None:
        """Compare-and-swap the evidence version and enqueue within the same transaction."""
        topic_id, now = snapshot['topic_id'], time.time()
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM linuxdo_topics WHERE topic_id=?', (topic_id,)).fetchone()
            if row['version'] != snapshot['version']:
                db.execute("UPDATE linuxdo_topics SET status='pending',lease_until=0,next_attempt=? WHERE topic_id=?", (now, topic_id))
                return None
            status = decision['decision']
            event_id = None
            expiry = timestamp(decision.get('expires_at', ''))
            if expiry is not None and expiry <= now:
                status = 'skip'
            if status == 'push' and not row['historical'] and message:
                facts = decision['facts']
                fact_key = digest(sorted(tuple(re.sub(r'\s+', '', f[key]).casefold()
                                               for key in ('kind', 'subject', 'value')) for f in facts))
                existing = db.execute('SELECT event_id FROM linuxdo_notifications WHERE topic_id=? AND fact_key=?',
                                      (topic_id, fact_key)).fetchone()
                if existing:
                    event_id = existing[0]
                else:
                    event_id = self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text',
                                                payload={'text': message}, key=f'linuxdo:{topic_id}:{fact_key}',
                                                run_id=run_id, connection=db)
                    db.execute('INSERT INTO linuxdo_notifications VALUES(?,?,?,?,?)',
                               (topic_id, fact_key, json.dumps(facts, ensure_ascii=False), event_id, now))
                state = db.execute('SELECT status FROM events WHERE id=?', (event_id,)).fetchone()[0]
                status = 'delivered' if state == 'delivered' else 'queued'
            elif status == 'push':
                status = 'historical' if row['historical'] else 'hold'
            db.execute('''UPDATE linuxdo_topics SET status=?,lease_until=0,error='',expires_at=?,
                next_attempt=?,attempts=CASE WHEN ?='hold' THEN attempts+1 ELSE 0 END WHERE topic_id=?''',
                       (status, expiry, now + min(21600, 300 * 2 ** min(row['attempts'], 6)), status, topic_id))
        return event_id

    def sync_deliveries(self) -> None:
        with self.box.connect() as db:
            db.execute('''UPDATE linuxdo_topics SET status='delivered' WHERE status='queued'
                AND EXISTS(SELECT 1 FROM linuxdo_notifications n WHERE n.topic_id=linuxdo_topics.topic_id)
                AND NOT EXISTS(SELECT 1 FROM linuxdo_notifications n JOIN events e ON e.id=n.event_id
                    WHERE n.topic_id=linuxdo_topics.topic_id AND e.status<>'delivered')''')

    def health(self, component: str, failed: bool) -> None:
        with self.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            name = 'health:' + component
            row = db.execute('SELECT value FROM linuxdo_meta WHERE key=?', (name,)).fetchone()
            current = json.loads(row[0]) if row else {'failures': 0, 'open': False}
            current['failures'] = current['failures'] + 1 if failed else 0
            transition = 'failed' if failed and current['failures'] >= 3 and not current['open'] else 'recovered' if not failed and current['open'] else ''
            if transition:
                current['open'] = transition == 'failed'
                text = 'Linux.do 价值判断连续失败，候选已保留，等待恢复。' if failed else 'Linux.do 价值判断已恢复，继续处理保留的候选。'
                self.box.enqueue(profile='webops', job_id=JOB_ID, kind='text', payload={'text': text},
                                 key=f'linuxdo:health:{component}:{transition}:{time.time_ns()}', connection=db)
            db.execute('INSERT OR REPLACE INTO linuxdo_meta VALUES(?,?)', (name, json.dumps(current)))

    def export_legacy(self, original: dict) -> dict:
        result = dict(original)
        channels, delivered = dict(result.get('channels', {})), dict(result.get('delivered_topics', {}))
        with self.box.connect() as db:
            for row in db.execute('SELECT channel,checkpoint FROM linuxdo_sources'):
                cp = json.loads(row['checkpoint'])
                if cp.get('cursor') is not None:
                    channels[row['channel']] = {'last_seen_id': cp['cursor']}
            for row in db.execute('SELECT topic_id,created_at FROM linuxdo_notifications'):
                from datetime import datetime, timezone
                delivered[row['topic_id']] = {'delivered_at': datetime.fromtimestamp(row['created_at'], timezone.utc).isoformat(), 'source': 'v2-outbox'}
        result.update(channels=channels, delivered_topics=delivered, delivery_queue=[])
        return result

    def stats(self) -> dict:
        with self.box.connect() as db:
            return {'topics': dict(db.execute('SELECT status,count(*) FROM linuxdo_topics GROUP BY status')),
                    'observations': db.execute('SELECT count(*) FROM linuxdo_observations').fetchone()[0],
                    'sources': [dict(row) for row in db.execute('SELECT channel,last_success,failures,error FROM linuxdo_sources')],
                    'notifications': dict(db.execute('''SELECT e.status,count(*) FROM linuxdo_notifications n
                        JOIN events e ON e.id=n.event_id GROUP BY e.status'''))}
