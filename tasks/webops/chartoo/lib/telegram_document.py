from __future__ import annotations

import json
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Any
from urllib import request


DEFAULT_API_ROOT = "https://api.telegram.org"


class TelegramDocumentError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelegramConfig:
    bot_token: str
    chat_id: str
    api_root: str = DEFAULT_API_ROOT


def _parse_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def load_telegram_config(env_path: Path | str) -> TelegramConfig:
    env = _parse_env(Path(env_path))
    token = env.get("CHARTOO_TELEGRAM_BOT_TOKEN") or env.get("TELEGRAM_BOT_TOKEN") or ""
    chat_id = env.get("CHARTOO_TELEGRAM_CHAT_ID") or env.get("TELEGRAM_HOME_CHANNEL") or ""
    api_root = env.get("TELEGRAM_API_ROOT") or DEFAULT_API_ROOT
    if not token:
        raise TelegramDocumentError("missing TELEGRAM_BOT_TOKEN")
    if not chat_id:
        raise TelegramDocumentError("missing TELEGRAM_HOME_CHANNEL")
    return TelegramConfig(bot_token=token, chat_id=chat_id, api_root=api_root.rstrip("/"))


def _mask(text: str, token: str) -> str:
    return str(text).replace(token, "***") if token else str(text)


def _part(boundary: str, name: str, value: str) -> bytes:
    return (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
    ).encode("utf-8")


def _file_part(boundary: str, name: str, path: Path) -> bytes:
    filename = path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    header = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    return header + path.read_bytes() + b"\r\n"


def _multipart_body(fields: dict[str, str], file_path: Path) -> tuple[bytes, str]:
    boundary = f"chartoo-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(_part(boundary, key, value))
    chunks.append(_file_part(boundary, "document", file_path))
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def send_document(
    config: TelegramConfig,
    path: Path | str,
    *,
    caption: str = "",
    timeout: int = 60,
    urlopen: Callable[..., Any] = request.urlopen,
) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        raise TelegramDocumentError(f"document does not exist: {file_path}")
    fields = {"chat_id": config.chat_id}
    if caption:
        fields["caption"] = caption
    body, content_type = _multipart_body(fields, file_path)
    url = f"{config.api_root}/bot{config.bot_token}/sendDocument"
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", content_type)
    req.add_header("Content-Length", str(len(body)))
    try:
        with urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except Exception as exc:
        raise TelegramDocumentError(_mask(str(exc), config.bot_token)) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TelegramDocumentError(_mask(f"telegram returned non-json response: {raw[:200]}", config.bot_token)) from exc
    if not payload.get("ok"):
        raise TelegramDocumentError(_mask(json.dumps(payload, ensure_ascii=False)[:500], config.bot_token))
    return payload
