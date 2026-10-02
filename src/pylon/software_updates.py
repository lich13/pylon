"""Software inventory, guarded updates and Telegram review cards."""
from __future__ import annotations

import hashlib
import html
import hmac
import json
import logging
import os
import re
import time
import uuid

from . import scheduler
from .config import profile_env, redact
from .outbox import Outbox
from .software_policy import apt_policy

NAMES = {'codex': 'Codex CLI', 'grok': 'Grok Build'}
STATES = {
    'queued': '已提交', 'running': '更新中', 'updated': '更新成功',
    'current': '无需更新', 'busy': '占用，未更新', 'failed': '更新失败',
    'uncertain': '结果待核对', 'deferred': '系统暂缓', 'stale': '候选已变化',
    'skipped': '已跳过', 'pending': '待确认',
}
REVIEW_PREFIX = 'su1'
REVIEW_ID_RE = re.compile(r'sur-[0-9a-f]{20}')
REVIEW_REQUEST_PREFIX = 'software-review:'


def _review_id(package: str, installed: str, candidate: str, source: str) -> tuple[str, str]:
    fingerprint = hashlib.sha256(json.dumps(
        {'package': package, 'installed': installed, 'candidate': candidate, 'source': source},
        ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return 'sur-' + fingerprint[:20], fingerprint


def eligible_candidates(snapshot: dict) -> list[dict]:
    """Return only the exact root-approved APT candidates."""
    rows = []
    apt = snapshot.get('apt') if isinstance(snapshot, dict) else None
    for package in (apt or {}).get('packages') or []:
        if not isinstance(package, dict):
            continue
        name = str(package.get('name') or '')
        policy = apt_policy(name)
        installed = str(package.get('installed') or '')
        candidate = str(package.get('candidate') or '')
        if not policy or not installed or not candidate or installed == candidate:
            continue
        review_id, fingerprint = _review_id(name, installed, candidate, 'Ubuntu APT')
        rows.append({
            'id': review_id,
            'target': 'apt:' + name,
            'package_name': name,
            'installed_version': installed,
            'candidate_version': candidate,
            'source': 'Ubuntu APT',
            'risk': policy['risk'],
            'fingerprint': fingerprint,
        })
    return rows


def _state_row(box: Outbox, candidate: dict) -> dict:
    row = box.software_review(candidate['id'])
    if row:
        return row
    return box.upsert_software_review(candidate | {
        'profile': 'default', 'status': 'pending', 'created_at': time.time(),
    })


def _signature(profile: str, action: str, review_id: str) -> str:
    secret = str(profile_env(profile).get('TELEGRAM_BOT_TOKEN') or 'pylon').encode()
    return hmac.new(secret, f'{profile}|{action}|{review_id}'.encode(), hashlib.sha256).hexdigest()[:12]


def callback_data(profile: str, action: str, review_id: str) -> str:
    return f'{REVIEW_PREFIX}|{action}|{review_id}|{_signature(profile, action, review_id)}'


def _parse_callback(profile: str, value: str) -> tuple[str, str]:
    parts = str(value or '').split('|')
    if len(parts) != 4 or parts[0] != REVIEW_PREFIX:
        raise ValueError('按钮已失效，请重新使用 /updates')
    action, review_id, signature = parts[1], parts[2], parts[3]
    if action not in {'approve', 'skip'} or not REVIEW_ID_RE.fullmatch(review_id):
        raise ValueError('按钮参数无效，请重新使用 /updates')
    if not hmac.compare_digest(signature, _signature(profile, action, review_id)):
        raise ValueError('按钮签名无效，请重新使用 /updates')
    return action, review_id


def _authorized_callback(profile: str, payload: dict) -> bool:
    message = payload.get('message') or {}
    sender = payload.get('from') or {}
    owner = str(profile_env(profile).get('TELEGRAM_HOME_CHANNEL') or '')
    chat = message.get('chat') or {}
    return bool(owner and chat.get('type') == 'private' and str(chat.get('id')) == owner
                and str(sender.get('id')) == owner and not sender.get('is_bot'))


def _keyboard(profile: str, review_id: str) -> dict:
    return {'inline_keyboard': [[
        {'text': '更新此包', 'callback_data': callback_data(profile, 'approve', review_id)},
        {'text': '跳过此版本', 'callback_data': callback_data(profile, 'skip', review_id)},
    ]]}


def review_text(row: dict) -> str:
    status = STATES.get(str(row.get('status') or 'pending'), str(row.get('status') or 'pending'))
    package = html.escape(str(row.get('package_name') or row.get('target') or '未知'))
    installed = html.escape(str(row.get('installed_version') or '未知'))
    candidate = html.escape(str(row.get('candidate_version') or '未知'))
    source = html.escape(str(row.get('source') or ''))
    risk = html.escape(str(row.get('risk') or ''))
    lines = [f'系统小包更新：{package}', f'状态：{status}', f'版本：{installed} → {candidate}', f'来源：{source}']
    if risk:
        lines.append('风险：' + risk)
    lines.append('只处理这个包；不执行 autoremove，不自动重启主机。')
    if row.get('detail'):
        lines.append('结果：' + html.escape(redact(str(row['detail']))[-1200:]))
    return '\n'.join(lines)


def result_text(row: dict) -> str:
    target = str(row.get('target') or '')
    if target.startswith('apt:'):
        package = target.removeprefix('apt:')
        name = '系统包 ' + package
    else:
        name = NAMES.get(target, target)
    status = STATES.get(row.get('status'), row.get('status', '未知'))
    lines = [f'{name}：{status}']
    if row.get('before_version') or row.get('after_version'):
        lines.append(f"版本：{row.get('before_version') or '未知'} → {row.get('after_version') or '未知'}")
    if row.get('latest_version'):
        lines.append('检查到的候选版本：' + str(row['latest_version']))
    if row.get('finished_at'):
        lines.append(f"耗时：{max(0, round(row['finished_at'] - row['created_at']))} 秒")
    if row.get('detail'):
        lines.append(redact(row['detail']))
    if row.get('status') in {'failed', 'uncertain'}:
        lines.append('不会自动重复安装；请核对版本和错误后再操作。')
    if row.get('status') == 'deferred':
        lines.append('系统仍按 phased updates 策略暂缓，本次没有安装。')
    return '\n'.join(lines)


def request_update(target, profile='webops'):
    if target not in NAMES or profile not in {'default', 'webops'}:
        raise ValueError('不支持的更新目标')
    key = os.environ.get('PYLON_COMMAND_ID') or 'cli:' + uuid.uuid4().hex
    row = json.loads(scheduler.admin('software', 'update', target, profile, key))
    if row['status'] in {'queued', 'running'}:
        return (f"{NAMES[target]}：{STATES[row['status']]}\n执行 sudo {target} update"
                '\n仅使用 stable 版本；发现会话占用时不执行。完成后自动回报结果。')
    return result_text(row)


def request_package_update(review_id: str, profile: str = 'default', request_key: str = '') -> str:
    if profile != 'default':
        raise ValueError('系统小包确认只允许默认本人会话')
    box = Outbox()
    row = box.software_review(review_id)
    if not row:
        raise ValueError('更新候选不存在，或已过期')
    if row['status'] in {'queued', 'running', 'updated', 'current', 'failed', 'uncertain',
                         'deferred', 'stale', 'skipped'}:
        return review_text(row)
    request_id = row.get('request_id') or REVIEW_REQUEST_PREFIX + review_id
    result = json.loads(scheduler.admin(
        'software', 'apt-update', row['package_name'], profile, request_id,
        row['installed_version'], row['candidate_version']))
    status = str(result.get('status') or 'uncertain')
    changes = {'status': status, 'request_id': request_id}
    if result.get('detail'):
        changes['detail'] = str(result['detail'])[-2000:]
    row = box.update_software_review(review_id, **changes)
    if status in {'queued', 'running'}:
        return '已提交系统包更新，完成后自动回报。\n\n' + review_text(row)
    return review_text(row)


def _current_snapshot(full: bool = False):
    from .software_inventory import check, render
    snapshot = check(max_age=0)
    return snapshot, render(snapshot, full=full)


def inventory_payload(profile: str = 'default', *, full: bool = False) -> dict:
    snapshot, text = _current_snapshot(full)
    box = Outbox()
    candidates = [_state_row(box, candidate) for candidate in eligible_candidates(snapshot)]
    if candidates:
        text += '\n\n可点选系统小包：\n' + '\n'.join(
            f"- {row['package_name']}：{row['installed_version']} → {row['candidate_version']}｜"
            f"{STATES.get(row['status'], row['status'])}" for row in candidates
        )
    keyboard = None
    if profile == 'default' and candidates:
        candidates = [row for row in candidates if row['status'] in {'pending', 'queued', 'running'}]
    if profile == 'default' and candidates:
        keyboard = {'inline_keyboard': [
            [{'text': f"更新 {row['package_name']}", 'callback_data': callback_data(profile, 'approve', row['id'])},
             {'text': '跳过此版本', 'callback_data': callback_data(profile, 'skip', row['id'])}]
            for row in candidates
        ]}
    rows = json.loads(scheduler.admin('software', 'status'))
    if rows:
        text += '\n\n最近更新：\n' + '\n\n'.join(result_text(row) for row in rows[:3])
    return {'text': text, 'reply_markup': keyboard}


def inventory_text(full=False):
    return inventory_payload('webops', full=full)['text']


def pending_keyboard(profile: str = 'default') -> dict | None:
    if profile != 'default':
        return None
    rows = Outbox().software_reviews(profile=profile, statuses=('pending', 'queued', 'running'))
    if not rows:
        return None
    return {'inline_keyboard': [
        [{'text': f"更新 {row['package_name']}", 'callback_data': callback_data(profile, 'approve', row['id'])},
         {'text': '跳过此版本', 'callback_data': callback_data(profile, 'skip', row['id'])}]
        for row in rows[:20]
    ]}


def publish_candidates(snapshot: dict, box: Outbox | None = None) -> int:
    """Queue one durable card for each new allowed candidate."""
    box = box or Outbox()
    published = 0
    for candidate in eligible_candidates(snapshot):
        row = _state_row(box, candidate)
        if row.get('event_id') or row.get('status') == 'skipped':
            continue
        event_id = box.enqueue(
            profile='default', job_id='software-update-review', kind='text',
            payload={'text': review_text(row), 'reply_markup': _keyboard('default', row['id'])},
            key='software-review:' + row['id'])
        box.update_software_review(row['id'], event_id=event_id, status='pending')
        published += 1
    return published


def callback(profile: str, payload: dict, request_key: str = '') -> dict:
    if profile != 'default' or not _authorized_callback(profile, payload):
        raise ValueError('只允许本人默认机器人私聊操作')
    action, review_id = _parse_callback(profile, payload.get('data', ''))
    box = Outbox()
    row = box.software_review(review_id)
    if not row:
        raise ValueError('更新候选不存在，或已过期')
    if action == 'skip':
        if row['status'] != 'pending':
            return {'text': review_text(row), 'callback_id': str(payload.get('id') or '')}
        row = box.update_software_review(review_id, status='skipped', detail='用户跳过当前版本')
        return {'text': '已跳过此版本；候选版本变化后会重新提示。', 'callback_id': str(payload.get('id') or '')}
    text = request_package_update(review_id, profile, request_key)
    row = box.software_review(review_id) or row
    markup = _keyboard(profile, review_id) if row['status'] in {'queued', 'running', 'pending'} else None
    return {'text': text, 'reply_markup': markup, 'callback_id': str(payload.get('id') or '')}


def _sync_review_result(box: Outbox, row: dict) -> bool:
    request_id = str(row.get('request_id') or '')
    if not request_id.startswith(REVIEW_REQUEST_PREFIX):
        return False
    review_id = request_id.removeprefix(REVIEW_REQUEST_PREFIX)
    if not REVIEW_ID_RE.fullmatch(review_id) or not box.software_review(review_id):
        return False
    status = str(row.get('status') or 'uncertain')
    detail = str(row.get('detail') or '')[-2000:]
    box.update_software_review(review_id, status=status, request_id=request_id, detail=detail)
    box.enqueue(profile='default', job_id='software-update', kind='text',
                payload={'text': result_text(row)}, key='software-update-result:' + str(row['id']))
    return True


def reconcile(box=None):
    from .software_inventory import invalidate
    box = box or Outbox()
    rows = json.loads(scheduler.admin('software', 'results'))
    for row in rows:
        if _sync_review_result(box, row):
            invalidate(box)
        else:
            # Root holds execution results. A restart between enqueue and ack
            # repeats this read, but the persistent event key prevents duplicates.
            box.enqueue(profile=row['profile'], job_id='software-update', kind='text',
                        payload={'text': result_text(row)}, key='software-update:' + row['id'])
            invalidate(box)
        scheduler.admin('software', 'ack', row['request_id'])
    return len(rows)


def review_status_text(box: Outbox | None = None) -> str:
    box = box or Outbox()
    rows = box.software_reviews(profile='default')
    counts = {}
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1
    pending = counts.get('pending', 0)
    active = counts.get('queued', 0) + counts.get('running', 0)
    recent = next((row for row in rows if row['status'] in {'updated', 'current', 'failed', 'uncertain', 'deferred'}), None)
    line = f'系统小包更新：待确认 {pending}；执行中 {active}'
    labels = [('skipped', '已跳过'), ('updated', '成功'), ('current', '无需更新'),
              ('failed', '失败'), ('uncertain', '待核对'), ('deferred', '系统暂缓')]
    details = [f'{label} {counts[key]}' for key, label in labels if counts.get(key)]
    if details:
        line += '；' + '、'.join(details)
    if recent:
        line += f"；最近 {recent['package_name']} {STATES.get(recent['status'], recent['status'])}"
    return line


def review_logs_text(box: Outbox | None = None) -> str:
    box = box or Outbox()
    rows = box.software_reviews(profile='default')[:20]
    if not rows:
        return '系统小包更新记录：暂无候选。'
    lines = ['系统小包更新记录：']
    for row in rows:
        status = STATES.get(row['status'], row['status'])
        detail = ('：' + redact(str(row['detail']))[-300:]) if row.get('detail') else ''
        lines.append(f"{row['id']} {row['package_name']} {status}{detail}")
    return '\n'.join(lines)


def synchronize_background(stop):
    log = logging.getLogger('pylon')
    delay = 15
    while not stop.is_set():
        try:
            reconcile()
            delay = 15
        except Exception as exc:
            log.warning('software update results: %s', redact(str(exc)))
            delay = min(300, delay * 2)
        stop.wait(delay)
