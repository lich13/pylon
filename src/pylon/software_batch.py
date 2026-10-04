"""Persistent explicit selection and immutable, idempotent APT batch requests."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid

from . import scheduler
from .config import profile_env, redact
from .inline import owns_callback
from .outbox import Outbox

PREFIX = 'su2'
REQUEST_PREFIX = 'software-batch:'
SELECTABLE = {'pending', 'failed', 'busy', 'deferred', 'stale', 'skipped'}
ACTIVE = {'submitting', 'queued', 'running'}
MAX_ITEMS = 20


def _signature(profile, action, target):
    token = profile_env(profile).get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ValueError('机器人配置不可用')
    return hmac.new(str(token).encode(), f'su2|{profile}|{action}|{target}'.encode(),
                    hashlib.sha256).hexdigest()[:12]


def callback_data(profile, action, target):
    return f'{PREFIX}|{action}|{target}|{_signature(profile, action, target)}'


def _parse(profile, value):
    parts = str(value or '').split('|')
    if len(parts) != 4 or parts[0] != PREFIX:
        raise ValueError('按钮已失效，请重新 /updates')
    _, action, target, signature = parts
    pattern = r'sur-[0-9a-f]{20}' if action in {'select', 'unselect', 'skip'} else r'[0-9a-f]{24}'
    if action not in {'select', 'unselect', 'skip', 'all', 'clear', 'run'} or not re.fullmatch(pattern, target):
        raise ValueError('按钮参数无效，请重新 /updates')
    if not hmac.compare_digest(signature, _signature(profile, action, target)):
        raise ValueError('按钮签名无效')
    return action, target


def current_candidates(box, candidates):
    """Keep old results, but make superseded fingerprints non-selectable."""
    from .software_updates import _state_row
    rows = [_state_row(box, candidate) for candidate in candidates]
    ids = {row['id'] for row in rows}
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT id,status FROM software_reviews WHERE profile='default'").fetchall():
            available = int(row['id'] in ids)
            db.execute('UPDATE software_reviews SET available=? WHERE id=?', (available, row['id']))
            if not available and row['status'] not in ACTIVE:
                db.execute('UPDATE software_reviews SET selected=0 WHERE id=?', (row['id'],))
    return [box.software_review(row['id']) for row in rows]


def _freeze(rows):
    return [{
        'review_id': row['id'], 'package': row['package_name'], 'before': row['installed_version'],
        'candidate': row['candidate_version'], 'source': row['source'],
        'fingerprint': row['fingerprint'], 'epoch': row['selection_epoch'],
    } for row in sorted(rows, key=lambda x: x['id'])]


def _draft(box, rows):
    if not rows or len(rows) > MAX_ITEMS or any(row['status'] not in SELECTABLE for row in rows):
        return None
    items = _freeze(rows)
    encoded = json.dumps(items, sort_keys=True, separators=(',', ':'))
    key = hashlib.sha256(encoded.encode()).hexdigest()[:24]
    with box.connect() as db:
        db.execute("INSERT OR IGNORE INTO software_update_batches"
                   "(id,profile,items,status,request_id,created_at,updated_at) VALUES(?,?,?,'draft',?,?,?)",
                   (key, 'default', encoded, REQUEST_PREFIX + key, time.time(), time.time()))
    return key


def selection_text(box):
    rows = box.software_selection()
    if not rows:
        return '已选 0 个包。勾选后点击“批量更新已选”；只有最后一步会提交安装。'
    return f'已选 {len(rows)} 个包：' + '、'.join(row['package_name'] for row in rows)


def keyboard(box, rows=None):
    rows = rows if rows is not None else box.software_reviews(profile='default')
    rows = [row for row in rows if row.get('available') and row['status'] in SELECTABLE][:MAX_ITEMS]
    if not rows:
        return None
    selected = box.software_selection()
    view_id = uuid.uuid4().hex[:24]
    with box.connect() as db:
        db.execute('INSERT INTO software_update_views VALUES(?,?,?,?)',
                   (view_id, 'default', json.dumps([row['id'] for row in rows]), time.time()))
        db.execute('DELETE FROM software_update_views WHERE created_at<?', (time.time() - 30 * 86400,))
    buttons = []
    for row in rows:
        chosen = bool(row['selected'])
        buttons.append([
            {'text': ('☑ ' if chosen else '☐ ') + row['package_name'],
             'callback_data': callback_data('default', 'unselect' if chosen else 'select', row['id'])},
            {'text': '跳过此版本', 'callback_data': callback_data('default', 'skip', row['id'])},
        ])
    buttons.append([
        {'text': '全选当前候选', 'callback_data': callback_data('default', 'all', view_id)},
        {'text': '清空选择', 'callback_data': callback_data('default', 'clear', view_id)},
    ])
    draft = _draft(box, selected)
    if draft:
        buttons.append([{'text': f'批量更新已选（{len(selected)}）',
                         'callback_data': callback_data('default', 'run', draft)}])
    return {'inline_keyboard': buttons}


def dashboard(box, note=''):
    from .software_updates import STATES
    rows = [row for row in box.software_reviews(profile='default') if row.get('available')]
    lines = [note] if note else []
    lines.append(selection_text(box))
    for row in sorted(rows, key=lambda x: x['package_name']):
        state = STATES.get(row['status'], row['status'])
        lines.append(f"{'☑' if row['selected'] else '☐'} {row['package_name']}："
                     f"{row['installed_version']} → {row['candidate_version']}｜{state}")
    lines.append('只升级明确选中的包。关联包需一并勾选；系统分阶段暂缓时整批不安装。')
    return {'text': '\n'.join(lines), 'reply_markup': keyboard(box, rows)}


def _select(db, ids, selected):
    rows = []
    for review_id in ids:
        row = db.execute('SELECT * FROM software_reviews WHERE id=? AND profile=?',
                         (review_id, 'default')).fetchone()
        if not row or not row['available'] or row['status'] not in SELECTABLE:
            raise ValueError('候选状态已变化，请重新 /updates')
        rows.append(row)
    count = db.execute("SELECT count(*) FROM software_reviews WHERE selected=1 AND available=1").fetchone()[0]
    if selected and count + sum(not row['selected'] for row in rows) > MAX_ITEMS:
        raise ValueError('一次最多选择 20 个包')
    for row in rows:
        if bool(row['selected']) == selected:
            continue
        db.execute('UPDATE software_reviews SET selected=?,selection_epoch=selection_epoch+?,updated_at=? WHERE id=?',
                   (int(selected), int(selected), time.time(), row['id']))
        if selected and row['status'] == 'skipped':
            db.execute("UPDATE software_reviews SET status='pending',detail='用户重新选择此版本' WHERE id=?", (row['id'],))


def _load_batch(box, key):
    with box.connect() as db:
        row = db.execute('SELECT * FROM software_update_batches WHERE id=?', (key,)).fetchone()
    return dict(row) if row else None


def batch_text(batch):
    from .software_updates import STATES
    names = '、'.join(item['package'] for item in json.loads(batch['items']))
    status = STATES.get(batch['status'], batch['status'])
    text = f"系统小包批次 {batch['id'][:8]}：{status}\n所选包：{names}"
    if batch.get('detail'):
        text += '\n' + redact(batch['detail'])[-1500:]
    return text


def submit(box, key):
    """Claim first; a crash or lost root response is reconciled, never reinstalled."""
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM software_update_batches WHERE id=?', (key,)).fetchone()
        if not row:
            raise ValueError('批次不存在，请重新 /updates')
        batch = dict(row)
        if batch['status'] != 'draft':
            return batch_text(batch)
        items = json.loads(batch['items'])
        selected = [dict(row) for row in db.execute(
            "SELECT * FROM software_reviews WHERE selected=1 AND available=1 AND profile='default' ORDER BY id")]
        if _freeze(selected) != items or any(row['status'] not in SELECTABLE for row in selected):
            raise ValueError('选择已变化，请使用最新卡片确认')
        db.execute("UPDATE software_update_batches SET status='submitting',updated_at=? WHERE id=?", (time.time(), key))
        for item in items:
            db.execute("UPDATE software_reviews SET status='queued',batch_id=?,request_id=?,updated_at=? WHERE id=?",
                       (key, batch['request_id'], time.time(), item['review_id']))
    wire = [{k: item[k] for k in ('package', 'before', 'candidate')} for item in items]
    try:
        row = json.loads(scheduler.admin('software', 'apt-batch', 'default', batch['request_id'],
                                        json.dumps(wire, separators=(',', ':'))))
        with box.connect() as db:
            db.execute("UPDATE software_update_batches SET status=?,root_id=?,updated_at=?"
                       " WHERE id=? AND status='submitting'",
                       (row.get('status', 'uncertain'), row.get('id'), time.time(), key))
        # Terminal results use the regular durable result/ack path as well.
        if row.get('status') not in {'queued', 'running'}:
            sync_result(box, row | {'request_id': batch['request_id']})
        return f'已提交系统小包批次 {key[:8]}，共 {len(items)} 个包。\n完成后自动汇总回报；重复点击不会再次安装。'
    except Exception as exc:
        with box.connect() as db:
            db.execute("UPDATE software_update_batches SET detail=?,updated_at=? WHERE id=? AND status='submitting'",
                       ('提交回执待核对：' + redact(str(exc))[:500], time.time(), key))
        return '提交回执待核对；后台将查询原批次，不会重复提交安装。'


def callback(profile, payload, request_key=''):
    box = Outbox()
    if profile != 'default' or not owns_callback(profile, payload, box=box):
        raise ValueError('只允许本人默认机器人私聊及原始消息操作')
    action, target = _parse(profile, payload.get('data'))
    if action == 'run':
        return {'text': submit(box, target)}
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if action in {'all', 'clear'}:
            view = db.execute('SELECT * FROM software_update_views WHERE id=? AND profile=?',
                              (target, profile)).fetchone()
            if not view:
                raise ValueError('卡片已过期，请重新 /updates')
            if action == 'clear':
                # Never change a submitted batch while it is running.
                db.execute("UPDATE software_reviews SET selected=0 WHERE status NOT IN ('queued','running','submitting')")
            else:
                ids = json.loads(view['candidates'])
                rows = [db.execute('SELECT * FROM software_reviews WHERE id=?', (key,)).fetchone() for key in ids]
                if any(row is None or not row['available'] for row in rows):
                    raise ValueError('候选已变化，请重新 /updates')
                _select(db, [row['id'] for row in rows if row['status'] in SELECTABLE and row['status'] != 'skipped'], True)
        elif action == 'skip':
            row = db.execute('SELECT * FROM software_reviews WHERE id=?', (target,)).fetchone()
            if not row or row['status'] not in SELECTABLE or not row['available']:
                raise ValueError('候选状态已变化，请重新 /updates')
            db.execute("UPDATE software_reviews SET selected=0,status='skipped',detail=?,updated_at=? WHERE id=?",
                       ('用户跳过当前版本', time.time(), target))
        else:
            _select(db, [target], action == 'select')
    return dashboard(box, '选择已保存。' if action != 'skip' else '已跳过此版本；新版本仍会提示。')


def sync_result(box, row):
    request_id = str(row.get('request_id') or '')
    if not request_id.startswith(REQUEST_PREFIX):
        return False
    key = request_id.removeprefix(REQUEST_PREFIX)
    batch = _load_batch(box, key)
    if not batch:
        return False
    from .software_updates import result_text
    detail = result_text(row)
    item_results = json.loads(row.get('item_results') or '[]')
    item_statuses = {item['package']: item['status'] for item in item_results}
    status = str(row.get('status') or 'uncertain')
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE software_update_batches SET status=?,root_id=?,detail=?,updated_at=? WHERE id=?",
                   (status, row.get('id'), detail, time.time(), key))
        for item in json.loads(batch['items']):
            item_status = item_statuses.get(item['package'], status)
            # Overall audit/health failure must not be hidden by a changed version.
            if status in {'failed', 'uncertain'} and item_status == 'updated':
                item_status = status
            db.execute('UPDATE software_reviews SET status=?,selected=0,detail=?,updated_at=? WHERE id=? AND batch_id=?',
                       (item_status, detail, time.time(), item['review_id'], key))
        box.enqueue(profile='default', job_id='software-update', kind='text',
                    payload={'text': detail}, key='software-batch-result:' + key, connection=db)
    return True


def recover_submissions(box):
    """Query an interrupted submission once it is old enough; never call start."""
    for batch in box.software_batches():
        if batch['status'] != 'submitting' or time.time() - batch['updated_at'] < 60:
            continue
        row = json.loads(scheduler.admin('software', 'request', batch['request_id']))
        if row:
            if row['status'] in {'queued', 'running'}:
                with box.connect() as db:
                    db.execute("UPDATE software_update_batches SET status=?,root_id=?,updated_at=? WHERE id=?",
                               (row['status'], row['id'], time.time(), batch['id']))
            else:
                sync_result(box, row | {'request_id': batch['request_id']})
        else:
            sync_result(box, {'id': '', 'request_id': batch['request_id'], 'target': 'apt-batch:',
                              'status': 'uncertain', 'detail': '提交中断，root 未找到原批次；未自动重复安装，请核对。'})
