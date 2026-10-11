"""Explicit feedback batches. Saving feedback never changes a filtering policy."""
from __future__ import annotations

import json
import re
import time
import uuid
from .store import detail, dumps, digest, latest

ACTIONS = {'correct': '判断正确', 'should_push': '应该推送', 'not_interested': '不应推送／不感兴趣',
           'category': '分类错误', 'facts': '价格或条件错误'}
SCOPES = {'item': '当前帖子或商品', 'brand': '品牌', 'series': '系列', 'model': '型号',
          'keyword': '关键词', 'category': '整个类别'}


class Conflict(ValueError): pass


def validate(value):
    if not isinstance(value, dict) or set(value)-{'actions', 'category', 'price', 'region', 'reason', 'scope', 'scope_value'}:
        raise ValueError('反馈格式无效')
    actions = value.get('actions', [])
    if not isinstance(actions, list) or not actions or any(v not in ACTIONS for v in actions): raise ValueError('请选择反馈意见')
    if 'correct' in actions and len(set(actions)) > 1: raise ValueError('判断正确不能与纠正意见同时选择')
    if 'should_push' in actions and 'not_interested' in actions: raise ValueError('推送意见互相冲突')
    result = {'actions': sorted(set(actions))}
    for key in ('category', 'price', 'region', 'reason', 'scope_value'):
        text = value.get(key, '')
        if not isinstance(text, str) or len(text) > (2000 if key == 'reason' else 200): raise ValueError('反馈文字过长')
        result[key] = text.strip()
    result['scope'] = value.get('scope', 'item')
    if result['scope'] not in SCOPES: raise ValueError('适用范围无效')
    if result['scope'] != 'item' and not result['scope_value']: raise ValueError('请填写扩大的适用范围')
    if result['scope'] == 'item': result['scope_value'] = ''
    return result


def save(db, targets, value, *, owner='webops'):
    value = validate(value)
    if not isinstance(targets, list) or not 1 <= len(targets) <= 200: raise ValueError('一次最多填写 200 条反馈')
    if any(not isinstance(t,dict) for t in targets):raise ValueError('反馈对象无效')
    if len({(str(t.get('entry_id','')),str(t.get('item_key',''))) for t in targets})!=len(targets):raise ValueError('重复反馈对象')
    now = time.time(); saved = []
    for target in targets:
        entry_id = str(target.get('entry_id', '')); item_key = str(target.get('item_key', ''))
        if len(item_key) > 100: raise ValueError('商品标识无效')
        snapshot = db.execute('SELECT max(id) FROM review_snapshots WHERE entry_id=?', (entry_id,)).fetchone()[0]
        if not snapshot: raise ValueError('记录不存在')
        if snapshot != target.get('snapshot_id'): raise Conflict('原文已更新，请重新打开记录再填写反馈')
        if item_key:
            # Item keys identify the exact decision and item index shown in the UI.
            match = re.fullmatch(r'(\d+):(\d+)', item_key)
            decision = db.execute('SELECT data FROM review_decisions WHERE id=? AND entry_id=?',
                                  (int(match[1]), entry_id)).fetchone() if match else None
            products = json.loads(decision[0]).get('items', []) if decision else []
            if not match or int(match[2]) >= len(products): raise ValueError('商品已变化，请重新选择')
        old = db.execute('SELECT * FROM review_feedback WHERE owner=? AND entry_id=? AND item_key=? AND batch_id IS NULL',
                         (owner, entry_id, item_key)).fetchone()
        if old and old['revision'] != target.get('revision'): raise Conflict('另一设备已修改此意见，请刷新后重试')
        if not old and target.get('revision') not in (None, 0): raise Conflict('反馈草稿已提交或删除，请刷新')
        shown = target.get('decision_ids', [r['id'] for r in latest(db, entry_id)])
        if (not isinstance(shown,list) or len(shown)>100 or any(type(i) is not int or not db.execute(
                'SELECT 1 FROM review_decisions WHERE id=? AND entry_id=?',(i,entry_id)).fetchone() for i in shown)):
            raise ValueError('原判断标识无效')
        shown = sorted(set(shown))
        if shown != [r['id'] for r in latest(db, entry_id)]:
            raise Conflict('判断已更新，请重新打开记录后填写反馈')
        ids = dumps(shown)
        if old:
            fid = old['id']
            db.execute('UPDATE review_feedback SET value=?,snapshot_id=?,decision_ids=?,revision=revision+1,updated_at=? WHERE id=?',
                       (dumps(value), snapshot, ids, now, fid))
        else:
            fid = uuid.uuid4().hex
            db.execute('''INSERT INTO review_feedback(id,owner,entry_id,item_key,snapshot_id,decision_ids,value,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)''', (fid, owner, entry_id, item_key, snapshot, ids, dumps(value), now, now))
        saved.append(dict(db.execute('SELECT * FROM review_feedback WHERE id=?', (fid,)).fetchone()))
    return {'saved': saved}


