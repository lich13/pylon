"""Owner-facing agent commands and durable result delivery."""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import uuid

from . import scheduler
from .config import profile_env, redact
from .outbox import Outbox

NAMES = {'codex': 'Codex', 'grok': 'Grok'}
STATES = {'queued': '排队中', 'running': '执行中', 'waiting': '等待回答', 'succeeded': '已完成并通过独立复核',
          'failed': '执行失败', 'uncertain': '结果待核验', 'interrupted': '执行中断', 'cancelled': '已取消', 'timed_out': '已超时'}
TERMINAL = set(STATES) - {'queued', 'running', 'waiting'}


def invoke(action, *args, text=''):
    result = subprocess.run(['sudo', '-n', scheduler.ADMIN, 'agent', action, *args], input=text,
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise scheduler.AdminError(result)
    return json.loads(result.stdout)


def render(row, *, details=True):
    lines = [f"Agent #{row['id'][:12]} · {NAMES[row['provider']]}：{STATES[row['status']]}"]
    if row.get('settings', {}).get('model'):
        lines.append(f"模型：{row['settings']['model']}；已执行 {row['turns']} 轮，累计 {round(row['runtime'])} 秒")
    if details:
        result = row.get('result', {})
        if result.get('summary'):
            lines.append(result['summary'])
        for field, title in [('changes', '修改'), ('verification', 'Agent 验证'), ('independent_checks', '独立复核'), ('limitations', '未完成或限制')]:
            if result.get(field):
                lines.append(title + '：\n' + '\n'.join('• ' + str(x) for x in result[field]))
        if row['status'] == 'waiting':
            lines.append(result.get('question', ''))
            lines.append(f"直接回复这条消息，或 /agent_reply {row['id'][:12]} 回答内容")
        elif row['status'] in {'queued', 'running'}:
            lines.append(row.get('progress') or '完成后自动回报；可补充要求或取消任务。')
        if row['status'] in TERMINAL:
            lines.append('线程清理：' + {'pending': '待执行', 'done': '已清理', 'failed': '失败，正在重试'}[row['cleanup']])
            if row.get('cleanup_error'):
                lines.append(row['cleanup_error'])
    if row.get('launch_error'):
        lines.append('启动暂时失败，任务已保存并等待重试：' + row['launch_error'])
    return redact('\n'.join(lines))


def command(action, argument='', profile='webops'):
    if profile not in {'default', 'webops'}:
        raise ValueError('invalid agent profile')
    key = os.environ.get('PYLON_COMMAND_ID') or 'cli:' + uuid.uuid4().hex
    task_id = ''
    if action in NAMES:
        row = invoke('run', action, profile, key, text=argument)
        task_id, text = row['id'], render(row)
    elif action == 'reply':
        parts = argument.split(maxsplit=1)
        if len(parts) != 2:
            from .command_catalog import help_text
            return help_text('agent_reply')
        row = invoke('reply', parts[0], profile, key, text=parts[1])
        task_id = row['id']
        text = f"Agent #{task_id[:12]} · {NAMES[row['provider']]}：补充内容已保存，将继续同一会话。"
    elif action == 'cancel':
        row = invoke('cancel', argument, profile, key)
        task_id = row['id']
        text = render(row) if row['status'] in TERMINAL else f'Agent #{task_id[:12]}：已请求停止，稍后回报执行与清理结果。'
    elif action in {'status', 'logs'}:
        rows = invoke(action, *([argument] if argument else []))
        text = '\n\n'.join(render(r, details=bool(argument)) for r in rows) or '暂无 Agent 任务。'
        task_id = rows[0]['id'] if len(rows) == 1 else ''
    else:
        raise ValueError('unsupported agent command')
    if task_id and key.startswith('telegram:'):
        Outbox().put('agent-command:' + key, task_id)
    return text


def reply_to(profile, message_id, text, box=None):
    """Only actual delivery receipts in this bot's owner chat bind replies."""
    box = box or Outbox()
    owner = str(profile_env(profile)['TELEGRAM_HOME_CHANNEL'])
    with box.connect() as db:
        rows = db.execute("SELECT payload,receipt FROM events WHERE profile=? AND receipt IS NOT NULL AND json_extract(payload,'$.agent_task') IS NOT NULL", (profile,)).fetchall()
    for row in rows:
        receipts = json.loads(row['receipt'])
        if isinstance(receipts, dict):
            receipts = [receipts]
        if any(r.get('message_id') == message_id and str(r.get('chat_id')) == owner for r in receipts or []):
            return command('reply', json.loads(row['payload'])['agent_task'] + ' ' + text, profile)
    return None


def scrub_inputs(box, task_ids):
    if not task_ids:
        return
    with box.connect() as db:
        db.execute('PRAGMA secure_delete=ON')
        for row in db.execute("SELECT key,value FROM kv WHERE key LIKE 'agent-command:telegram:%'").fetchall():
            if json.loads(row['value']) not in task_ids:
                continue
            match = re.fullmatch(r'agent-command:telegram:(default|webops):(\d+)', row['key'])
            if match:
                changed = db.execute("UPDATE updates SET payload='{}' WHERE profile=? AND update_id=? AND status NOT IN ('pending','running')", match.groups()).rowcount
                if changed:
                    db.execute('DELETE FROM kv WHERE key=?', (row['key'],))
        for task_id in task_ids:
            db.execute("UPDATE events SET payload=json_set(payload,'$.text','本任务对话已清理'),status=CASE WHEN status='pending' THEN 'superseded' ELSE status END WHERE json_extract(payload,'$.agent_task')=? AND json_extract(payload,'$.agent_state') IN ('queued','waiting') AND status!='sending'", (task_id,))


def reconcile(box=None):
    box = box or Outbox()
    rows = invoke('results')
    for event in rows:
        row = json.loads(event['payload'])
        if row['cleanup'] == 'done':
            text = f"Agent #{row['id'][:12]}：本次线程、原始对话及临时文件已清理。"
        elif row['cleanup'] == 'failed':
            text = f"Agent #{row['id'][:12]}：线程清理未完成，正在重试。\n" + row['cleanup_error']
        else:
            text = render(row)
        box.enqueue(profile=event['profile'], job_id='agent', kind='text',
                    payload={'text': text, 'agent_task': row['id'], 'agent_state': row['status']},
                    key='agent-event:' + event['id'])
        if row['cleanup'] == 'done':
            scrub_inputs(box, {row['id']})
        # An ack means a durable notification exists, not that Telegram
        # necessarily received it. Delivery remains the outbox's responsibility.
        invoke('ack', event['id'])
    cleaned = invoke('cleaned')
    scrub_inputs(box, set(cleaned['ids']))
    with box.connect() as db:
        db.execute("DELETE FROM events WHERE json_extract(payload,'$.agent_task') IS NOT NULL AND status IN ('delivered','superseded') AND updated_at<?",
                   (time.time() - cleaned['retention_days'] * 86400,))
    return len(rows)


def synchronize_background(stop):
    delay = 15
    while not stop.is_set():
        try:
            reconcile()
            delay = 15
        except Exception as exc:
            logging.getLogger('pylon').warning('agent results: %s', redact(str(exc)))
            delay = min(300, delay * 2)
        stop.wait(delay)
