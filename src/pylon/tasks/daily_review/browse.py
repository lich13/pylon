"""Stable review windows and owner-controlled retrieval, never delivery policy."""
from __future__ import annotations

import base64
import json
import time
import uuid

from . import store

GROUPS = {'other': '其余候选', 'excluded': '已过滤', 'unresolved': '资料不足／处理失败', 'duplicate': '重复'}
LIMIT = 50
SCHEMA = '''
CREATE TABLE IF NOT EXISTS review_daily_lists(day TEXT PRIMARY KEY,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_daily_choices(day TEXT NOT NULL,entry_id TEXT NOT NULL,rank INTEGER NOT NULL,
 PRIMARY KEY(day,entry_id));
CREATE TABLE IF NOT EXISTS review_recalls(owner TEXT NOT NULL,entry_id TEXT NOT NULL,active INTEGER NOT NULL,
 updated_at REAL NOT NULL,PRIMARY KEY(owner,entry_id));
CREATE TABLE IF NOT EXISTS review_views(id TEXT PRIMARY KEY,owner TEXT NOT NULL,query TEXT NOT NULL,
 revision TEXT NOT NULL,created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS review_view_rows(view_id TEXT NOT NULL,entry_id TEXT NOT NULL,section TEXT NOT NULL,
 position INTEGER NOT NULL,snapshot_id INTEGER NOT NULL,categories TEXT NOT NULL,sources TEXT NOT NULL,
 PRIMARY KEY(view_id,entry_id));
CREATE INDEX IF NOT EXISTS review_view_section ON review_view_rows(view_id,section,position);
'''


def initialize(db):
    store.initialize(db)
    for sql in SCHEMA.split(';'):
        if sql.strip(): db.execute(sql)


def revision(db):
    # Delivery changes and user feedback matter even without a new source input.
    values = []
    for table, expr in [('review_snapshots', 'max(id)'), ('review_decisions', 'max(id)'),
                        ('review_feedback', 'max(updated_at)'), ('review_resolutions', 'max(updated_at)'),
                        ('review_recalls', 'max(updated_at)')]:
        values.append(tuple(db.execute(f'SELECT count(*),{expr} FROM {table}').fetchone()))
    values.append(tuple(db.execute('''SELECT count(*),max(e.updated_at) FROM events e JOIN
        (SELECT DISTINCT event_id FROM review_decisions WHERE event_id IS NOT NULL) d
        ON d.event_id=e.id''').fetchone()))
    return store.digest(values)


def evidence_rank(values):
    """Structured evidence outranks old lexical priority; no new model request."""
    opportunity = matched = conflict = 0
    for value in values:
        match = value.get('decision') == 'push' or value.get('policy_decision') == 'push'
        if value.get('decision') == 'push' and value.get('policy_decision') in {'skip', 'hold'}:
            conflict = 1
        for part in value.get('items', []):
            if not isinstance(part, dict): continue
            if part.get('state') == 'match' or part.get('decision') in {'match', 'push'} or part.get('matched'):
                match = True
            if part.get('price_advantage') or part.get('opportunity'): opportunity = 1
        if value.get('matches') or value.get('price_advantage'): opportunity = 1
        matched = max(matched, int(match))
    return (opportunity, conflict, matched)


def choose_day(db, day):
    if db.execute('SELECT 1 FROM review_daily_lists WHERE day=?', (day,)).fetchone(): return
    rows = db.execute("SELECT id,priority,first_seen FROM review_entries WHERE day=? AND status NOT IN ('delivered','duplicate')", (day,)).fetchall()
    judgments = {}
    for row in db.execute('''SELECT d.entry_id,d.data FROM review_decisions d
        JOIN review_entries r ON r.id=d.entry_id WHERE r.day=? AND d.id IN
        (SELECT max(v.id) FROM review_decisions v JOIN review_snapshots s ON s.id=v.snapshot_id
         JOIN review_entries e ON e.id=v.entry_id WHERE e.day=? GROUP BY v.entry_id,v.lane,s.source,s.source_id)''', (day, day)):
        judgments.setdefault(row['entry_id'], []).append(json.loads(row['data']))
    ranked = sorted(rows, key=lambda r: (*evidence_rank(judgments.get(r['id'], [])), r['priority'], r['first_seen'] or 0, r['id']), reverse=True)
    db.execute('INSERT INTO review_daily_lists VALUES(?,?)', (day, time.time()))
    db.executemany('INSERT INTO review_daily_choices VALUES(?,?,?)', [(day, r['id'], i) for i, r in enumerate(ranked[:LIMIT])])