def discard(db, targets, *, owner='webops'):
    if not isinstance(targets,list) or len(targets)>1000 or any(not isinstance(t,dict) or 'id' not in t for t in targets):raise ValueError('草稿标识无效')
    for target in targets:
        old = db.execute('SELECT * FROM review_feedback WHERE id=? AND owner=? AND batch_id IS NULL', (target['id'], owner)).fetchone()
        if not old: continue
        if old['revision'] != target.get('revision'): raise Conflict('草稿已变化，请刷新后重试')
        db.execute('DELETE FROM review_feedback WHERE id=?', (old['id'],))
    return {'discarded': len(targets)}


def submit(db, targets, *, owner='webops'):
    if not isinstance(targets, list) or not 1 <= len(targets) <= 200: raise ValueError('每批可提交 1 至 200 条反馈')
    if any(not isinstance(t,dict) or not isinstance(t.get('id'),str) or type(t.get('revision')) is not int for t in targets):raise ValueError('反馈标识无效')
    if len({t['id'] for t in targets}) != len(targets): raise ValueError('重复反馈标识')
    request_id = digest([owner, sorted((t['id'], t['revision']) for t in targets)])
    old = db.execute('SELECT * FROM review_batches WHERE request_id=?', (request_id,)).fetchone()
    if old:
        count = db.execute('SELECT count(*) FROM review_feedback WHERE batch_id=?', (old['id'],)).fetchone()[0]
        return {'batch_id': old['id'], 'status': old['status'], 'repeated': True, 'count': count}
    rows = []
    for target in targets:
        row = db.execute('SELECT * FROM review_feedback WHERE id=? AND owner=? AND batch_id IS NULL', (target['id'], owner)).fetchone()
        if not row or row['revision'] != target['revision']: raise Conflict('反馈草稿已变化，请刷新后提交')
        current = db.execute('SELECT max(id) FROM review_snapshots WHERE entry_id=?', (row['entry_id'],)).fetchone()[0]
        if current != row['snapshot_id']: raise Conflict('本批存在已更新的原文，请重新核对后提交')
        if sorted(json.loads(row['decision_ids'])) != [r['id'] for r in latest(db, row['entry_id'])]:
            raise Conflict('本批存在已更新的判断，请重新核对后提交')
        rows.append(row)
    bid = uuid.uuid4().hex; now = time.time()
    db.execute('INSERT INTO review_batches(id,owner,request_id,status,created_at,updated_at) VALUES(?,?,?,\'submitted\',?,?)',
               (bid, owner, request_id, now, now))
    for row in rows:
        # Preserve the exact displayed evidence and original decisions for repair/regression work.
        frozen = detail(db, row['entry_id'])
        for field in ('drafts', 'feedback', 'latest'): frozen.pop(field, None)
        shown=set(json.loads(row['decision_ids']))
        frozen['decisions']=[d for d in frozen['decisions'] if d['id'] in shown]
        value = json.loads(row['value']); value['snapshot'] = frozen
        db.execute('UPDATE review_feedback SET batch_id=?,value=?,updated_at=? WHERE id=?', (bid, dumps(value), now, row['id']))
    return {'batch_id': bid, 'status': 'submitted', 'count': len(rows)}


def batches(db, *, owner='webops', unresolved=False):
    result = []
    for row in db.execute('SELECT * FROM review_batches WHERE owner=?' + (" AND status<>'fixed'" if unresolved else '') + ' ORDER BY created_at DESC', (owner,)):
        result.append({**dict(row), 'result': json.loads(row['result']),
                       'count': db.execute('SELECT count(*) FROM review_feedback WHERE batch_id=?', (row['id'],)).fetchone()[0],
                       'fixed_count': db.execute("SELECT count(*) FROM review_resolutions x JOIN review_feedback f ON f.id=x.feedback_id WHERE f.batch_id=? AND x.status='fixed'", (row['id'],)).fetchone()[0]})
    return result


