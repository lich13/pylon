from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time

from .commands import authorized
from .bot_menu import watch_registration as synchronize_background
from . import scheduler
from .config import home, profile_env, redact
from .outbox import Outbox
from .transport import DeliveryError, client, drain, telegram_request

log = logging.getLogger("pylon")


def store_updates(box: Outbox, profile: str, updates: list[dict]):
    from .extensions import document_module
    not_before = box.get('commands_not_before', 0)
    with box.connect() as db:
        for update in updates:
            message = update.get('message') or {}
            callback = update.get('callback_query') or {}
            callback_message = callback.get('message') or {}
            callback_owner = {
                'chat': callback_message.get('chat', {}),
                'from': callback.get('from', {}),
                'date': callback_message.get('date', not_before),
            }
            if authorized(profile, message) and isinstance(message.get('text'), str) and message.get('date', not_before) >= not_before:
                db.execute("INSERT OR IGNORE INTO updates(profile,update_id,payload,kind,chat_id,message_id) VALUES(?,?,?,'message',?,?)",
                           (profile, update['update_id'], json.dumps(message, ensure_ascii=False),
                            str(message.get('chat', {}).get('id', '')), message.get('message_id')))
            elif authorized(profile, message) and document_module(profile, message) and message.get('date', not_before) >= not_before:
                db.execute("INSERT OR IGNORE INTO updates(profile,update_id,payload,kind,chat_id,message_id) VALUES(?,?,?,'following_document',?,?)",
                           (profile, update['update_id'], json.dumps(message, ensure_ascii=False),
                            str(message.get('chat', {}).get('id', '')), message.get('message_id')))
            elif authorized(profile, callback_owner) and isinstance(callback.get('data'), str) and callback_owner.get('date', not_before) >= not_before:
                db.execute("INSERT OR IGNORE INTO updates(profile,update_id,payload,kind,chat_id,message_id) VALUES(?,?,?,'callback_query',?,?)",
                           (profile, update['update_id'], json.dumps(callback, ensure_ascii=False),
                            str(callback_message.get('chat', {}).get('id', '')), callback_message.get('message_id')))
        if updates:
            db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", ('offset:' + profile, json.dumps(max(u['update_id'] for u in updates) + 1)))


def poll(box: Outbox, profile: str, stop: threading.Event):
    token = profile_env(profile)['TELEGRAM_BOT_TOKEN']
    with client() as http:
        while not stop.is_set():
            try:
                updates = telegram_request(http, token, 'getUpdates', data={
                    'offset': box.get('offset:' + profile, 0), 'timeout': 25,
                    'allowed_updates': '["message","callback_query"]'}, read_only=True)
                store_updates(box, profile, updates or [])
            except Exception as exc:
                log.warning("%s update polling: %s", profile, redact(str(exc)))
                stop.wait(5)


def command_once(box: Outbox) -> bool:
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM updates WHERE status='pending' ORDER BY update_id LIMIT 1").fetchone()
        if row is None:
            return False
        row = dict(row)
        db.execute("UPDATE updates SET status='running' WHERE profile=? AND update_id=?", (row['profile'], row['update_id']))
    message = json.loads(row['payload'])
    try:
        env = os.environ.copy()
        env['PYLON_COMMAND_ID'] = f"telegram:{row['profile']}:{row['update_id']}"
        is_callback = row.get('kind') == 'callback_query'
        if is_callback:
            try:
                with client() as http:
                    telegram_request(http, profile_env(row['profile'])['TELEGRAM_BOT_TOKEN'], 'answerCallbackQuery',
                                     data={'callback_query_id': message.get('id', '')}, read_only=True)
            except Exception as exc:
                log.info('callback acknowledgement deferred: %s', redact(str(exc)))
        reply_id = (message.get('message', {}) if is_callback else message).get('message_id') if is_callback else message.get('reply_to_message', {}).get('message_id')
        env.pop('PYLON_REPLY_MESSAGE_ID', None)
        if type(reply_id) is int and reply_id > 0:
            env['PYLON_REPLY_MESSAGE_ID'] = str(reply_id)
        if row.get('kind') == 'following_document':
            from .extensions import document_module, invoke
            module_id = document_module(row['profile'], message)
            if not module_id: raise ValueError('document handler unavailable')
            reply, status = invoke(module_id, 'document', row['profile'], message=message), 'done'
            proc = None
        elif is_callback:
            proc = subprocess.run([str(home() / 'venv/bin/python'), '-m', 'pylon.commands', row['profile'], '--callback'],
                                  input=json.dumps(message, ensure_ascii=False), capture_output=True, text=True, timeout=180, env=env)
        else:
            proc = subprocess.run([str(home() / 'venv/bin/python'), '-m', 'pylon.commands', row['profile']],
                                  input=message['text'], capture_output=True, text=True, timeout=180, env=env)
        if proc is not None:
            reply = proc.stdout.strip() or '操作失败，请查看 pylon 日志'
            status = 'done' if proc.returncode == 0 else 'failed'
    except Exception as exc:
        reply, status = '操作结果不确定，请重新打开对应入口核对后再操作', 'uncertain'
        log.warning("command %s: %s", row['update_id'], redact(str(exc)))
    # Persist the reply before completion. A crash never silently replays
    # a mutating user command (especially a numbered removal).
    reply_markup = None
    if reply.startswith('PYLON_REPLY_JSON:'):
        try:
            decoded = json.loads(reply.removeprefix('PYLON_REPLY_JSON:'))
            reply, reply_markup = str(decoded.get('text') or ''), decoded.get('reply_markup')
        except Exception:
            pass
    payload = {'text': reply}
    if reply_markup:
        payload['reply_markup'] = reply_markup
    if is_callback:
        payload['reply_to_message_id'] = reply_id
    agent_task = box.get(f"agent-command:telegram:{row['profile']}:{row['update_id']}")
    if agent_task:
        payload['agent_task'] = agent_task
    box.enqueue(profile=row['profile'], job_id='bot', kind='text', payload=payload, key=f"reply:{row['update_id']}")
    with box.connect() as db:
        db.execute("UPDATE updates SET status=? WHERE profile=? AND update_id=?", (status, row['profile'], row['update_id']))
    return True


