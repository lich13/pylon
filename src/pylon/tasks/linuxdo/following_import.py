"""Bounded Telegram document input. File contents never enter command logs."""
from __future__ import annotations

import re

from pylon.config import profile_env
from pylon.transport import client, telegram_request
from .following import import_text

FILE_NAME = 'linuxdo-following.json'
LIMIT = 2_000_000


def accepts(profile: str, message: dict) -> bool:
    document = message.get('document')
    return (profile == 'webops' and isinstance(document, dict)
            and (document.get('file_name') == FILE_NAME
                 or str(message.get('caption', '')).split(maxsplit=1)[0:1] == ['/linuxdo_following_import']))


def document(profile: str, message: dict) -> str:
    from pylon.commands import authorized
    if not authorized(profile, message) or not accepts(profile, message):
        return '未识别到本人发送的关注名单文件。'
    metadata = message['document']
    if type(metadata.get('file_size')) is not int or not 0 < metadata['file_size'] <= LIMIT:
        return '关注名单文件为空或过大，未导入。'
    token = profile_env(profile)['TELEGRAM_BOT_TOKEN']
    try:
        with client() as http:
            info = telegram_request(http, token, 'getFile', data={'file_id': metadata['file_id']}, read_only=True)
            path = info.get('file_path', '')
            if not isinstance(path, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', path) or '..' in path or path.startswith('/'):
                return '关注名单下载地址无效，未导入。'
            content = bytearray()
            with http.stream('GET', f'https://api.telegram.org/file/bot{token}/{path}', follow_redirects=False) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    content.extend(chunk)
                    if len(content) > LIMIT:
                        return '关注名单文件过大，未导入。'
        return import_text(profile, content.decode('utf-8'))
    except Exception:
        # HTTP URLs contain the bot token. Never propagate download exceptions.
        return '关注名单暂未导入，请稍后重新发送。'
