from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Asia/Shanghai')
START = '2026-10-10'
SOURCES = {'linuxdo': 'Linux.do', 'v2ex': 'V2EX', 'chiphell': 'CHH', 'nga': 'NGA', 'xianbao': '线报'}
STATUSES = {'delivered': '已推送', 'queued': '待投递', 'excluded': '已过滤', 'needs_info': '待核对',
            'failed': '处理失败', 'duplicate': '重复', 'pending': '处理中', 'unknown': '原判断缺失'}
SCHEMA = '''
CREATE TABLE IF NOT EXISTS review_entries (
 id TEXT PRIMARY KEY, first_seen REAL, day TEXT NOT NULL, date_quality TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL, published_at TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending', categories TEXT NOT NULL DEFAULT '[]',
 priority INTEGER NOT NULL DEFAULT 0, priority_reasons TEXT NOT NULL DEFAULT '[]', updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS review_day ON review_entries(day,priority DESC,id);
CREATE TABLE IF NOT EXISTS review_origins (
 source TEXT NOT NULL, source_id TEXT NOT NULL, entry_id TEXT NOT NULL,
 first_seen REAL, date_quality TEXT NOT NULL, observations INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(source,source_id));
CREATE INDEX IF NOT EXISTS review_origin_entry ON review_origins(entry_id);
CREATE TABLE IF NOT EXISTS review_snapshots (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL, source TEXT NOT NULL, source_id TEXT NOT NULL,
 lane TEXT NOT NULL, fingerprint TEXT NOT NULL, content_hash TEXT NOT NULL, captured_at REAL NOT NULL,
 data TEXT NOT NULL, UNIQUE(source,source_id,lane,fingerprint));
CREATE INDEX IF NOT EXISTS review_snapshot_entry ON review_snapshots(entry_id,id);
CREATE INDEX IF NOT EXISTS review_content_hash ON review_snapshots(content_hash);
CREATE TABLE IF NOT EXISTS review_decisions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT NOT NULL, snapshot_id INTEGER NOT NULL,
 lane TEXT NOT NULL, status TEXT NOT NULL, rule_version TEXT NOT NULL, data TEXT NOT NULL,
 event_id TEXT, decided_at REAL NOT NULL, restored INTEGER NOT NULL DEFAULT 0, fingerprint TEXT NOT NULL UNIQUE);
CREATE INDEX IF NOT EXISTS review_decision_entry ON review_decisions(entry_id,id);
CREATE TABLE IF NOT EXISTS review_feedback (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, entry_id TEXT NOT NULL, item_key TEXT NOT NULL,
 snapshot_id INTEGER NOT NULL, decision_ids TEXT NOT NULL, value TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1, batch_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS review_draft_target ON review_feedback(owner,entry_id,item_key) WHERE batch_id IS NULL;
CREATE TABLE IF NOT EXISTS review_batches (
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
 result TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS review_digests (day TEXT PRIMARY KEY,event_id TEXT,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_resolutions (
 feedback_id TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}',updated_at REAL NOT NULL);
'''


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def initialize(db):
    for sql in SCHEMA.split(';'):
        if sql.strip(): db.execute(sql)


def stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool): return float(value) if value > 0 else None
    if not value: return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=ZONE)).timestamp()
    except (ValueError, TypeError, OverflowError): return None


def day_of(value):
    value = stamp(value)
    try:return datetime.fromtimestamp(value, ZONE).date().isoformat() if value else 'unknown'
    except (ValueError, OverflowError, OSError):return 'unknown'


