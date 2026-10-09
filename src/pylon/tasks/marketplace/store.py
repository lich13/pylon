"""Durable ingestion, analysis checkpoints and atomic notification decisions."""
from __future__ import annotations

import hashlib
import json
import time
import re
from pylon.outbox import Outbox
from .classifier import category_for, classify_listing


class Store:
    def __init__(self, box: Outbox | None = None):
        self.box = box or Outbox()
        with self.box.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS marketplace_posts (
                    source TEXT NOT NULL, source_id TEXT NOT NULL, url TEXT NOT NULL,
                    title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', body_complete INTEGER NOT NULL,
                    posted_at TEXT NOT NULL DEFAULT '', first_seen REAL NOT NULL, updated_at REAL NOT NULL,
                    items TEXT NOT NULL, status TEXT NOT NULL, history INTEGER NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', policy_version INTEGER, event_id TEXT,
                    PRIMARY KEY(source,source_id));
                CREATE INDEX IF NOT EXISTS marketplace_pending ON marketplace_posts(status,next_attempt);
                CREATE TABLE IF NOT EXISTS marketplace_checkpoints (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS marketplace_analyses (
                    fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, started_at REAL,
                    updated_at REAL NOT NULL, model TEXT NOT NULL, request_id TEXT,
                    result TEXT, error TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS marketplace_analysis_pending
                    ON marketplace_analyses(status,next_attempt);
            ''')
            db.execute('BEGIN IMMEDIATE')
            if 'wanted_policy_version' not in {row[1] for row in db.execute('PRAGMA table_info(marketplace_posts)')}:
                db.execute('ALTER TABLE marketplace_posts ADD COLUMN wanted_policy_version INTEGER')

    def checkpoint(self, name: str, value=None):
        with self.box.connect() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO marketplace_checkpoints VALUES(?,?,?)',
                           (name, json.dumps(value, ensure_ascii=False), time.time()))
                return value
            row = db.execute('SELECT value FROM marketplace_checkpoints WHERE name=?', (name,)).fetchone()
            return json.loads(row[0]) if row else None

    def ingest(self, source: str, posts: list[dict], *, history: bool = False) -> int:
        if source not in {'v2ex', 'chiphell', 'nga'}:
            raise ValueError('unknown marketplace source')
        count = 0
        with self.box.connect() as db:
            for post in posts:
                source_id = str(post.get('id') or post.get('message_id') or '')
                url = str(post.get('url') or post.get('v2ex_url') or '')
                title = str(post.get('title') or '').strip()
                if not source_id or not title or not url.startswith('https://'):
                    continue
                if source=='v2ex':
                    match = re.search(r'/t/(\d+)',url)
                    if match:
                        source_id = match[1]
                        url = 'https://www.v2ex.com/t/'+source_id
                if not re.fullmatch(r'\d{1,18}',source_id):
                    continue
                if source=='nga':
                    url = 'https://bbs.nga.cn/read.php?tid='+source_id
                elif source=='chiphell':
                    url = f'https://www.chiphell.com/thread-{source_id}-1-1.html'
                body = str(post.get('body_full') or post.get('body_excerpt') or '')
                complete = bool(post.get('body_complete')) and bool(body)
                items = classify_listing(title, body, source, complete)
                stamp = time.time()
                status = 'historical' if history else 'ready' if complete else 'pending_body'
                cursor = db.execute('''INSERT OR IGNORE INTO marketplace_posts
                    (source,source_id,url,title,body,body_complete,posted_at,first_seen,updated_at,items,status,history)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (source,source_id,url,title,body,int(complete),str(post.get('posted_at') or ''),stamp,stamp,
                     json.dumps(items, ensure_ascii=False),status,int(history)))
                count += cursor.rowcount
                # Never replace an enqueued message or promote historical data to live work.
                if not cursor.rowcount and complete:
                    db.execute('''UPDATE marketplace_posts SET title=?,body=?,body_complete=1,items=?,updated_at=?,
                        posted_at=CASE WHEN ?<>'' THEN ? ELSE posted_at END,
                        status=CASE WHEN history=1 THEN 'historical' ELSE 'ready' END,error=''
                        WHERE source=? AND source_id=? AND status IN ('pending_body','historical','unavailable')''',
                        (title,body,json.dumps(items,ensure_ascii=False),stamp,str(post.get('posted_at') or ''),
                         str(post.get('posted_at') or ''),source,source_id))
                elif not cursor.rowcount and post.get('posted_at'):
                    db.execute("UPDATE marketplace_posts SET posted_at=? WHERE source=? AND source_id=? AND posted_at=''",
                               (str(post['posted_at']),source,source_id))
        return count

    def pending(self, *, history: bool = False, limit: int = 20, source: str | None = None) -> list[dict]:
        if source is not None and source not in {'v2ex','chiphell','nga'}:
            raise ValueError('unknown marketplace source')
        with self.box.connect() as db:
            rows = db.execute('''WITH ranked AS (
                SELECT *,ROW_NUMBER() OVER (PARTITION BY source ORDER BY first_seen,source_id) AS source_rank
                FROM marketplace_posts WHERE history=? AND body_complete=0 AND next_attempt<=?
                AND status IN ('pending_body','historical') AND (? IS NULL OR source=?))
                SELECT * FROM ranked ORDER BY source_rank,first_seen,source LIMIT ?''',
                (int(history),time.time(),source,source,limit)).fetchall()
            return [{k:r[k] for k in r.keys() if k!='source_rank'} for r in rows]

    def body_queue(self, source: str, *, history: bool = True) -> dict:
        with self.box.connect() as db:
            row = db.execute('''SELECT COUNT(*), MIN(next_attempt) FROM marketplace_posts
                WHERE source=? AND history=? AND body_complete=0
                AND status IN ('pending_body','historical')''',(source,int(history))).fetchone()
        return {'pending':row[0],'next_attempt':row[1] or 0}

    def body_error(self, post: dict, reason: str, *, permanent: bool = False):
        with self.box.connect() as db:
            db.execute('''UPDATE marketplace_posts SET attempts=attempts+1,error=?,next_attempt=?,
                status=CASE WHEN ? THEN CASE WHEN history=1 THEN 'unavailable' ELSE 'ready' END ELSE status END WHERE source=? AND source_id=?''',
                (reason[:120],time.time()+min(21600,60*2**min(post['attempts'],8)),permanent,post['source'],post['source_id']))

    def stats(self, *, fresh: bool = False) -> dict:
        from .taxonomy import CATEGORIES, GROUPS, canonical, group_for, hierarchy
        from .wanted import descriptor
        categories = {key: {'label':label,'posts':{},'items':{},'statuses':{},'examples':[],
                            'offer_posts':{},'offer_items':{},'incomplete_posts':{}}
                      for key,label in CATEGORIES.items()}
        coverage = {}
        groups={key:{'label':label,'posts':{},'items':{},'complete_posts':{},'unknown':0} for key,label in GROUPS.items()}
        wanted_catalog={}
        wanted_unresolved={}
        with self.box.connect() as db:
            db.execute('BEGIN')
            for row in db.execute('SELECT * FROM marketplace_posts'):
                source = row['source']
                cov = coverage.setdefault(source,dict(posts=0,complete=0,missing=0,unavailable=0,unknown=0,
                    unknown_posts=0,mixed_posts=0,available_items=0,gap_reasons={},first='',last=''))
                cov['posts'] += 1
                cov['complete'] += int(row['body_complete'])
                cov['missing'] += int(not row['body_complete'])
                cov['unavailable'] += int(row['status']=='unavailable')
                if row['error']:
                    cov['gap_reasons'][row['error']] = cov['gap_reasons'].get(row['error'],0)+1
                stamp = row['posted_at']
                if stamp:
                    cov['first'] = min(cov['first'] or stamp,stamp)
                    cov['last'] = max(cov['last'],stamp)
                seen = set()
                seen_sales = set()
                seen_offers = set()
                items = (classify_listing(row['title'],row['body'],source,bool(row['body_complete']))
                         if fresh else json.loads(row['items']))
                group_seen=set()
                wanted_seen=set()
                cov['unknown_posts'] += int(any(item['category']=='unknown' for item in items))
                cov['mixed_posts'] += int(len({item['category'] for item in items if item['category']!='unknown'})>1)
                for item in items:
                    key = canonical(item['category'])
                    if item['status']=='buy' and item['attributes'].get('intent')=='wanted':
                        identity=descriptor(item['text'],key)
                        if identity:
                            identity_key=json.dumps({k:v for k,v in identity.items() if k!='model'},sort_keys=True,ensure_ascii=False)
                            request=wanted_catalog.setdefault(identity_key,{**identity,'posts':{},'items':{},'complete_posts':{}})
                            request['items'][source]=request['items'].get(source,0)+1
                            if identity_key not in wanted_seen:
                                request['posts'][source]=request['posts'].get(source,0)+1
                                if row['body_complete']:
                                    request['complete_posts'][source]=request['complete_posts'].get(source,0)+1
                                wanted_seen.add(identity_key)
                        else:
                            unresolved=wanted_unresolved.setdefault(key,{})
                            unresolved[source]=unresolved.get(source,0)+1
                    if key not in categories:
                        cov['unknown'] += 1
                        continue
                    cat = categories[key]
                    cat['statuses'][item['status']] = cat['statuses'].get(item['status'],0)+1
                    cat['items'][source] = cat['items'].get(source,0)+1
                    if key not in seen:
                        cat['posts'][source] = cat['posts'].get(source,0)+1
                        if not row['body_complete']:
                            incomplete = cat['incomplete_posts']
                            incomplete[source] = incomplete.get(source,0)+1
                        seen.add(key)
                    # Historical supply includes sold listings, but a wanted
                    # post or ambiguous mention is not an offer to sell.
                    if item['status'] in {'available','sold'}:
                        major=group_for(key)
                        group=groups[major]
                        group['items'][source]=group['items'].get(source,0)+1
                        if major not in group_seen:
                            group['posts'][source]=group['posts'].get(source,0)+1
                            if row['body_complete']:
                                group['complete_posts'][source]=group['complete_posts'].get(source,0)+1
                            group_seen.add(major)
                        offers = cat['offer_items']
                        offers[source] = offers.get(source,0)+1
                        if key not in seen_offers:
                            offers = cat['offer_posts']
                            offers[source] = offers.get(source,0)+1
                            seen_offers.add(key)
                    if item['status']=='available':
                        cov['available_items'] += 1
                        sale_posts = cat.setdefault('sale_posts',{})
                        sale_items = cat.setdefault('sale_items',{})
                        sale_items[source] = sale_items.get(source,0)+1
                        if key not in seen_sales:
                            sale_posts[source] = sale_posts.get(source,0)+1
                            seen_sales.add(key)
                    # An offer's own model is a useful example. A computer's
                    # GPU or a case's compatible phone is not the sold subject.
                    if item['status'] not in {'available','sold'}:
                        continue
                    example = next((model for model in item['attributes'].get('models',[])
                                    if canonical(category_for(model)[0]) == key),item['evidence'])
                    if example and example not in cat['examples'] and len(cat['examples']) < 4:
                        cat['examples'].append(example)
            checkpoints = {r[0]:json.loads(r[1]) for r in db.execute('SELECT name,value FROM marketplace_checkpoints')}
            pending={r[0]:r[1] for r in db.execute("SELECT source,COUNT(*) FROM marketplace_posts WHERE history=1 AND body_complete=0 AND status IN ('historical','pending_body') GROUP BY source")}
        return {'coverage':coverage,'categories':categories,'groups':groups,'hierarchy':hierarchy(),
                'pending':pending,'checkpoints':checkpoints,'generated_at':time.time(),
                'wanted_catalog':list(wanted_catalog.values()),'wanted_unresolved':wanted_unresolved}

    def reclassify_history(self) -> int:
        count = 0
        with self.box.connect() as db:
            for row in db.execute('SELECT source,source_id,title,body,body_complete FROM marketplace_posts WHERE history=1').fetchall():
                items = classify_listing(row['title'],row['body'],row['source'],bool(row['body_complete']))
                db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=? AND history=1',
                           (json.dumps(items,ensure_ascii=False),row['source'],row['source_id']))
                count += 1
        return count


def key(source: str, source_id: str) -> str:
    return 'marketplace:'+hashlib.sha256(f'{source}:{source_id}'.encode()).hexdigest()