def results(db, *, entry_id=None, batch_id=None):
    where, args = ["f.owner='webops'", 'f.batch_id IS NOT NULL'], []
    for column, val in (('entry_id', entry_id), ('batch_id', batch_id)):
        if val is not None: where.append('f.'+column+'=?'); args.append(val)
    return [{**dict(row), 'result': json.loads(row['result']), 'value': json.loads(row['value'])}
            for row in db.execute('''SELECT f.*,coalesce(x.status,b.status) status,coalesce(x.result,'{}') result
                FROM review_feedback f JOIN review_batches b ON b.id=f.batch_id
                LEFT JOIN review_resolutions x ON x.feedback_id=f.id WHERE ''' + ' AND '.join(where) +
                ' ORDER BY f.updated_at DESC', args)]


def update_batch(db, batch_id, *, status, result):
    """Maintenance-only reconciliation, never exposed as a web mutation."""
    if status not in {'processing', 'fixed', 'needs_info'}: raise ValueError('invalid batch state')
    if not isinstance(result, dict): raise ValueError('反馈处理结果无效')
    old = db.execute('SELECT * FROM review_batches WHERE id=?', (batch_id,)).fetchone()
    if not old: raise ValueError('batch not found')
    ids = {r[0] for r in db.execute('SELECT id FROM review_feedback WHERE batch_id=?', (batch_id,))}
    resolved = {r['feedback_id']: dict(r) for r in db.execute('SELECT * FROM review_resolutions WHERE feedback_id IN (SELECT id FROM review_feedback WHERE batch_id=?)', (batch_id,))}
    comparisons = result.get('comparisons', [])
    if not isinstance(comparisons, list) or len(comparisons) > 1000: raise ValueError('逐条结果无效')
    updates = {}
    for row in comparisons:
        if not isinstance(row, dict) or row.get('feedback_id') not in ids or row['feedback_id'] in updates:
            raise ValueError('请提供本批不重复的具体反馈 ID')
        item_status = row.get('status', 'fixed')
        if item_status not in {'processing', 'fixed', 'needs_info'}: raise ValueError('反馈状态无效')
        value = {**row, 'commit': row.get('commit', result.get('commit', ''))}
        if item_status == 'fixed' and (not re.fullmatch(r'[0-9a-f]{40}', str(value['commit'])) or
                any(not isinstance(value.get(key), str) or not value[key].strip() for key in ('before', 'after', 'evidence'))):
            raise ValueError('修复需提供完整提交、逐样本前后结果和验证证据')
        updates[row['feedback_id']] = {'status': item_status, 'result': dumps(value)}
    combined = {**resolved, **updates}
    all_fixed = bool(ids) and all(combined.get(fid, {}).get('status') == 'fixed' for fid in ids)
    if status == 'fixed' and not all_fixed: raise ValueError('本批还有未修复反馈，不能标记整批完成')
    now = time.time()
    for fid, value in updates.items():
        db.execute('INSERT OR REPLACE INTO review_resolutions VALUES(?,?,?,?)', (fid, value['status'], value['result'], now))
    for fid in ids - set(updates):
        if resolved.get(fid, {}).get('status') == 'fixed': continue
        db.execute('INSERT OR REPLACE INTO review_resolutions VALUES(?,?,?,?)',
                   (fid, status, dumps({'feedback_id':fid,'status':status,'reason': result.get('reason', '')}), now))
    final_status = 'fixed' if all_fixed else ('needs_info' if status == 'needs_info' else 'processing')
    history = json.loads(old['result'])
    history.update({k: v for k, v in result.items() if k != 'comparisons'})
    history['comparisons'] = [json.loads(r['result']) for r in db.execute(
        'SELECT result FROM review_resolutions WHERE feedback_id IN (SELECT id FROM review_feedback WHERE batch_id=?)', (batch_id,))]
    db.execute('UPDATE review_batches SET status=?,result=?,updated_at=? WHERE id=?', (final_status, dumps(history), now, batch_id))
