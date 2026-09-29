from __future__ import annotations

import argparse
import json
import sys
import subprocess

from .outbox import Outbox


def main():
    parser = argparse.ArgumentParser(prog='pylon')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('daemon', 'status', 'outbox', 'reconcile'):
        sub.add_parser(name)
    for name in ('run', 'pause', 'resume', 'logs', 'execute'):
        p = sub.add_parser(name)
        p.add_argument('job_id')
        if name == 'execute':
            p.add_argument('--trigger', choices=['manual', 'scheduled'], default='scheduled')
    ssh = sub.add_parser('ssh')
    ssh.add_argument('action', choices=['status', 'check', 'sync', 'refresh'], default='status', nargs='?')
    ssh.add_argument('address', nargs='?')
    chiphell = sub.add_parser('chiphell')
    chiphell.add_argument('action', choices=['status', 'test', 'baseline', 'pause', 'resume', 'import-cookies'])
    bot_menu = sub.add_parser('bot-menu', help='同步或核对两个机器人的中文菜单')
    bot_menu.add_argument('action', choices=['sync', 'check'])
    updates = sub.add_parser('updates', help='检查腾讯云软件更新，不安装软件')
    updates.add_argument('--all', action='store_true', help='包含全部系统包和依赖明细')
    update = sub.add_parser('update', help='后台执行固定的 sudo 软件更新并向 WebOps 回报')
    update.add_argument('target', choices=['codex', 'grok'])
    args = parser.parse_args()
    if args.command in {'updates', 'update'}:
        from .software_updates import inventory_text, request_update
        print(inventory_text(full=args.all) if args.command == 'updates' else request_update(args.target))
        return 0
    if args.command == 'bot-menu':
        from .bot_menu import run
        return run(args.action)
    if args.command == 'execute':
        from .runner import run
        return run(args.job_id, args.trigger)
    if args.command == 'daemon':
        from .bot import main as serve
        return serve()
    if args.command == 'chiphell':
        return subprocess.run(['sudo', '-n', '/usr/local/sbin/pylon-admin', 'chiphell', args.action]).returncode
    from . import scheduler
    from .commands import control_task, resolve_job, status_text
    if args.command in {'pause', 'resume'}:
        key = 'all' if args.job_id == 'all' else resolve_job(args.job_id)
        print(control_task(key, args.command == 'pause'))
    elif args.command == 'run':
        print(scheduler.start(resolve_job(args.job_id)))
    elif args.command == 'logs':
        print(scheduler.logs(resolve_job(args.job_id)))
    elif args.command == 'status':
        print(status_text())
    elif args.command == 'ssh':
        if args.action in {'sync', 'refresh'}:
            if args.address:
                parser.error('此操作不接受 IP 参数')
            print(scheduler.start('ssh-admin-sync' if args.action == 'sync' else 'ssh-beijing-refresh'))
        else:
            print(json.dumps(scheduler.ssh(args.action, args.address), ensure_ascii=False, indent=2))
    else:
        box = Outbox()
        with box.connect() as db:
            unresolved = [dict(row) for row in db.execute("SELECT id,job_id,kind,status,attempts,error FROM events WHERE status!='delivered' ORDER BY created_at LIMIT 30")]
        print(json.dumps({'outbox': box.counts(), 'unresolved': unresolved}, ensure_ascii=False))
        return int(args.command == 'reconcile' and bool(unresolved))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        from .config import redact
        print(redact(f'{type(exc).__name__}: {exc}'), file=sys.stderr)
        sys.exit(1)
