"""A private, explicitly imported snapshot of the user's forum following list."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import time

from .common import digest, timestamp
from .helper_account import _check_private, _write_private, account_lock, private_root


@dataclass(frozen=True)
class Following:
    usernames: frozenset[str]
    imported_at: str
    owner: str = ''

    @property
    def fingerprint(self) -> str:
        return digest(sorted(self.usernames))

    def payload(self) -> dict:
        value = {'version': 1, 'complete': True, 'imported_at': self.imported_at,
                 'usernames': sorted(self.usernames)}
        if self.owner:
            value.update(version=2, owner=self.owner)
        return value

    def summary(self) -> dict:
        return {'configured': True, 'count': len(self.usernames), 'imported_at': self.imported_at,
                'fingerprint': self.fingerprint}


def validate(value: dict) -> Following:
    fields = {'version', 'complete', 'imported_at', 'usernames'}
    if isinstance(value, dict) and value.get('version') == 2:
        fields.add('owner')
    if (not isinstance(value, dict) or set(value) != fields
            or type(value.get('version')) is not int or value['version'] not in {1, 2}
            or value.get('complete') is not True or not isinstance(value.get('imported_at'), str)
            or timestamp(value['imported_at']) is None):
        raise ValueError('invalid following snapshot')
    names = value['usernames']
    if (not isinstance(names, list) or len(names) > 10000
            or any(not isinstance(name, str) or not re.fullmatch(r'[\w.-]{1,100}', name) for name in names)):
        raise ValueError('invalid following usernames')
    owner = value.get('owner', '')
    if value['version'] == 2 and (not isinstance(owner, str) or not re.fullmatch(r'[\w.-]{1,100}', owner)):
        raise ValueError('invalid following owner')
    return Following(frozenset(name.casefold() for name in names), value['imported_at'], owner.casefold())


def load(path: Path | None = None) -> Following | None:
    path = path or private_root() / 'following.json'
    if not path.exists():
        return None
    _check_private(path.parent, directory=True)
    _check_private(path)
    if path.stat().st_size > 2_000_000:
        raise ValueError('following snapshot too large')
    return validate(json.loads(path.read_text()))


def install(path: Path, *, owner: str = '') -> dict:
    value = load(path)
    if value is None:
        raise ValueError('following snapshot missing')
    if owner:
        if not re.fullmatch(r'[\w.-]{1,100}', owner) or (value.owner and value.owner != owner.casefold()):
            raise ValueError('invalid following owner')
        value = Following(value.usernames, value.imported_at, owner.casefold())
    return replace(value, require_owner=False)


def replace(value: Following, *, require_owner: bool = True) -> dict:
    """The trusted terminal binds the forum identity; Telegram cannot rebind it."""
    with account_lock() as root:
        previous = load(root / 'following.json')
        if require_owner and (not value.owner or previous is None or not previous.owner):
            raise ValueError('请先在终端绑定本人的论坛账号')
        if previous and previous.owner and value.owner != previous.owner:
            raise ValueError('关注名单所属账号不匹配')
        incoming_time = timestamp(value.imported_at)
        if incoming_time > time.time() + 300:
            raise ValueError('名单采集时间晚于当前时间')
        if previous and incoming_time < timestamp(previous.imported_at):
            raise ValueError('旧名单不能覆盖更新的名单')
        if previous and incoming_time == timestamp(previous.imported_at) and value.usernames != previous.usernames:
            raise ValueError('同一采集时间的名单内容不一致')
        before = previous.usernames if previous else frozenset()
        changed = previous is None or previous.payload() != value.payload()
        if changed:
            _write_private(root / 'following.json', value.payload())
        return value.summary() | {'added': len(value.usernames - before),
                                  'removed': len(before - value.usernames), 'changed': changed}


def import_text(profile: str, text: str) -> str:
    if profile != 'webops':
        return '请将关注名单发送给负责 Linux.do 通知的机器人。'
    if len(text.encode('utf-8')) > 2_000_000:
        return '关注名单过大，未导入。'
    try:
        value = validate(json.loads(text))
        result = replace(value)
    except (ValueError, TypeError) as exc:
        # Never echo parser input, names or untrusted JSON exception details.
        known = {'请先在终端绑定本人的论坛账号', '关注名单所属账号不匹配',
                 '名单采集时间晚于当前时间', '旧名单不能覆盖更新的名单', '同一采集时间的名单内容不一致'}
        return str(exc) if str(exc) in known else '关注名单格式无效或不完整，未导入。'
    except Exception:
        return '关注名单暂未导入，请稍后重新发送。'
    return (f"关注名单已更新：{result['count']} 人，新增 {result['added']} 人，移除 {result['removed']} 人。"
            if result['changed'] else f"关注名单未变化：{result['count']} 人。")


def available() -> tuple[Following | None, str]:
    try:
        return load(), ''
    except Exception as exc:
        # Invalid/missing metadata withholds only ambiguous notices, not personal alerts.
        return None, type(exc).__name__
