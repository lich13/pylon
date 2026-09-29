"""Reconcile Telegram metadata independently of polling and message delivery."""
from __future__ import annotations

import json
import logging
import re
import threading

from .command_catalog import menu_commands
from .config import profile_env, redact
from .transport import client, telegram_request

PROFILES = ('default', 'webops')
LANGUAGES = ('', 'zh', 'en')
log = logging.getLogger('pylon')


def synchronize(profile: str, *, apply: bool = True, http=None) -> dict:
    if http is None:
        with client() as connection:
            return synchronize(profile, apply=apply, http=connection)
    env = profile_env(profile)
    token, owner = env.get('TELEGRAM_BOT_TOKEN', ''), env.get('TELEGRAM_HOME_CHANNEL', '')
    if not token or not re.fullmatch(r'[1-9][0-9]*', owner):
        raise ValueError('机器人 token 或本人私聊 ID 未配置')
    expected = menu_commands()
    scopes = ({'type': 'default'}, {'type': 'all_private_chats'}, {'type': 'chat', 'chat_id': int(owner)})
    differences, changed, checked = [], 0, 0

    def request(method, data):
        # Metadata setters are idempotent. After any failed/uncertain response,
        # the next attempt reads the actual value before deciding to write.
        return telegram_request(http, token, method, data=data, read_only=True)

    def reconcile(getter, setter, query, field, wanted, label):
        nonlocal checked, changed
        actual = request(getter, query)
        checked += 1
        if actual == wanted:
            return
        differences.append(label)
        if apply:
            request(setter, {**query, field: json.dumps(wanted, ensure_ascii=False)})
            if request(getter, query) != wanted:
                raise RuntimeError('Telegram 菜单写入后核对不一致：' + label)
            changed += 1

    for scope in scopes:
        for language in LANGUAGES:
            query = {'scope': json.dumps(scope), 'language_code': language}
            reconcile('getMyCommands', 'setMyCommands', query, 'commands', expected,
                      scope['type'] + '/' + (language or 'default'))
    for query, label in (({}, 'button/default'), ({'chat_id': owner}, 'button/owner')):
        reconcile('getChatMenuButton', 'setChatMenuButton', query, 'menu_button', {'type': 'commands'}, label)
    return {'profile': profile, 'commands': len(expected), 'checked': checked, 'changed': changed,
            'differences': differences, 'ok': apply or not differences}


def synchronize_background(stop: threading.Event):
    pending, attempt = list(PROFILES), 0
    while pending and not stop.is_set():
        delay = (5, 30, 300)[min(attempt, 2)]
        for profile in tuple(pending):
            if stop.is_set():
                return
            try:
                result = synchronize(profile)
                pending.remove(profile)
                log.info('%s 中文菜单已核对：%s 项命令，更新 %s 处', profile, result['commands'], result['changed'])
            except Exception as exc:
                log.warning('%s 菜单同步失败：%s', profile, redact(str(exc)))
                delay = max(delay, getattr(exc, 'delay', 0))
        if pending:
            stop.wait(delay)
            attempt += 1


def run(action: str) -> int:
    failed = False
    for profile in PROFILES:
        try:
            result = synchronize(profile, apply=action == 'sync')
            failed |= not result['ok']
            print(f"{profile}：{'通过' if result['ok'] else '不一致'}，{result['commands']} 项中文命令，"
                  f"核对 {result['checked']} 处，更新 {result['changed']} 处")
            if not result['ok']:
                print('需同步：' + '、'.join(result['differences']))
        except Exception as exc:
            failed = True
            print(profile + '：菜单核对失败，' + redact(str(exc)))
    return int(failed)
