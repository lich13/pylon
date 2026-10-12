from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from .config import home, jobs, profile_env, redact
from .command_catalog import COMMANDS, RETIRED, RETIRED_MESSAGE, help_text
from . import scheduler, extensions
from .outbox import Outbox
from .runner import task_environment


RESPONSE_PREFIX = "PYLON_REPLY_JSON:"


def encode_response(text: str, reply_markup: dict | None = None) -> str:
    if not reply_markup:
        return text
    return RESPONSE_PREFIX + json.dumps({"text": text, "reply_markup": reply_markup}, ensure_ascii=False, separators=(",", ":"))


def authorized(profile: str, message: dict) -> bool:
    owner = str(profile_env(profile).get("TELEGRAM_HOME_CHANNEL", ""))
    return bool(owner and message.get("chat", {}).get("type") == "private"
                and str(message.get("chat", {}).get("id")) == owner
                and str(message.get("from", {}).get("id")) == owner
                and not message.get("from", {}).get("is_bot"))


def resolve_job(value: str) -> str:
    catalog = jobs()
    if value in catalog:
        return value
    if value.isdigit() and 1 <= int(value) <= len(catalog):
        return list(catalog)[int(value) - 1]
    matches = [key for key, job in catalog.items() if value.casefold() == job['name'].casefold()]
    if len(matches) == 1:
        return matches[0]
    raise ValueError("任务不存在；发送 /status 查看编号")


def control_task(job_id: str, paused: bool) -> str:
    if job_id == 'all':
        for key in jobs():
            control_task(key, paused)
        return '全部定时任务已' + ('暂停' if paused else '恢复')
    if job_id not in jobs():
        raise ValueError("任务不存在")
    scheduler.suspend(job_id, paused)
    return f"{jobs()[job_id]['name']}已{'暂停' if paused else '恢复'}"


def status_text() -> str:
    box = Outbox()
    lines = []
    try:
        timers = scheduler.states()
    except Exception as exc:
        timers = {}
        lines.append('调度状态查询失败：' + redact(str(exc))[:200])
    names = {"ok": "正常", "no_change": "无变化", "running": "运行中", "failed": "业务失败",
             "delivery_pending": "待补发", "interrupted": "执行中断"}
    with box.connect() as db:
        for index, (key, job) in enumerate(jobs().items(), 1):
            row = db.execute("SELECT status,id FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1", (key,)).fetchone()
            state = names.get(row[0], row[0]) if row else "未执行"
            if row and row[0] == 'delivery_pending' and all(e['status'] == 'delivered' for e in box.run_events(row[1])):
                state = '正常'
            timer = timers.get(key, {})
            if timer.get('running'):
                state = '运行中'
            if timer.get('paused'):
                state = "已暂停 / " + state
            lines.append(f"{index}. {job['name']}：{state}")
            if timer.get('next') and not timer.get('paused'):
                lines.append('   下次 ' + timer['next'])
    counts = box.counts()
    unresolved = sum(counts.get(s, 0) for s in ('pending', 'sending', 'uncertain', 'failed'))
    lines.append(f"待处理通知 {unresolved}；回执不确定 {counts.get('uncertain', 0)}")
    for mod in extensions.snapshot().values():
        if 'status' in mod.manifest.get('hooks', []):
            try:
                value = extensions.invoke(mod.id, 'status', mod.manifest.get('status_profile', 'webops'))
                if value: lines.append(value)
            except Exception as exc:
                lines.append(mod.id + ' 状态暂不可读：' + redact(str(exc))[:160])
    return "\n".join(lines)


