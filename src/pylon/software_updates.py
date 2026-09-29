"""Unprivileged command handling and durable update-result delivery."""
from __future__ import annotations

import json
import logging
import os
import uuid

from . import scheduler
from .config import redact
from .outbox import Outbox

NAMES = {'codex': 'Codex CLI', 'grok': 'Grok Build'}
STATES = {'queued': '已提交', 'running': '更新中', 'updated': '更新成功', 'current': '无需更新',
          'busy': '占用，未更新', 'failed': '更新失败', 'uncertain': '结果待核对'}


def result_text(row):
    lines = [f"{NAMES[row['target']]}：{STATES[row['status']]}"]
    if row.get('before_version') or row.get('after_version'):
        lines.append(f"版本：{row.get('before_version') or '未知'} → {row.get('after_version') or '未知'}")
    if row.get('latest_version'):
        lines.append('检查到的 stable 版本：' + row['latest_version'])
    if row.get('finished_at'):
        lines.append(f"耗时：{max(0, round(row['finished_at'] - row['created_at']))} 秒")
    if row.get('detail'):
        lines.append(redact(row['detail']))
    if row['status'] in {'failed', 'uncertain'}:
        lines.append('不会自动重复安装；请核对版本和错误后再操作。')
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


def inventory_text(full=False):
    from .software_inventory import check, render
    text = render(check(), full=full)
    rows = json.loads(scheduler.admin('software', 'status'))
    if rows:
        text += '\n\n最近更新：\n' + '\n\n'.join(result_text(row) for row in rows[:3])
    return text


def reconcile(box=None):
    box = box or Outbox()
    rows = json.loads(scheduler.admin('software', 'results'))
    for row in rows:
        # Root holds execution results. A restart between enqueue and ack
        # repeats this read, but the persistent event key prevents duplicates.
        box.enqueue(profile=row['profile'], job_id='software-update', kind='text',
                    payload={'text': result_text(row)}, key='software-update:' + row['id'])
        box.put('software_inventory', None)
        scheduler.admin('software', 'ack', row['request_id'])
    return len(rows)


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
