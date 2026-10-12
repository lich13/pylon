"""Native Codex/Grok updates and durable completion replies."""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import os
import uuid

from . import scheduler
from .config import profile_env, redact
from .outbox import Outbox
from .software_retirement import RETIRED_MESSAGE

NAMES = {'codex': 'Codex CLI', 'grok': 'Grok Build'}
STATES = {
    'queued': '正在检查', 'running': '正在检查或更新', 'updated': '更新成功',
    'current': '已是最新', 'busy': '暂未更新', 'failed': '更新失败',
    'uncertain': '结果待核对',
}


def _signature(profile: str, action: str, review_id: str) -> str:
    """SDK v1 compatibility: daily-review cards share this signing function."""
    secret = str(profile_env(profile).get('TELEGRAM_BOT_TOKEN') or 'pylon').encode()
    return hmac.new(secret, f'{profile}|{action}|{review_id}'.encode(), hashlib.sha256).hexdigest()[:12]


def result_text(row: dict) -> str:
    target = str(row.get('target') or '')
    if target not in NAMES:
        return RETIRED_MESSAGE
    status = row.get('status', 'uncertain')
    label = STATES.get(status, '结果待核对')
    if status == 'current' and row.get('latest_version') and row.get('after_version') != row['latest_version']:
        label = '无需更新'
    lines = [f'{NAMES[target]}：{label}']
    before, after = row.get('before_version'), row.get('after_version')
    if before and after and before != after:
        lines.append(f'版本：{before} → {after}')
    elif after:
        lines.append('当前版本：' + str(after))
    elif before:
        lines.append('更新前：' + str(before))
        lines.append('当前版本：待核对')
    if status in {'queued', 'running'}:
        lines.append('完成后会在这里回报；重复点击不会重复安装。')
    elif status not in {'updated', 'current'}:
        if row.get('detail'):
            lines.append(redact(str(row['detail']))[:600])
        if status == 'busy':
            lines.append('结束相关会话或等待当前更新完成后，再次点击即可。')
        elif status == 'failed':
            lines.append('请根据原因处理后，再次点击更新。')
        else:
            lines.append('需先核对实装版本；不会自动重复安装。')
    elif label == '无需更新':
        lines.append('当前版本高于 stable，已保留。')
    return '\n'.join(html.escape(str(line)) for line in lines)


def request_update(target, profile='webops'):
    if target not in NAMES or profile not in {'default', 'webops'}:
        raise ValueError('不支持的更新目标')
    key = os.environ.get('PYLON_COMMAND_ID') or 'cli:' + uuid.uuid4().hex
    row = json.loads(scheduler.admin('software', 'update', target, profile, key))
    return result_text(row)


def inventory_payload(profile='default', *, full=False):
    """Old commands and SDK consumers cannot start an inventory scan."""
    return {'text': RETIRED_MESSAGE, 'reply_markup': None}


def inventory_text(full=False):
    return RETIRED_MESSAGE


def pending_keyboard(profile='default'):
    return None


def publish_candidates(snapshot, box=None):
    """Pinned older business versions must not publish retired APT cards."""
    return 0


def callback(profile, payload, request_key=''):
    from .commands import authorized
    message = payload.get('message') or {}
    if not authorized(profile, {'chat': message.get('chat', {}), 'from': payload.get('from', {})}):
        raise ValueError('只允许本人私聊操作')
    return {'text': RETIRED_MESSAGE, 'callback_id': str(payload.get('id') or '')}


def reconcile(box=None):
    box = box or Outbox()
    rows = json.loads(scheduler.admin('software', 'results'))
    count = 0
    for row in rows:
        if row.get('target') not in NAMES:
            continue
        # Persist before acknowledging root. Retried delivery never installs.
        box.enqueue(profile=row['profile'], job_id='software-update', kind='text',
                    payload={'text': result_text(row)}, key='software-update:' + row['id'])
        scheduler.admin('software', 'ack', row['request_id'])
        count += 1
    return count


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
