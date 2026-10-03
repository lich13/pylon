"""One Gmail inbox for batch examples and button-driven rule review."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid

from . import cleanup, review, store
from .logic import fingerprint, get_message_full

MAX_SAMPLES = 20
PREFIX = 'g3'
TERMINAL = {'applied', 'rejected', 'cancelled'}
NAMES = {'draft': '待提交', 'reviewing': 'Grok 分析中', 'waiting': '等待补充',
         'proposal_pending': '草案待确认', 'error': '分析失败', 'applied': '已应用',
         'rejected': '已拒绝'}


def _path():
    return cleanup.DATA_DIR / 'gmail_batches.json'


def state():
    return store.read(_path(), {'version': 1, 'active': '', 'batches': {}, 'callbacks': {}})


def save(data):
    store.write(_path(), data)


def current(data, *, create=True):
    batch = data['batches'].get(data.get('active'))
    if batch and batch['status'] == 'proposal_pending' and batch.get('proposal_ids'):
        last = review._proposals()['proposals'].get(batch['proposal_ids'][-1], {})
        if last.get('status') in {'applied', 'rejected'}:
            batch['status'] = 'applied' if last['status'] == 'applied' else 'draft'
    if (not batch or batch['status'] in TERMINAL) and create:
        bid = uuid.uuid4().hex[:12]
        batch = {'id': bid, 'status': 'draft', 'items': [], 'feedback': [], 'task_id': '',
                 'proposal_ids': [], 'pending_replies': {}, 'revision': 0, 'created_at': review._now()}
        if not data.get('legacy_samples_imported'):
            for cid, old in review._state()['reviews'].items():
                if old.get('status') not in {'pending', 'error', 'reviewing', 'waiting'} or old.get('task_id'):
                    continue
                for mode in ('delete', 'keep'):
                    for item in old.get('examples', {}).get(mode, []):
                        if len(batch['items']) < MAX_SAMPLES and not any(x['message_id'] == item.get('message_id') for x in batch['items']):
                            batch['items'].append({'candidate_id': cid, 'message_id': item['message_id'],
                                                   'mode': mode, 'reason': item.get('reason', '')})
            data['legacy_samples_imported'] = True
        data['batches'][bid] = batch
        data['active'] = bid
    return batch


def token(profile, action, target='home'):
    signature = review._signature(profile, PREFIX + ':' + action, target)
    value = f'{PREFIX}|{action}|{target}|{signature}'
    if len(value.encode()) > 64:
        raise ValueError('按钮参数过长')
    return value


def button(profile, text, action, target='home'):
    return {'text': text, 'callback_data': token(profile, action, target)}


def navigation(profile):
    return [button(profile, '继续选样本', 'list', '0'), button(profile, '本批样本', 'home')]


def _candidate_ids(batch):
    return list(dict.fromkeys(item['candidate_id'] for item in batch['items']))


def _digest(batch):
    data = {'items': batch['items'], 'feedback': batch['feedback']}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _mutable(batch):
    if batch['status'] in {'reviewing', 'proposal_pending'}:
        raise ValueError('本批正在分析或等待确认；先打开草案，或点击“补充样本”。')


def mark(candidate_id, message_id, mode, profile, card_message_id=0):
    if mode not in {'delete', 'keep', 'remove'}:
        raise ValueError('样本标记无效')
    with review.locked():
        candidate = review._candidate(candidate_id)
        if candidate.get('status') not in review.ACTIVE_STATUSES:
            raise ValueError('这类邮件已经处理，请重新选择')
        if message_id not in review._candidate_message_ids(candidate):
            raise ValueError('样本已变化，请重新打开')
        data = state()
        batch = current(data)
        _mutable(batch)
        item = next((x for x in batch['items'] if x['message_id'] == message_id), None)
        if mode == 'remove':
            if item:
                batch['items'].remove(item)
            batch['pending_replies'].pop(profile, None)
        else:
            if item is None:
                if len(batch['items']) >= MAX_SAMPLES:
                    raise ValueError('本批已选 20 个样本，请先提交这一批')
                item = {'candidate_id': candidate_id, 'message_id': message_id, 'reason': ''}
                batch['items'].append(item)
            if item.get('mode') != mode:
                item['reason'] = ''
            item['mode'] = mode
            batch['pending_replies'][profile] = {'message_id': message_id, 'card_id': card_message_id, 'at': time.time()}
        batch['revision'] += 1
        batch.pop('last_error', None)
        save(data)
    return '已移出本批。' if mode == 'remove' else ('已标记为应删除。' if mode == 'delete' else '已标记为必须保留。') + '直接发送原因即可。'


def bind_reason(profile, message_id, text):
    with review.locked():
        data = state()
        batch = current(data, create=False)
        if not batch or batch['status'] in TERMINAL:
            return None
        pending = batch['pending_replies'].get(profile)
        if not pending or time.time() - pending['at'] > 86400:
            return None
        # A reply to another feature must not become a Gmail sample reason.
        if message_id and pending.get('card_id') != message_id:
            # Bot replies contain the same batch-navigation buttons, but have
            # their own Telegram message ID; check their delivery receipt.
            from pylon.outbox import Outbox
            box = Outbox()
            with box.connect() as db:
                own = db.execute("SELECT 1 FROM events WHERE profile=? AND json_extract(payload,'$.text') LIKE '已标记为%' "
                                 "AND json_extract(receipt,'$[#-1].message_id')=?", (profile, message_id)).fetchone()
                if pending.get('question'):
                    own = db.execute("SELECT 1 FROM events WHERE profile=? AND json_extract(payload,'$.agent_task')=? AND json_extract(receipt,'$[#-1].message_id')=?",
                                     (profile, batch.get('task_id'), message_id)).fetchone()
            if not own:
                return None
        text = review._safe_text(text, review.MAX_FEEDBACK_BYTES)
        if not text:
            raise ValueError('原因不能为空')
        if pending.get('question'):
            batch['feedback'].append({'text': text, 'at_utc': review._now()})
        else:
            item = next((x for x in batch['items'] if x['message_id'] == pending['message_id']), None)
            if item is None:
                return None
            item['reason'] = text
        batch['pending_replies'].pop(profile, None)
        batch['revision'] += 1
        save(data)
    from pylon.commands import encode_response
    return encode_response('原因已记录。', {'inline_keyboard': [navigation(profile),
                           [button(profile, '整批交给 Grok', 'submit', batch['id'])]]})


def dashboard_payload(profile):
    with review.locked():
        data = state()
        batch = current(data)
        save(data)
        pending = [x for x in cleanup.load_candidate_state().get('candidates', {}).values()
                   if x.get('status') in review.ACTIVE_STATUSES]
        proposals = review._proposals()['proposals']
        drafts = [p for p in proposals.values() if p.get('status') in {'pending', 'previewed', 'stale'}]
    delete = sum(x['mode'] == 'delete' for x in batch['items'])
    keep = len(batch['items']) - delete
    missing = sum(not x.get('reason') for x in batch['items'])
    lines = ['Gmail 清理', f'待确认 {len(pending)} 类 · 本批 {len(batch["items"])}/20 个样本',
             f'应删除 {delete} · 必须保留 {keep}' + (f' · 待写原因 {missing}' if missing else ''),
             NAMES.get(batch['status'], batch['status'])]
    for index, item in enumerate(batch['items'], 1):
        candidate = cleanup.load_candidate_state().get('candidates', {}).get(item['candidate_id'], {})
        label = '删除' if item['mode'] == 'delete' else '保留'
        lines.append(f'{index}. {label}｜{candidate.get("subject_family", "样本")}｜' + (item.get('reason') or '待写原因')[:160])
    if batch.get('last_error'):
        lines.append('失败：' + str(batch['last_error'])[:500])
    if batch.get('question'):
        lines.append('Grok：' + batch['question'])
    keyboard = [[button(profile, '选择邮件样本', 'list', '0'), button(profile, '已生效规则', 'rules')]]
    if batch['status'] not in {'reviewing', 'proposal_pending'}:
        for index, item in enumerate(batch['items'], 1):
            candidate = cleanup.load_candidate_state()['candidates'].get(item['candidate_id'])
            ids = review._candidate_message_ids(candidate) if candidate else []
            if item['message_id'] in ids:
                keyboard.append([button(profile, f'修改样本 {index}' + (' · 待写原因' if not item.get('reason') else ''),
                                        'sample', f'{item["candidate_id"]}~{ids.index(item["message_id"])}')])
    if batch['items'] and batch['status'] not in {'reviewing', 'proposal_pending'}:
        keyboard.append([button(profile, '整批交给 Grok', 'submit', batch['id'])])
    for draft in sorted(drafts, key=lambda p: p.get('created_at_utc', ''), reverse=True)[:8]:
        keyboard.append([button(profile, draft.get('title') or '查看待确认草案', 'proposal', draft['id'])])
    if batch['status'] == 'reviewing':
        keyboard.append([button(profile, '刷新进度', 'home')])
    return {'text': '\n'.join(lines), 'reply_markup': {'inline_keyboard': keyboard}}


def candidates_payload(profile, page=0):
    items = [x for x in cleanup.load_candidate_state().get('candidates', {}).values()
             if x.get('status') in review.ACTIVE_STATUSES]
    items.sort(key=lambda x: (x.get('last_seen', ''), x['id']), reverse=True)
    page = max(0, min(page, max(0, (len(items) - 1) // 6)))
    rows = []
    lines = [f'待确认类型 · 第 {page + 1} 页']
    for index, item in enumerate(items[page * 6:page * 6 + 6], page * 6 + 1):
        lines.append(f'{index}. {item.get("subject_family") or "无主题"}\n{item.get("from") or "未知发件人"} · {item.get("sample_count", 0)} 封')
        rows.append([button(profile, f'{index}. {str(item.get("subject_family") or "无主题")[:40]}', 'candidate', item['id'])])
    pages = []
    if page:
        pages.append(button(profile, '上一页', 'list', str(page - 1)))
    if (page + 1) * 6 < len(items):
        pages.append(button(profile, '下一页', 'list', str(page + 1)))
    if pages:
        rows.append(pages)
    rows.append([button(profile, '本批样本', 'home')])
    return {'text': '\n\n'.join(lines) if items else '暂无待确认新类型。', 'reply_markup': {'inline_keyboard': rows}}


def sample_payload(profile, candidate_id, index=0, *, full=False):
    candidate = review._candidate(candidate_id)
    ids = review._candidate_message_ids(candidate)
    if not ids:
        return {'text': '这类邮件暂时没有可用样本。', 'reply_markup': {'inline_keyboard': [navigation(profile)]}}
    index = min(max(0, index), len(ids) - 1)
    mid = ids[index]
    target = f'{candidate_id}~{index}'
    metadata = next((x for x in candidate.get('samples', []) if x.get('id') == mid), candidate.get('sample') or {})
    if full:
        metadata = get_message_full(cleanup.build_gmail_service(), mid)
    body = metadata.get('body') if full else metadata.get('snippet')
    lines = [f'样本 {index + 1}/{len(ids)}', str(metadata.get('subject') or candidate.get('subject_family')),
             str(metadata.get('from') or candidate.get('from')), str(metadata.get('date') or ''), body or '（正文为空）']
    rows = [[button(profile, '应删除', 'delete', target), button(profile, '必须保留', 'keep', target)],
            [button(profile, '完整文本', 'full', target), button(profile, '移出本批', 'remove', target)]]
    pages = []
    if index:
        pages.append(button(profile, '上个样本', 'sample', f'{candidate_id}~{index - 1}'))
    if index + 1 < len(ids):
        pages.append(button(profile, '下个样本', 'sample', f'{candidate_id}~{index + 1}'))
    if pages:
        rows.append(pages)
    rows.append(navigation(profile))
    return {'text': '\n\n'.join(lines), 'reply_markup': {'inline_keyboard': rows}}


def examples(batch):
    service = cleanup.build_gmail_service()
    result = []
    for item in batch['items']:
        if not item.get('reason'):
            raise ValueError('请先为本批每个样本填写原因')
        full = get_message_full(service, item['message_id'])
        result.append({**full, 'message_id': item['message_id'], 'label': item['mode'], 'reason': item['reason']})
    return result


def submit(batch_id, profile):
    from pylon import agent_tasks
    with review.locked():
        data = state()
        batch = data['batches'].get(batch_id)
        if not batch or data['active'] != batch_id:
            raise ValueError('本批已经结束，请重新打开 /gmail')
        if batch['status'] in {'reviewing', 'proposal_pending'}:
            return '本批已经提交，请等待分析结果或打开草案。'
        if not 2 <= len(batch['items']) <= MAX_SAMPLES or {x['mode'] for x in batch['items']} != {'delete', 'keep'}:
            raise ValueError('本批至少选择一条应删除样本和一条必须保留样本')
        content = examples(batch)
        candidate = {'id': batch_id, 'from': '本批跨发件人样本', 'subject_family': '批量逻辑规则',
                     'sample_count': len(content), 'sample': {}}
        prompt = review._prompt(candidate, batch['feedback'])
        prompt += '\n这是一次整批审查，包含多个发件人和主题族。为本批所有应删除样本生成 1 至 20 个窄范围规则单元，所有必须保留样本都不能命中。每项 sample_validation.keep 必须列出本批全部保留样本。每个规则的 from 粗筛与逻辑 from 条件必须一致。不要按候选逐个提交。'
        request = 'cli:' + hashlib.sha256(f'gmail-batch:{batch_id}:{batch["revision"]}'.encode()).hexdigest()
        task_id = batch.get('task_id')
        active = False
        if task_id:
            rows = agent_tasks.invoke('status', task_id, text='')
            active = bool(rows and rows[0].get('status') in {'queued', 'running', 'waiting'})
        if active and batch.get('profile') != profile:
            raise ValueError('请回到发起本批分析的机器人继续提交')
        result = agent_tasks.invoke('gmail-reply' if active else 'gmail-run', task_id if active else 'grok', profile, request,
                                    text=json.dumps({'prompt': prompt, 'examples': content}, ensure_ascii=False))
        batch.update(task_id=result['id'], profile=profile, status='reviewing',
                     analysis_hash=_digest(batch), analysis_rules_hash=review._rule_digest(),
                     analysis_samples={x['message_id']: fingerprint(x) for x in content})
        batch.pop('last_error', None)
        batch.pop('question', None)
        batch.pop('consumed_task_status', None)
        save(data)
    return f'本批 {len(content)} 个样本已一起交给 Grok。完成后会收到草案按钮。'


def consume_agent_result(row):
    with review.locked():
        data = state()
        batch = next((b for b in data['batches'].values() if b.get('task_id') == row.get('id') and row.get('id')), None)
        if batch is None:
            return None
        if batch.get('consumed_task_status') == row.get('status'):
            return batch.get('result_info', {})
        result = row.get('result') or {}
        info = {'batch_id': batch['id'], 'status': row.get('status'), 'proposal_id': ''}
        if row.get('status') in {'queued', 'running'}:
            return info
        if row.get('status') == 'succeeded' and result.get('gmail_proposal'):
            try:
                if batch['analysis_hash'] != _digest(batch) or batch['analysis_rules_hash'] != review._rule_digest():
                    raise ValueError('本批样本或规则已变化，请重新提交')
                proposal = review.validate_proposal(result['gmail_proposal'], require_logic=True)
                evidence = review._validate_samples(proposal['operations'], examples(batch))
                if {x['message_id']: x['sha256'] for x in evidence} != batch['analysis_samples']:
                    raise ValueError('样本内容或标签已变化，请重新提交')
                pid = 'gmail-rp-' + uuid.uuid4().hex[:12]
                record = {**proposal, 'id': pid, 'batch_id': batch['id'], 'candidate_ids': _candidate_ids(batch),
                          'candidate_id': _candidate_ids(batch)[0], 'agent_task_id': row['id'], 'status': 'pending',
                          'base_rules_hash': review._rule_digest(), 'created_at_utc': review._now(),
                          'format_version': 3, 'sample_evidence': evidence, 'review_hash': _digest(batch)}
                proposals = review._proposals()
                proposals['proposals'][pid] = record
                review._save(review.PROPOSALS_PATH, proposals)
                batch['proposal_ids'].append(pid)
                batch['status'] = 'proposal_pending'
                info.update(status='proposal_pending', proposal_id=pid, summary=proposal['summary'])
            except Exception as exc:
                batch.update(status='error', last_error=str(exc)[:1200])
                info.update(status='error', error=batch['last_error'])
        elif row.get('status') == 'waiting':
            batch.update(status='waiting', question=str(result.get('question') or '')[:1200])
            batch['pending_replies'][batch['profile']] = {'question': True, 'at': time.time(), 'card_id': 0}
            info.update(status='waiting', question=batch['question'])
        else:
            batch.update(status='error', last_error=str(result.get('summary') or 'Grok 未返回规则草案')[:1200])
            info.update(status='error', error=batch['last_error'])
        batch['consumed_task_status'] = row.get('status')
        batch['result_info'] = info
        save(data)
        return info


def proposal_keyboard(profile, proposal):
    if proposal.get('status') == 'applied':
        rows = [[button(profile, '回滚这次修改', 'rollback', proposal['id'])]]
    elif proposal.get('status') in {'rejected', 'rolled_back'}:
        rows = []
    else:
        pid = proposal['id']
        rows = [[button(profile, '查看逻辑', 'logic', pid), button(profile, '预览影响', 'preview', pid)],
                [button(profile, '应用规则', 'apply', pid), button(profile, '补充样本', 'more', pid)],
                [button(profile, '拒绝草案', 'reject', pid)]]
    return {'inline_keyboard': rows + [[button(profile, '返回 Gmail', 'home')]]}


def callback(profile, payload, request_key=''):
    from pylon.inline import owns_callback
    if not owns_callback(profile, payload):
        raise ValueError('按钮不属于本人会话或原始机器人消息')
    parts = str(payload.get('data') or '').split('|')
    if len(parts) != 4 or parts[0] != PREFIX or not hmac.compare_digest(parts[3], review._signature(profile, PREFIX + ':' + parts[1], parts[2])):
        raise ValueError('按钮已失效，请重新打开 /gmail')
    _, action, target, _ = parts
    qid = profile + ':' + str(payload.get('id') or request_key)
    with review.locked():
        saved = state()['callbacks'].get(qid)
    if saved:
        return saved
    if action == 'home':
        result = dashboard_payload(profile)
    elif action == 'list':
        result = candidates_payload(profile, int(target))
    elif action in {'candidate', 'sample', 'full', 'delete', 'keep', 'remove'}:
        cid, _, index = target.partition('~')
        index = int(index or '0')
        candidate = review._candidate(cid)
        ids = review._candidate_message_ids(candidate)
        if index < 0 or index >= len(ids):
            raise ValueError('样本已变化，请重新打开 /gmail')
        if action in {'delete', 'keep', 'remove'}:
            text = mark(cid, ids[index], action, profile, payload['message']['message_id'])
            result = sample_payload(profile, cid, index)
            result['text'] = text
        else:
            result = sample_payload(profile, cid, index, full=action == 'full')
    elif action == 'submit':
        text = submit(target, profile)
        result = dashboard_payload(profile)
        result['text'] = text
    elif action == 'rules':
        result = review.rules_payload()
        proposals = review._proposals()['proposals']
        rows = [[button(profile, '查看最近修改：' + p.get('title', '')[:24], 'proposal', p['id'])]
                for p in sorted(proposals.values(), key=lambda p: p.get('applied_at_utc', ''), reverse=True)
                if p.get('status') == 'applied'][:5]
        result['reply_markup'] = {'inline_keyboard': rows + [[button(profile, '返回 Gmail', 'home')]]}
    elif action in {'proposal', 'logic', 'preview', 'apply', 'reject', 'rollback', 'more'}:
        _, proposal = review._load_proposal(target)
        if action == 'proposal':
            result = review.proposal_payload(profile, target)
        elif action == 'logic':
            result = {'text': '\n\n'.join(op['description'] + '\n' + op['reason'] + '\n' + json.dumps(op.get('logic'), ensure_ascii=False, indent=2)
                                        for op in proposal['operations'])}
        elif action == 'more':
            with review.locked():
                data = state()
                batch = data['batches'].get(proposal.get('batch_id'))
                if batch:
                    batch['status'] = 'draft'
                    batch['revision'] += 1
                    data['active'] = batch['id']
                    save(data)
                proposals = review._proposals()
                if proposals['proposals'][target]['status'] not in {'applied', 'rejected', 'rolled_back'}:
                    proposals['proposals'][target]['status'] = 'rejected'
                    review._save(review.PROPOSALS_PATH, proposals)
            result = dashboard_payload(profile)
        else:
            text = getattr(review, action)(target)
            with review.locked():
                data = state()
                batch = data['batches'].get(proposal.get('batch_id'))
                if batch and action in {'apply', 'reject', 'rollback'}:
                    batch['status'] = {'apply': 'applied', 'reject': 'draft', 'rollback': 'draft'}[action]
                    batch['revision'] += 1
                    save(data)
            result = {'text': text}
        if action != 'more':
            _, proposal = review._load_proposal(target)
            result['reply_markup'] = proposal_keyboard(profile, proposal)
    else:
        raise ValueError('按钮操作无效')
    # Raw email text is returned to the owner, never copied into review state.
    if action not in {'full', 'sample', 'candidate', 'home', 'list', 'rules', 'proposal', 'logic'}:
        with review.locked():
            data = state()
            data['callbacks'][qid] = result
            save(data)
    return result