def recover(box: Outbox):
    box.recover_sending()
    with box.connect() as db:
        interrupted = db.execute("SELECT profile,update_id FROM updates WHERE status='running'").fetchall()
        db.execute("UPDATE updates SET status='uncertain' WHERE status='running'")
        operations = db.execute("SELECT id,kind FROM operations WHERE status='started'").fetchall()
    for row in interrupted:
        box.enqueue(profile=row['profile'], job_id='bot', kind='text',
                    payload={'text': '服务重启，上一条操作结果待核对。请查询任务或监控列表后再操作。'}, key=f"interrupted:{row['update_id']}")
    for row in operations:
        box.enqueue(profile='default', job_id='b5d6e708bf6e', kind='text',
                    payload={'text': 'Gmail 清理运行中断，删除结果待核对；不会因通知重试而重复执行清理。'}, key='operation:' + row['id'])


def synchronize_chiphell():
    # Cookie import stops and swaps the browser profile transactionally.
    # Leave recovery to the privileged importer while its journal exists.
    if (home() / 'profiles/webops/data/chiphell_fid26/.cookie-import-transaction.json').exists():
        return
    scheduler.admin('chiphell-reconcile')


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    # Telegram and Bark put credentials in their request URLs.
    logging.getLogger('httpx').setLevel(logging.WARNING)
    logging.getLogger('httpcore').setLevel(logging.WARNING)
    (home() / 'run').mkdir(parents=True, exist_ok=True)
    lock = (home() / 'run/daemon.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    box, stop = Outbox(), threading.Event()
    recover(box)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    def commands():
        while not stop.is_set():
            try:
                if not command_once(box):
                    stop.wait(1)
            except Exception as exc:
                log.error('command worker: %s', redact(str(exc)))
                stop.wait(5)

    def control():
        while not stop.is_set():
            try:
                synchronize_chiphell()
            except Exception as exc:
                log.warning('Chiphell control: %s', redact(str(exc)))
            stop.wait(5)

    threads = [threading.Thread(target=poll, args=(box, p, stop), daemon=True) for p in ('default', 'webops')]
    threads.append(threading.Thread(target=commands, daemon=True))
    threads.append(threading.Thread(target=control, daemon=True))
    threads.append(threading.Thread(target=synchronize_background, args=(stop,), daemon=True))
    from .software_updates import synchronize_background as synchronize_updates
    threads.append(threading.Thread(target=synchronize_updates, args=(stop,), daemon=True))
    from .agent_tasks import synchronize_background as synchronize_agents
    threads.append(threading.Thread(target=synchronize_agents, args=(stop,), daemon=True))
    for thread in threads:
        thread.start()
    with client() as http:
        while not stop.is_set():
            try:
                if not drain(box, http, limit=5):
                    stop.wait(0.5)
            except Exception as exc:
                log.error('delivery worker: %s', redact(str(exc)))
                stop.wait(5)
    for thread in threads:
        thread.join(timeout=1)
    lock.close()