def capture(db, source, source_id, *, lane, title, body, url, published_at='', first_seen=None,
            date_quality='native', evidence_scope='excerpt', fingerprint=None, metadata=None, restored=False):
    """Persist before filtering. No network, model call or outbox side effect."""
    if source not in SOURCES or lane not in {'intel', 'market'}: raise ValueError('invalid review origin')
    initialize(db)
    source_id = str(source_id)
    observed = stamp(first_seen) if first_seen is not None else (None if restored else time.time())
    day = day_of(observed)
    if day != 'unknown' and day < START: return None
    data = dict(title=str(title), body=str(body), url=str(url), published_at=str(published_at or ''),
                evidence_scope=evidence_scope, metadata=metadata or {})
    fp = fingerprint or digest(data)
    # Exact long content can be a repost; short/generic titles are never merged.
    plain = re.sub(r'\s+', ' ', str(title) + '\n' + str(body)).strip()
    content_hash = digest(plain) if len(plain) >= 80 else digest([source, source_id])
    origin = db.execute('SELECT * FROM review_origins WHERE source=? AND source_id=?', (source, source_id)).fetchone()
    if origin:
        entry_id = origin['entry_id']
        if observed and (origin['first_seen'] is None or observed < origin['first_seen']):
            db.execute('UPDATE review_origins SET first_seen=?,date_quality=? WHERE source=? AND source_id=?',
                       (observed, date_quality, source, source_id))
    else:
        duplicate = db.execute('SELECT entry_id FROM review_snapshots WHERE content_hash=? LIMIT 1', (content_hash,)).fetchone()
        entry_id = duplicate[0] if duplicate else digest([source, source_id])[:24]
        db.execute('INSERT INTO review_origins(source,source_id,entry_id,first_seen,date_quality) VALUES(?,?,?,?,?)',
                   (source, source_id, entry_id, observed, date_quality if observed else 'unknown'))
    now = time.time()
    db.execute('''INSERT OR IGNORE INTO review_entries(id,first_seen,day,date_quality,title,body,url,published_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?)''', (entry_id, observed, day, date_quality if observed else 'unknown', title, body, url, published_at or '', now))
    origin_date = db.execute('SELECT first_seen,date_quality FROM review_origins WHERE entry_id=? AND first_seen IS NOT NULL ORDER BY first_seen LIMIT 1', (entry_id,)).fetchone()
    earliest = origin_date['first_seen'] if origin_date else None
    db.execute('UPDATE review_entries SET first_seen=?,day=?,date_quality=? WHERE id=?',
               (earliest, day_of(earliest), origin_date['date_quality'] if origin_date else 'unknown', entry_id))
    cur = db.execute('''INSERT OR IGNORE INTO review_snapshots(entry_id,source,source_id,lane,fingerprint,content_hash,captured_at,data)
        VALUES(?,?,?,?,?,?,?,?)''', (entry_id, source, source_id, lane, fp, content_hash, now, dumps(data)))
    if cur.rowcount:
        db.execute('UPDATE review_entries SET title=?,body=?,url=?,published_at=?,updated_at=? WHERE id=?',
                   (title, body, url, published_at or '', now, entry_id))
        if not restored:
            record(db,source,source_id,lane=lane,status='pending',snapshot_id=cur.lastrowid)
        else:
            refresh(db, entry_id)
    return db.execute('SELECT id FROM review_snapshots WHERE source=? AND source_id=? AND lane=? AND fingerprint=?',
                      (source, source_id, lane, fp)).fetchone()[0]