def filters(query):
    mode = query.get('review', 'pending')
    if mode not in {'pending', 'all'}: raise ValueError('审核范围无效')
    clauses, args = [], []
    if mode == 'pending':
        clauses.append('(NOT ' + store.REVIEWED_SQL + " OR EXISTS (SELECT 1 FROM review_feedback f WHERE f.entry_id=r.id AND f.owner='webops' AND f.batch_id IS NULL))")
    for key in ('day', 'status'):
        if query.get(key): clauses.append(f'r.{key}=?'); args.append(str(query[key]))
    if query.get('source'):
        clauses.append('EXISTS (SELECT 1 FROM review_origins o WHERE o.entry_id=r.id AND o.source=?)'); args.append(str(query['source']))
    if query.get('lane'):
        clauses.append('EXISTS (SELECT 1 FROM review_snapshots s WHERE s.entry_id=r.id AND s.lane=?)'); args.append(str(query['lane']))
    if query.get('category'):
        clauses.append('EXISTS (SELECT 1 FROM json_each(r.categories) WHERE value=?)'); args.append(str(query['category']))
    if query.get('q'):
        clauses.append('instr(lower(r.title||char(10)||r.body),lower(?))>0'); args.append(str(query['q'])[:200])
    return ' AND '.join(clauses) or '1', args


def _view(db, view_id, owner):
    row = db.execute('SELECT * FROM review_views WHERE id=? AND owner=? AND created_at>?', (view_id, owner, time.time()-7*86400)).fetchone()
    if not row: raise ValueError('浏览记录已失效，请刷新列表')
    return row


def _counts(db, view_id):
    counts = {r['section']: r['n'] for r in db.execute('SELECT section,count(*) n FROM review_view_rows WHERE view_id=? GROUP BY section', (view_id,))}
    main = counts.get('main', 0)
    total = sum(counts.values())
    return {'main': main, 'stored': total-main, 'total': total}


def _groups(db, view_id):
    values = []
    for key, label in GROUPS.items():
        count = db.execute('SELECT count(*) FROM review_view_rows WHERE view_id=? AND section=?', (view_id, key)).fetchone()[0]
        titles = [json.loads(r[0]).get('title', '') for r in db.execute('''SELECT s.data FROM review_view_rows v
            JOIN review_snapshots s ON s.id=v.snapshot_id WHERE v.view_id=? AND v.section=? ORDER BY v.position LIMIT 3''', (view_id, key))]
        values.append({'key': key, 'label': label, 'count': count, 'titles': titles})
    return values


def create(db, query, *, owner='webops'):
    initialize(db)
    store.sync_deliveries(db)
    query = {k: str(query[k]) for k in ('day', 'source', 'lane', 'status', 'category', 'q', 'review') if query.get(k)}
    where, args = filters(query)
    days = [r[0] for r in db.execute('SELECT DISTINCT day FROM review_entries' + (' WHERE day=?' if query.get('day') else ''), (query['day'],) if query.get('day') else ())]
    for day in days: choose_day(db, day)
    expired = [r[0] for r in db.execute('SELECT id FROM review_views WHERE created_at<?', (time.time()-7*86400,))]
    for key in expired:
        db.execute('DELETE FROM review_view_rows WHERE view_id=?', (key,))
        db.execute('DELETE FROM review_views WHERE id=?', (key,))
    view_id = uuid.uuid4().hex
    db.execute('INSERT INTO review_views VALUES(?,?,?,?,?)', (view_id, owner, store.dumps(query), revision(db), time.time()))
    rows = db.execute('''SELECT r.id,r.status,r.categories,r.priority,r.first_seen,
        coalesce(c.active,0) recalled,d.rank,
        (SELECT max(id) FROM review_snapshots WHERE entry_id=r.id) snapshot_id,
        (SELECT json_group_array(source) FROM review_origins WHERE entry_id=r.id) sources
        FROM review_entries r LEFT JOIN review_recalls c ON c.entry_id=r.id AND c.owner=?
        LEFT JOIN review_daily_choices d ON d.entry_id=r.id AND d.day=r.day WHERE ''' + where + '''
        ORDER BY CASE WHEN r.status='delivered' THEN 0 WHEN c.active=1 THEN 1 WHEN d.rank IS NOT NULL THEN 2 ELSE 3 END,
        CASE WHEN r.status<>'delivered' AND coalesce(c.active,0)=0 THEN d.rank END,
        r.priority DESC,r.first_seen DESC,r.id''', (owner, *args)).fetchall()
    records = []
    for i, row in enumerate(rows):
        section = ('main' if row['status'] == 'delivered' or row['recalled'] or row['rank'] is not None else
                   'duplicate' if row['status'] == 'duplicate' else 'excluded' if row['status'] == 'excluded' else
                   'unresolved' if row['status'] in {'needs_info', 'failed', 'unknown'} else 'other')
        records.append((view_id, row['id'], section, i, row['snapshot_id'], row['categories'], row['sources']))
    db.executemany('INSERT INTO review_view_rows VALUES(?,?,?,?,?,?,?)', records)
    result = feed(db, {'view': view_id, 'section': 'main'}, owner=owner)
    return {**result, 'counts': _counts(db, view_id), 'groups': _groups(db, view_id), 'has_updates': False, 'query': query}