def locked_command(lock_name: str, args: list[str], profile: str) -> str:
    lockdir = home() / "run"
    lockdir.mkdir(parents=True, exist_ok=True)
    with (lockdir / f"{lock_name}.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "相关任务正在运行，请稍后重试"
        result = subprocess.run(args, env=task_environment(profile), capture_output=True, text=True, timeout=120)
        if result.returncode:
            return "操作失败：" + redact(result.stdout or result.stderr)[-1200:]
        return result.stdout.strip()




def outbox_text() -> str:
    box = Outbox()
    names = {'pending': '待发送', 'sending': '发送中', 'failed': '发送失败', 'uncertain': '回执不确定'}
    counts = box.counts()
    lines = ['通知队列', *[f'{label}：{counts.get(status, 0)}' for status, label in names.items()]]
    with box.connect() as db:
        rows = db.execute("SELECT id,job_id,status,attempts,error FROM events WHERE status NOT IN ('delivered','suppressed') ORDER BY created_at DESC LIMIT 10").fetchall()
    catalog = jobs()
    for row in rows:
        job = catalog.get(row['job_id'], {}).get('name', '机器人回复' if row['job_id'] == 'bot' else row['job_id'])
        lines.append(f"#{row['id']} {job}：{names.get(row['status'], row['status'])}，已尝试 {row['attempts']} 次")
        if row['error']:
            lines.append('  ' + redact(row['error'])[:180])
    if counts.get('uncertain'):
        lines.append('回执不确定的通知不会自动重发。')
    return '\n'.join(lines)


def ssh_text(action: str, address: str = '') -> str:
    if action in {'sync', 'refresh'}:
        return scheduler.start('ssh-admin-sync' if action == 'sync' else 'ssh-beijing-refresh')
    if action == 'check':
        try:
            address = str(ipaddress.IPv4Address(address))
        except ipaddress.AddressValueError:
            return '请输入有效的 IPv4 地址。\n\n' + help_text('ssh_check')
    data = scheduler.ssh(action, address or None)
    if action == 'check':
        return (f"IP：{data['address']}\n北京网段命中：{'是' if data['beijing_set'] else '否'}\n"
                f"fail2ban 豁免：{'是' if data['fail2ban_exempt'] else '否'}")
    return (f"北京网段：{data['cidrs']} 条\n内核生效：{data['live_entries']} 条\n"
            f"可信登录 IP：{'、'.join(data['trusted_ips']) or '近期无记录'}\n"
            f"待恢复事务：{'有' if data['transaction_pending'] else '无'}")


def chiphell_text(action: str, argument: str = '') -> str:
    failure = None
    try:
        output = scheduler.admin('chiphell', action, timeout=150)
    except scheduler.AdminError as exc:
        failure, output = exc, exc.stdout
    if 'POLLER_LOCKED' in output:
        return 'Chiphell 抓取正在运行，本次未执行测试，请稍后重试。'
    if action == 'status':
        data = next((json.loads(line) for line in output.splitlines() if line.startswith('{')), None)
        if not isinstance(data, dict) or not isinstance(data.get('poller_active'), bool):
            raise failure or ValueError('Chiphell 状态返回格式异常')
        # The compatibility CLI returns 1 for a stopped poller.
        if failure and not (failure.returncode == 1 and data['poller_active'] is False):
            raise failure
        paused = scheduler.paused(scheduler.CHIPHELL)
        lines = [f"投递排程：{'已暂停' if paused else '运行中'}",
                 f"抓取服务：{'运行中' if data['poller_active'] else '已停止'}", f"待发通知：{data['pending']}"]
        health = data.get('health') or {}
        status = health.get('status', 'UNKNOWN')
        health_names = {'OK': '正常', 'UNKNOWN': '暂无结果', 'COOKIE_EXPIRED': 'Cookie 已失效',
                        'LOGIN_REQUIRED': '需要登录', 'PROXY_UNAVAILABLE': '代理不可用', 'UPSTREAM_UNAVAILABLE': '网站不可用',
                        'ACCESS_DENIED': '访问受限', 'PARSE_FAILED': '页面解析失败'}
        lines.append('最近健康结果：' + health_names.get(status, f'异常（{status}）'))
        if health.get('last_poll_at'):
            lines.append('检查时间：' + datetime.fromtimestamp(health['last_poll_at'], ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S'))
        return '\n'.join(lines)
    if failure:
        return 'Chiphell 操作失败：' + redact(str(failure))[-1200:]
    if action == 'test':
        if not re.search(r'\bPOLL_OK\b', output):
            return 'Chiphell 测试结果未确认，请用 /chiphell_status 核对。\n' + redact(output)[-1000:]
        return 'Chiphell 测试通过：实际页面抓取成功，登录与代理可用。'
    return 'Chiphell 抓取和定时投递已' + ('暂停' if action == 'pause' else '恢复')




def handle(profile: str, text: str) -> str:
    os.environ['PYLON_COMMAND_PROFILE'] = profile
    value = text.strip()
    reply_id = os.environ.get('PYLON_REPLY_MESSAGE_ID', '')
    if not value.startswith('/'):
        result = extensions.hook('text', profile, reply_id=int(reply_id) if reply_id.isdigit() else 0, text=value)
        if result is not None: return result
        from .agent_tasks import reply_to
        result = reply_to(profile, int(reply_id), value) if reply_id.isdigit() else None
        if result is not None:
            return result
    if value == '任务状态':
        return RETIRED_MESSAGE
    if value.startswith('/'):
        token, *rest = value.split(maxsplit=1)
        name, argument = token[1:].split('@', 1)[0].lower(), rest[0].strip() if rest else ''
        extensions.hook('before_command', profile, name=name)
        if name == 'start':
            result = extensions.hook('start', profile, argument=argument)
            return result if result is not None else help_text(profile=profile)
        if name.startswith('gmail_') or (name == 'gmail' and argument):
            return 'Gmail 操作已合并，请使用 /gmail'
        if name in {'ssh', 'chiphell'}:
            parts = argument.split(maxsplit=1)
            action = parts[0] if parts else 'status'
            argument = parts[1] if len(parts) > 1 else ''
            name = name + '_' + action
        if name in {'chiphell_categories', 'chiphell_silence', 'chiphell_unsilence'}:
            return 'CHH 分类管理入口已移除，推送偏好在维护时统一调整。'
        if name == 'updates':
            from .software_retirement import RETIRED_MESSAGE as software_retired
            return software_retired
        if name in RETIRED:
            return RETIRED_MESSAGE
        item = COMMANDS.get(name)
        if item is None:
            return '没有这个命令。发送 /help 查看可用命令。'
        if profile not in item.profiles:
            return item.profile_error
        if item.route == 'help':
            return help_text(argument,profile=profile)
        if item.argument and not item.argument.startswith('[') and not argument:
            return help_text(name)
        if not item.argument and argument:
            raise ValueError('此操作不接受参数；用法：' + item.usage)
        if item.module:
            return extensions.invoke(item.module, 'command', profile, name=name, argument=argument, command_action=item.action)
        if item.route == 'software_update':
            from .software_updates import request_update
            return request_update(item.action, profile)
        if item.route == 'agent':
            from .agent_tasks import command
            return command(item.action, argument, profile)
        if item.route == 'ssh_travel':
            from .ssh_travel import command
            result = command(profile, argument)
            return encode_response(result['text'], result.get('reply_markup'))
        if item.route == 'ssh':
            return ssh_text(item.action, argument)
    result = extensions.hook('fallback_text', profile, text=value)
    if result is not None:
        return result
    return '没有识别到命令。发送 /help 查看可用命令。'


def handle_callback(profile: str, payload: dict) -> str:
    value = str(payload.get('data') or '')
    owner = next((mod.id for mod in extensions.snapshot().values()
                  if any(value.startswith(prefix) for prefix in mod.manifest.get('callbacks', []))), None)
    if owner:
        result = extensions.invoke(owner, 'callback', profile, query=payload)
    elif value.startswith('st1|'):
        from . import ssh_travel
        result = ssh_travel.callback(profile, payload)
    elif value.startswith(('su1|', 'su2|')):
        from . import software_updates
        result = software_updates.callback(profile, payload, os.environ.get('PYLON_COMMAND_ID', ''))
    elif value.startswith('ch1|') or value.startswith('ch2|'):
        result = {'text':'旧 CHH 分类入口已移除，请在库库干使用 /market。'}
    else:
        from .inline import owns_callback
        if not owns_callback(profile, payload):
            raise ValueError('按钮不属于本人会话或原始机器人消息')
        raise ValueError('按钮已失效，请重新打开对应入口')
    return encode_response(result.get('text', ''), result.get('reply_markup'))


if __name__ == "__main__":
    try:
        if len(sys.argv) > 2 and sys.argv[2] == '--callback':
            print(handle_callback(sys.argv[1], json.loads(sys.stdin.read())))
        else:
            print(handle(sys.argv[1], sys.stdin.read()))
    except Exception as exc:
        print("操作失败：" + redact(f"{type(exc).__name__}: {exc}"))
        raise SystemExit(1)