def record(db, source, source_id, *, lane, status, result=None, rule_version='', event_id=None,
           snapshot_id=None, decided_at=None, restored=False):
    initialize(db)
    row = db.execute('SELECT id,entry_id FROM review_snapshots WHERE source=? AND source_id=? AND lane=? ORDER BY id DESC LIMIT 1',
                     (source, str(source_id), lane)).fetchone() if snapshot_id is None else db.execute(
                         'SELECT id,entry_id FROM review_snapshots WHERE id=?', (snapshot_id,)).fetchone()
    if not row: return None
    normalized = {'skipped': 'excluded', 'needs_review': 'needs_info', 'unavailable': 'failed',
                  'pending_analysis': 'pending', 'pending_price': 'pending', 'retry': 'pending', 'ready': 'pending',
                  'awaiting_helper': 'needs_info', 'pending_body': 'pending', 'historical': 'unknown', 'trade': 'pending'}.get(status, status)
    if normalized not in STATUSES: normalized = 'unknown'
    value = result or {}
    fp = digest([row['id'], lane, normalized, str(rule_version or ''), value, event_id, restored])
    db.execute('''INSERT OR IGNORE INTO review_decisions(entry_id,snapshot_id,lane,status,rule_version,data,event_id,decided_at,restored,fingerprint)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (row['entry_id'], row['id'], lane, normalized, str(rule_version or ''), dumps(value),
                                      event_id, decided_at or time.time(), int(restored), fp))
    refresh(db, row['entry_id'])
    return row['entry_id']


def latest(db, entry_id):
    return [dict(r) for r in db.execute('''SELECT d.* FROM review_decisions d WHERE d.entry_id=? AND d.id IN
        (SELECT max(v.id) FROM review_decisions v JOIN review_snapshots s ON s.id=v.snapshot_id
         WHERE v.entry_id=? GROUP BY v.lane,s.source,s.source_id) ORDER BY d.id''', (entry_id, entry_id))]


def refresh(db, entry_id):
    entry = db.execute('SELECT * FROM review_entries WHERE id=?', (entry_id,)).fetchone()
    if not entry: return
    decisions = latest(db, entry_id)
    states, categories, reasons = set(), set(), []
    score = 0
    for row in decisions:
        value = json.loads(row['data']); state = row['status']
        if row['event_id'] and state in {'queued','delivered'}:
            event = db.execute('SELECT status FROM events WHERE id=?', (row['event_id'],)).fetchone()
            if event:
                state = {'delivered': 'delivered', 'suppressed': 'excluded', 'uncertain': 'failed', 'failed': 'failed'}.get(event[0], 'queued')
        states.add(state)
        categories.update(str(c) for c in value.get('categories', []) if isinstance(c, str))
        for item in value.get('items', []):
            if not isinstance(item, dict): continue
            product = item.get('item') or item
            if not isinstance(product, dict): continue
            categories.update(str(c) for c in product.get('categories', []) if isinstance(c, str))
            if product.get('category'): categories.add(str(product['category']))
            if item.get('decision') in {'push', 'match'} or item.get('state')=='match' or item.get('matched'):
                score += 8
            if item.get('opportunity') or item.get('price_advantage'): score += 15
        if value.get('policy_decision') == 'push' or value.get('decision') == 'push': score += 8
    # Later analysis cannot erase evidence that this post was actually sent.
    if db.execute("SELECT 1 FROM review_decisions d JOIN events e ON e.id=d.event_id WHERE d.entry_id=? AND e.status='delivered' LIMIT 1", (entry_id,)).fetchone():
        states.add('delivered')
    state = next((s for s in ('delivered', 'queued', 'failed', 'needs_info', 'excluded', 'duplicate', 'pending', 'unknown') if s in states), 'pending')
    text = entry['title'] + '\n' + entry['body']
    signals = [(r'免费|免单|返现|立减|补贴|白嫖|赠送|开源', 12, '明确收益'),
               (r'教程|方法|解决|修复|脚本|部署|额度|优惠', 6, '可行动内容'),
               (r'售价|出价|价格|\d+\s*(?:元|块|[rR]\b)', 6, '明确报价'),
               (r'北京|全国|邮寄|快递', 3, '适用范围'),
               (r'今日|限时|截止|补货|恢复', 4, '时效变化')]
    for pattern, weight, reason in signals:
        if re.search(pattern, text): score += weight; reasons.append(reason)
    if state in {'failed', 'needs_info', 'excluded'} and score >= 12:
        score += 15; reasons.append('优先核对可能漏推')
    if state == 'duplicate': score -= 10
    db.execute('UPDATE review_entries SET status=?,categories=?,priority=?,priority_reasons=? WHERE id=?',
               (state, dumps(sorted(categories)), min(score, 100), dumps(reasons), entry_id))


def sync_deliveries(db):
    for row in db.execute('''SELECT DISTINCT d.entry_id FROM review_decisions d JOIN events e ON e.id=d.event_id
        JOIN review_entries r ON r.id=d.entry_id WHERE (e.status='delivered' AND r.status<>'delivered')
        OR (e.status IN ('uncertain','failed','suppressed') AND r.status='queued')''').fetchall(): refresh(db, row[0])


def detail(db, entry_id):
    row = db.execute('SELECT * FROM review_entries WHERE id=?', (entry_id,)).fetchone()
    if not row: raise ValueError('记录不存在')
    value = dict(row)
    value['categories'] = json.loads(value['categories']); value['priority_reasons'] = json.loads(value['priority_reasons'])
    value['origins'] = [dict(r) for r in db.execute('SELECT * FROM review_origins WHERE entry_id=?', (entry_id,))]
    value['snapshots'] = [{**dict(r), 'data': json.loads(r['data'])} for r in db.execute('SELECT * FROM review_snapshots WHERE entry_id=? ORDER BY id', (entry_id,))]
    value['decisions'] = []
    for r in db.execute('SELECT * FROM review_decisions WHERE entry_id=? ORDER BY id', (entry_id,)):
        decision = {**dict(r), 'data': json.loads(r['data'])}
        event = db.execute('SELECT status,payload,receipt,error,updated_at FROM events WHERE id=?', (r['event_id'],)).fetchone() if r['event_id'] else None
        # Render text and delivery state only; payload may also contain Bark credentials.
        if event: decision['notification'] = {'status': event['status'], 'text': json.loads(event['payload']).get('text', ''), 'at': event['updated_at']}
        value['decisions'].append(decision)
    value['drafts'] = [{**dict(r), 'value': json.loads(r['value'])} for r in db.execute('SELECT * FROM review_feedback WHERE entry_id=? AND batch_id IS NULL', (entry_id,))]
    from .feedback import results
    value['feedback'] = results(db, entry_id=entry_id)
    value['latest'] = [{**r, 'data': json.loads(r['data'])} for r in latest(db, entry_id)]
    value['snapshot_id'] = max((s['id'] for s in value['snapshots']), default=None)
    value['analysis'] = []
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='model_executions'").fetchone():
        from pathlib import Path
        from types import SimpleNamespace
        from pylon.shared.model_execution import active
        path = db.execute('PRAGMA database_list').fetchone()[2]
        if path:
            box = SimpleNamespace(path=Path(path))
            for origin in value['origins']:
                for run in db.execute("SELECT component,started_at,heartbeat_at,request_id,status FROM model_executions WHERE source=? AND source_id=? AND status='running'", (origin['source'],origin['source_id'])):
                    value['analysis'].append({**dict(run), 'status': 'waiting' if active(box,run['component']) else 'interrupted',
                                              'elapsed': max(0,time.time()-run['started_at'])})
    return value


# Whole-post feedback is tied to both the input and judgment versions.
REVIEWED_SQL = '''EXISTS (SELECT 1 FROM review_feedback f WHERE f.entry_id=r.id
        AND f.owner='webops' AND f.batch_id IS NOT NULL AND f.item_key=''
        AND f.snapshot_id=(SELECT max(id) FROM review_snapshots WHERE entry_id=r.id)
        AND f.decision_ids=(SELECT json_group_array(id) FROM
          (SELECT d.id FROM review_decisions d WHERE d.entry_id=r.id AND d.id IN
           (SELECT max(v.id) FROM review_decisions v JOIN review_snapshots s ON s.id=v.snapshot_id
            WHERE v.entry_id=r.id GROUP BY v.lane,s.source,s.source_id) ORDER BY d.id)))'''

def items(db, query):
    sync_deliveries(db)
    page = max(1, int(query.get('page', 1))); where = []; args = []
    size = int(query.get('page_size', 50))
    if size not in (50, 100): raise ValueError('每页可选 50 或 100 条')
    mode = query.get('review', 'pending')
    if mode not in ('pending', 'all'): raise ValueError('审核范围无效')
    reviewed = REVIEWED_SQL
    if mode == 'pending':
        where.append('(NOT ' + reviewed + " OR EXISTS (SELECT 1 FROM review_feedback f WHERE f.entry_id=r.id AND f.owner='webops' AND f.batch_id IS NULL))")
    for key in ('day', 'status'):
        if query.get(key): where.append(f'r.{key}=?'); args.append(query[key])
    if query.get('source'):
        where.append('EXISTS (SELECT 1 FROM review_origins o WHERE o.entry_id=r.id AND o.source=?)'); args.append(query['source'])
    if query.get('lane'):
        where.append('EXISTS (SELECT 1 FROM review_snapshots s WHERE s.entry_id=r.id AND s.lane=?)'); args.append(query['lane'])
    if query.get('category'):
        where.append('EXISTS (SELECT 1 FROM json_each(r.categories) WHERE value=?)'); args.append(query['category'])
    if query.get('q'):
        where.append('instr(lower(r.title||char(10)||r.body),lower(?))>0'); args.append(str(query['q'])[:200])
    sql = ' FROM review_entries r' + (' WHERE ' + ' AND '.join(where) if where else '')
    total = db.execute('SELECT count(*)' + sql, args).fetchone()[0]
    rows = db.execute('SELECT r.id,r.title,r.status,r.categories,r.priority,r.first_seen,r.published_at,r.url,r.day,' +
                      reviewed + ' reviewed' + sql +
                      " ORDER BY (r.status='delivered') DESC,r.priority DESC,r.first_seen DESC,r.id LIMIT ? OFFSET ?",
                      (*args, size, (page-1)*size)).fetchall()
    return {'items': summaries(db, rows), 'total': total, 'page': page, 'page_size': size, 'pages': (total+size-1)//size}


def summaries(db, rows):
    result = []
    for row in rows:
        value = dict(row); value['reviewed'] = bool(value['reviewed'])
        value['categories'] = json.loads(value['categories'])
        value['origins'] = [dict(r) for r in db.execute('SELECT source,source_id FROM review_origins WHERE entry_id=?', (row['id'],))]
        decisions = latest(db, row['id'])
        value['decision_ids'] = [d['id'] for d in decisions]
        products, facts = [], []
        for decision in decisions:
            data = json.loads(decision['data'])
            products.extend(p.get('item') or p for p in data.get('items', []) if isinstance(p, dict))
            facts.extend(str(f.get('value', '')) for f in data.get('facts', []) if isinstance(f, dict))
        value['product_count'] = len(products)
        prices = []
        for product in products:
            if not isinstance(product, dict): continue
            attrs = product.get('attributes') or {}
            price = product.get('price', attrs.get('price'))
            if price is None or isinstance(price, bool) or not str(price).strip(): continue
            quoted_currency = ((attrs.get('ai_price_result') or {}).get('offer') or {}).get('currency')
            if isinstance(quoted_currency, dict): quoted_currency = quoted_currency.get('value')
            currency = product.get('currency') or attrs.get('currency') or quoted_currency or 'CNY'
            prices.append(str(price) + ' ' + {'CNY': '元', 'USD': '美元'}.get(currency, str(currency)))
        value['price_summary'] = ' / '.join(dict.fromkeys(prices))[:100] or ('价格未标' if products else '')
        value['summary'] = '；'.join(facts)[:100]
        value['snapshot_id'] = db.execute('SELECT max(id) FROM review_snapshots WHERE entry_id=?', (row['id'],)).fetchone()[0]
        value['feedback_count'] = db.execute('SELECT count(*) FROM review_feedback WHERE entry_id=?', (row['id'],)).fetchone()[0]
        feedback = db.execute('''SELECT f.batch_id,f.snapshot_id,f.decision_ids,coalesce(x.status,b.status,'draft') status
            FROM review_feedback f LEFT JOIN review_batches b ON b.id=f.batch_id
            LEFT JOIN review_resolutions x ON x.feedback_id=f.id
            WHERE f.entry_id=? AND f.owner='webops' ORDER BY f.updated_at DESC LIMIT 1''', (row['id'],)).fetchone()
        value['feedback_state'] = ''
        if feedback:
            same = feedback['snapshot_id'] == value['snapshot_id'] and sorted(json.loads(feedback['decision_ids'])) == value['decision_ids']
            value['feedback_state'] = feedback['status'] if same else 'updated'
        result.append(value)
    return result


def prune(db, *, now=None):
    cutoff = (now or time.time()) - 90*86400
    ids = [r[0] for r in db.execute('''SELECT id FROM review_entries r WHERE coalesce(first_seen,
        (SELECT min(captured_at) FROM review_snapshots s WHERE s.entry_id=r.id))<? AND NOT EXISTS
        (SELECT 1 FROM review_feedback f LEFT JOIN review_batches b ON b.id=f.batch_id
         WHERE f.entry_id=r.id AND (f.batch_id IS NULL OR b.status<>'fixed'))''', (cutoff,))]
    for entry_id in ids:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='review_recalls'").fetchone():
            for table in ('review_recalls', 'review_daily_choices', 'review_view_rows'):
                db.execute(f'DELETE FROM {table} WHERE entry_id=?', (entry_id,))
        db.execute('DELETE FROM review_resolutions WHERE feedback_id IN (SELECT id FROM review_feedback WHERE entry_id=?)', (entry_id,))
        for table in ('review_feedback', 'review_decisions', 'review_snapshots', 'review_origins'):
            db.execute(f'DELETE FROM {table} WHERE entry_id=?', (entry_id,))
        db.execute('DELETE FROM review_entries WHERE id=?', (entry_id,))
    return len(ids)