def _cursor(view_id, section, query, position):
    signature = store.digest([view_id, section, {k: str(query.get(k, '')) for k in ('source', 'category', 'q')}, position])[:24]
    return base64.urlsafe_b64encode(f'{position}:{signature}'.encode()).decode().rstrip('=')


def feed(db, query, *, owner='webops'):
    view_id = str(query.get('view', ''))
    _view(db, view_id, owner)
    section = query.get('section', 'main')
    if section not in {'main', *GROUPS}: raise ValueError('分组不存在')
    position = -1
    if query.get('cursor'):
        try:
            raw = str(query['cursor'])
            position = int(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)).decode().split(':')[0])
            if raw != _cursor(view_id, section, query, position) or position < -1: raise ValueError()
        except (ValueError, UnicodeError): raise ValueError('续载位置无效') from None
    clauses, args = ['v.view_id=?', 'v.section=?'], [view_id, section]
    for key, col in [('source', 'sources'), ('category', 'categories')]:
        if query.get(key): clauses.append(f'EXISTS (SELECT 1 FROM json_each(v.{col}) WHERE value=?)'); args.append(str(query[key]))
    if query.get('q'):
        clauses.append("instr(lower(json_extract(s.data,'$.title')||char(10)||json_extract(s.data,'$.body')),lower(?))>0")
        args.append(str(query['q'])[:200])
    sql = ' FROM review_view_rows v JOIN review_snapshots s ON s.id=v.snapshot_id JOIN review_entries r ON r.id=v.entry_id WHERE ' + ' AND '.join(clauses)
    total = db.execute('SELECT count(*)' + sql, args).fetchone()[0]
    rows = db.execute('SELECT r.id,r.title,r.status,r.categories,r.priority,r.first_seen,r.published_at,r.url,r.day,v.position,' + store.REVIEWED_SQL + ' reviewed' + sql + ' AND v.position>? ORDER BY v.position LIMIT 51', (*args, position)).fetchall()
    values = store.summaries(db, rows[:LIMIT])
    for value in values:
        recall_row = db.execute('SELECT active FROM review_recalls WHERE owner=? AND entry_id=?', (owner, value['id'])).fetchone()
        value['recalled'] = bool(recall_row and recall_row[0])
    cursor = _cursor(view_id, section, query, rows[LIMIT-1]['position']) if len(rows) > LIMIT else None
    return {'view_id': view_id, 'section': section, 'items': values, 'total': total, 'next_cursor': cursor}


def updates(db, view_id, *, owner='webops'):
    view = _view(db, view_id, owner)
    return {'has_updates': revision(db) != view['revision']}


def resume(db, view_id, *, owner='webops'):
    view = _view(db, view_id, owner)
    return {**feed(db, {'view': view_id}, owner=owner), 'counts': _counts(db, view_id),
            'groups': _groups(db, view_id), 'query': json.loads(view['query']),
            **updates(db, view_id, owner=owner)}


def recall(db, targets, *, active=True, owner='webops'):
    initialize(db)
    if type(active) is not bool: raise ValueError('取回状态无效')
    if not isinstance(targets, list) or not 1 <= len(targets) <= 200 or any(not isinstance(v, str) for v in targets):
        raise ValueError('一次可取回 1 至 200 条')
    if len(set(targets)) != len(targets): raise ValueError('重复记录')
    for entry_id in targets:
        if not db.execute('SELECT 1 FROM review_entries WHERE id=?', (entry_id,)).fetchone(): raise ValueError('记录不存在')
    changed = 0
    for entry_id in targets:
        old = db.execute('SELECT active FROM review_recalls WHERE owner=? AND entry_id=?', (owner, entry_id)).fetchone()
        if bool(old and old[0]) == active: continue
        db.execute('INSERT INTO review_recalls VALUES(?,?,?,?) ON CONFLICT(owner,entry_id) DO UPDATE SET active=excluded.active,updated_at=excluded.updated_at', (owner, entry_id, int(active), time.time()))
        changed += 1
    return {'changed': changed, 'active': active}
