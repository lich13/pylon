"""Private user authorization and bounded, read-only access to one Helper dialog."""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import getpass
import json
import logging
import os
from pathlib import Path
import re
import tempfile

HELPER_USERNAME = 'linux_do_helper_bot'


class AccountError(RuntimeError):
    pass


class AuthorizationRequired(AccountError):
    pass


class RateLimited(AccountError):
    def __init__(self, seconds: int):
        self.seconds = max(1, int(seconds))
        super().__init__('Telegram rate limit')


def private_root() -> Path:
    from pylon.config import profile_root
    return profile_root('webops') / 'private' / 'linuxdo-helper'


def _check_private(path: Path, *, directory: bool = False) -> None:
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.geteuid() or stat.st_mode & 0o077:
        raise AccountError('unsafe Helper credential permissions')
    if directory != path.is_dir():
        raise AccountError('invalid Helper credential path')


def _read_private(path: Path) -> dict:
    if not path.exists():
        raise AuthorizationRequired('Helper authorization required')
    _check_private(path)
    return json.loads(path.read_text())


def _write_private(path: Path, data: dict) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.helper-', mode='w', delete=False) as file:
        temporary = Path(file.name)
        os.fchmod(file.fileno(), 0o600)
        json.dump(data, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def account_lock(*, create: bool = False):
    root = private_root()
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir():
        raise AuthorizationRequired('Helper authorization required')
    _check_private(root, directory=True)
    lock_path = root / 'account.lock'
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as lock:
        _check_private(lock_path)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AccountError('Helper account already in use') from None
        yield root


def _client(root: Path, credentials: dict):
    from telethon import TelegramClient
    for file in root.glob('account.session*'):
        _check_private(file)
    logging.getLogger('telethon').setLevel(logging.CRITICAL)
    client = TelegramClient(str(root / 'account.session'), credentials['api_id'], credentials['api_hash'],
                            receive_updates=False, entity_cache_limit=16, flood_sleep_threshold=0,
                            request_retries=0, connection_retries=1, timeout=10,
                            device_model='Pylon Linux.do Helper', app_version='1')
    client.session.save_entities = False
    return client


def _translate_error(exc: Exception) -> AccountError:
    name = type(exc).__name__
    if name in {'FloodWaitError', 'FloodPremiumWaitError'}:
        return RateLimited(getattr(exc, 'seconds', 60))
    if name in {'AuthKeyUnregisteredError', 'SessionRevokedError', 'SessionExpiredError',
                'UserDeactivatedError', 'UserDeactivatedBanError', 'UnauthorizedError'}:
        return AuthorizationRequired('Helper authorization required')
    if isinstance(exc, AccountError):
        return exc
    return AccountError(name)


def error_code(exc: Exception) -> str:
    """Expose an exception class only, never an RPC message or supplied input."""
    detail = str(exc) if type(exc) is AccountError else ''
    return detail if re.fullmatch(r'[A-Za-z][A-Za-z0-9]*Error', detail) else type(exc).__name__


class HelperAccount:
    """Only getHistory/getMessages for the pinned bot; never enumerate dialogs or mark read."""
    async def __aenter__(self):
        self.lock = account_lock()
        root = self.lock.__enter__()
        self.client = None
        try:
            credentials = _read_private(root / 'credentials.json')
            self.identity = _read_private(root / 'identity.json')
            self.client = _client(root, credentials)
            await self.client.connect()
            if not await self.client.is_user_authorized():
                raise AuthorizationRequired('Helper authorization required')
            me = await self.client.get_me()
            if me.bot or me.id != self.identity['account_id']:
                raise AuthorizationRequired('Helper account identity changed')
            from telethon.tl.types import InputPeerUser
            self.peer = InputPeerUser(self.identity['bot_id'], self.identity['access_hash'])
            return self
        except Exception as exc:
            await self.__aexit__(None, None, None)
            raise _translate_error(exc) from None

    async def __aexit__(self, *_):
        try:
            if self.client is not None:
                await self.client.disconnect()
        finally:
            self.lock.__exit__(None, None, None)

    async def page(self, *, after: int = 0, limit: int = 100, recent: bool = False) -> list[dict]:
        from .helper import envelope
        try:
            messages = await self.client.get_messages(self.peer, limit=limit, min_id=0 if recent else after,
                                                      reverse=not recent)
            return [envelope(message, self.identity['bot_id']) for message in messages]
        except Exception as exc:
            raise _translate_error(exc) from None

    async def read_marker(self) -> int:
        """Read only this dialog's marker for acceptance; never acknowledge messages."""
        from telethon.tl.functions.messages import GetPeerDialogsRequest
        from telethon.tl.types import InputDialogPeer
        try:
            result = await self.client(GetPeerDialogsRequest([InputDialogPeer(self.peer)]))
            return result.dialogs[0].read_inbox_max_id if result.dialogs else 0
        except Exception as exc:
            raise _translate_error(exc) from None


def authorize() -> int:
    # TTY-only interaction keeps codes/passwords out of automation stdin and captured logs.
    with open('/dev/tty', 'w') as tty:
        def secret(prompt: str) -> str:
            return getpass.getpass(prompt, stream=tty).strip()

        with account_lock(create=True) as root:
            os.umask(0o077)
            credentials_path = root / 'credentials.json'
            if credentials_path.exists():
                credentials = _read_private(credentials_path)
            else:
                tty.write('请使用 my.telegram.org 的 API development tools 中自己的 API 凭据。\n')
                tty.flush()
                api_id, api_hash = secret('API ID（输入隐藏）：'), secret('API hash（输入隐藏）：')
                if not api_id.isdigit() or not re.fullmatch(r'[a-fA-F0-9]{32}', api_hash):
                    raise AccountError('invalid Telegram API credentials')
                credentials = {'api_id': int(api_id), 'api_hash': api_hash}
                _write_private(credentials_path, credentials)

            async def login():
                client = _client(root, credentials)
                try:
                    await client.connect()
                    if not await client.is_user_authorized():
                        phone = re.sub(r'[\s()-]', '', secret('手机号（含国家区号，输入隐藏）：'))
                        if not re.fullmatch(r'\+\d{7,15}', phone):
                            raise AccountError('invalid phone number')
                        await client.send_code_request(phone)
                        try:
                            await client.sign_in(phone, secret('Telegram 登录码（输入隐藏）：'))
                        except Exception as exc:
                            if type(exc).__name__ != 'SessionPasswordNeededError':
                                raise
                            await client.sign_in(password=secret('两步验证密码（输入隐藏）：'))
                    me = await client.get_me()
                    bot = await client.get_entity(HELPER_USERNAME)
                    if me.bot or not bot.bot or (bot.username or '').casefold() != HELPER_USERNAME:
                        raise AccountError('unexpected Helper account identity')
                    identity = {'account_id': me.id, 'bot_id': bot.id, 'access_hash': bot.access_hash}
                    identity_path = root / 'identity.json'
                    if identity_path.exists():
                        previous = _read_private(identity_path)
                        if any(previous[key] != identity[key] for key in ('account_id', 'bot_id')):
                            raise AccountError('Helper identity differs from existing authorization')
                    _write_private(credentials_path, credentials)
                    _write_private(identity_path, identity)
                    tty.write('授权完成。尚未切换采集来源，也不会发送历史消息。\n')
                    tty.flush()
                finally:
                    await client.disconnect()

            try:
                asyncio.run(login())
            except Exception as exc:
                translated = _translate_error(exc)
                if error_code(translated) == 'ApiIdInvalidError':
                    credentials_path.unlink(missing_ok=True)
                raise translated from None
    return 0


def run_authorize() -> int:
    try:
        return authorize()
    except Exception as exc:
        print('授权未完成：' + error_code(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(run_authorize())
