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
from .command_catalog import COMMANDS, help_text
from . import scheduler
from .outbox import Outbox
from .runner import task_environment


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
    from .tasks.quotes.health import status_text as quote_status
    lines.append(quote_status(box=box))
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


def monitor_command(text: str, chat_id: str) -> str | None:
    os.environ.update(task_environment("webops"))
    from .tasks.software import commands as module
    source = SimpleNamespace(platform="telegram", chat_id=chat_id, thread_id=None)
    parsed = (module.MonitorCommand('list') if text == '/monitors' else
              module.parse_command(text, has_pending=module.source_key(source) in module.load_pending(module.PENDING_PATH)))
    if parsed is None:
        return None
    (home() / 'run').mkdir(parents=True, exist_ok=True)
    with (home() / "run/fa27c561ded6.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "软件更新检查正在运行，请稍后重试"
        if parsed.kind == "add":
            return module.handle_add(parsed.value)
        if parsed.kind == "remove_list":
            return module.handle_remove_list(source)
        if parsed.kind == 'list':
            return module.handle_list()
        return module.handle_remove_number(source, parsed.value)


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


def chiphell_text(action: str) -> str:
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


def gmail_text(action: str, candidate: str = '') -> str:
    if action != 'list' and not re.fullmatch(r'gmail-nt-[A-Za-z0-9_-]+', candidate):
        return '候选 ID 格式不正确，请从 /gmail_candidates 复制完整 ID。\n\n' + help_text('gmail_' + action)
    arguments = ['--list-candidates'] if action == 'list' else [f'--{action}-candidate', candidate]
    result = locked_command('b5d6e708bf6e', [str(home() / 'venv/bin/python'),
                            '-m', 'pylon.tasks.gmail.cleanup', *arguments, '--format', 'text'], 'default')
    if action == 'accept' and result.startswith('已加入'):
        result += '\n仅影响后续定时清理，本次未删除邮件。'
    return result


def handle(profile: str, text: str) -> str:
    value = text.strip()
    reply_id = os.environ.get('PYLON_REPLY_MESSAGE_ID', '')
    if reply_id.isdigit() and not value.startswith('/'):
        from .agent_tasks import reply_to
        result = reply_to(profile, int(reply_id), value)
        if result is not None:
            return result
    if value == '任务状态':
        return status_text()
    if value.startswith('/'):
        token, *rest = value.split(maxsplit=1)
        name, argument = token[1:].split('@', 1)[0].lower(), rest[0].strip() if rest else ''
        if name == 'start':
            return help_text()
        if name in {'gmail', 'ssh', 'chiphell'}:
            parts = argument.split(maxsplit=1)
            action = parts[0] if parts else ('list' if name == 'gmail' else 'status')
            argument = parts[1] if len(parts) > 1 else ''
            name = name + '_' + ('candidates' if name == 'gmail' and action == 'list' else action)
        item = COMMANDS.get(name)
        if item is None:
            return '没有这个命令。发送 /help 查看可用命令。'
        if item.route == 'help':
            return help_text(argument)
        if item.argument and not item.argument.startswith('[') and not argument:
            return help_text(name)
        if not item.argument and argument:
            raise ValueError('此操作不接受参数；用法：' + item.usage)
        if item.route == 'status':
            return status_text()
        if item.route == 'outbox':
            return outbox_text()
        if item.route == 'updates':
            if argument not in {'', 'all'}:
                return help_text(name)
            from .software_updates import inventory_text
            return inventory_text(full=argument == 'all')
        if item.route == 'software_update':
            from .software_updates import request_update
            return request_update(item.action, profile)
        if item.route == 'agent':
            from .agent_tasks import command
            return command(item.action, argument, profile)
        if item.route == 'task':
            key = 'all' if argument == 'all' and item.action in {'pause', 'resume'} else resolve_job(argument)
            if item.action in {'pause', 'resume'}:
                return control_task(key, item.action == 'pause')
            if item.action == 'logs':
                return scheduler.logs(key)[-12000:]
            return jobs()[key]['name'] + '：' + scheduler.start(key)
        if item.route == 'ssh':
            return ssh_text(item.action, argument)
        if item.route == 'chiphell':
            return chiphell_text(item.action)
        if item.route == 'gmail':
            return gmail_text(item.action, argument)
        if item.route == 'monitor':
            value = {'list': '/monitors', 'remove': '移除监控', 'add': '增加监控 ' + argument}[item.action]
    result = monitor_command(value, profile_env(profile)["TELEGRAM_HOME_CHANNEL"])
    if result is not None:
        return result
    return '没有识别到命令。发送 /help 查看可用命令。'


if __name__ == "__main__":
    try:
        print(handle(sys.argv[1], sys.stdin.read()))
    except Exception as exc:
        print("操作失败：" + redact(f"{type(exc).__name__}: {exc}"))
        raise SystemExit(1)
