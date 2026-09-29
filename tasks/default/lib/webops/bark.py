from __future__ import annotations

import json
from pylon import events
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import paths

DEFAULT_API_BASE = "https://api.day.app"
SECRET_DIR = paths.PROFILE_ROOT / "secrets"
DEFAULT_DEVICE_KEY_FILE = SECRET_DIR / "bark_device_key"
# Backward-compatible fallback for the first electronics-arbitrage integration.
LEGACY_DEVICE_KEY_FILES = (SECRET_DIR / "bark_arbitrage_device_key",)
DEFAULT_GROUP = "webops"
DEFAULT_TIMEOUT_SECONDS = 10
DEFAULT_KEY_ENV_NAMES = ("BARK_DEVICE_KEY", "BARK_ARBITRAGE_DEVICE_KEY")
VALID_LEVELS = {"active", "timeSensitive", "passive", "critical"}


@dataclass(frozen=True)
class BarkResult:
    ok: bool
    http_status: int
    code: int | None
    message: str


def _compact(s: str, limit: int) -> str:
    s = " ".join((s or "").split())
    if len(s) <= limit:
        return s
    return s[: limit - 1].rstrip() + "…"


def _as_path(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    return Path(value).expanduser()


def secret_file(name: str) -> Path:
    """Return a safe secret path below the WebOps profile secret directory."""
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
        raise ValueError("secret name must be 1-80 chars: letters, numbers, dot, underscore, dash")
    return SECRET_DIR / name


def _candidate_key_files(secret_name: str | None = None, key_file: str | Path | None = None) -> list[Path]:
    candidates: list[Path] = []
    explicit = _as_path(key_file)
    if explicit is not None:
        candidates.append(explicit)
    if secret_name:
        candidates.append(secret_file(secret_name))
    candidates.append(DEFAULT_DEVICE_KEY_FILE)
    candidates.extend(LEGACY_DEVICE_KEY_FILES)

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def load_device_key(
    *,
    secret_name: str | None = None,
    key_file: str | Path | None = None,
    env_names: Sequence[str] = DEFAULT_KEY_ENV_NAMES,
) -> str:
    """Load a Bark device key without hard-coding it in monitored scripts.

    Lookup order:
    1. Environment variables in env_names.
    2. Explicit key_file, if provided.
    3. Named secret file under /srv/pylon/profiles/default/secrets/, if provided.
    4. Generic WebOps Bark secret file.
    5. Legacy electronics-arbitrage secret file.
    """
    for env_name in env_names:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    for path in _candidate_key_files(secret_name=secret_name, key_file=key_file):
        try:
            if path.exists():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            continue
    return ""


def redact_device_key(payload: dict[str, Any]) -> dict[str, Any]:
    redacted = dict(payload)
    if "device_key" in redacted:
        redacted["device_key"] = "[REDACTED]"
    if "device_keys" in redacted:
        redacted["device_keys"] = "[REDACTED]"
    return redacted


def build_payload(
    *,
    title: str,
    body: str,
    device_key: str,
    url: str = "",
    group: str = DEFAULT_GROUP,
    level: str = "active",
    volume: int = 5,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if level not in VALID_LEVELS:
        raise ValueError(f"invalid Bark level: {level}")
    payload: dict[str, Any] = {
        "device_key": device_key,
        "title": _compact(title, 160),
        "body": _compact(body, 3500),
        "level": level,
        "volume": max(0, min(10, int(volume))),
        "group": group or DEFAULT_GROUP,
        "isArchive": "1",
    }
    if url:
        payload["url"] = url
    if extra:
        payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _parse_bark_response(http_status: int, body: bytes) -> BarkResult:
    text = body.decode("utf-8", "replace")[:1000]
    code: int | None = None
    message = text.strip() or f"HTTP {http_status}"
    try:
        payload = json.loads(text) if text else {}
        if isinstance(payload, dict):
            raw_code = payload.get("code")
            if isinstance(raw_code, int):
                code = raw_code
            elif isinstance(raw_code, str) and raw_code.isdigit():
                code = int(raw_code)
            raw_msg = payload.get("message") or payload.get("msg")
            if raw_msg:
                message = str(raw_msg)
    except Exception:
        pass
    ok = 200 <= http_status < 300 and (code in (None, 200, 0))
    return BarkResult(ok=ok, http_status=http_status, code=code, message=message)


def send_payload(
    payload: dict[str, Any],
    *,
    api_base: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool | None = None,
) -> BarkResult:
    """Send an already-built Bark JSON payload.

    Never log payload directly without redact_device_key().
    """
    if dry_run is None:
        dry_run = os.environ.get("BARK_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}
    if dry_run:
        return BarkResult(ok=True, http_status=0, code=0, message="dry-run")

    base = (api_base or os.environ.get("BARK_API_BASE") or DEFAULT_API_BASE).rstrip("/")
    if events.enabled():
        event_id = events.publish_bark(payload, base)
        return BarkResult(ok=True, http_status=202, code=202, message="queued:" + str(event_id))
    endpoint = f"{base}/push"
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "Hermes-WebOps/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            return _parse_bark_response(int(resp.status), resp.read())
    except urllib.error.HTTPError as exc:
        try:
            body_bytes = exc.read()
        except Exception:
            body_bytes = b""
        return _parse_bark_response(int(exc.code), body_bytes)
    except Exception as exc:
        return BarkResult(ok=False, http_status=0, code=None, message=f"{type(exc).__name__}: {exc}")


def send_push(
    *,
    title: str,
    body: str,
    url: str = "",
    group: str = DEFAULT_GROUP,
    level: str = "active",
    volume: int = 5,
    device_key: str | None = None,
    secret_name: str | None = None,
    key_file: str | Path | None = None,
    api_base: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> BarkResult:
    """Send a Bark push via JSON POST.

    Generic reusable entrypoint for monitors. Use send_critical_push() or pass
    level='critical', volume=10 only for alerts that truly need the highest iOS
    interruption level.
    """
    key = (device_key if device_key is not None else load_device_key(secret_name=secret_name, key_file=key_file)).strip()
    if not key:
        return BarkResult(ok=False, http_status=0, code=None, message="missing Bark device key")
    payload = build_payload(title=title, body=body, url=url, group=group, level=level, volume=volume, device_key=key, extra=extra)
    return send_payload(payload, api_base=api_base, timeout_seconds=timeout_seconds, dry_run=dry_run)


def send_critical_push(
    *,
    title: str,
    body: str,
    url: str = "",
    group: str = DEFAULT_GROUP,
    device_key: str | None = None,
    secret_name: str | None = None,
    key_file: str | Path | None = None,
    api_base: str | None = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    dry_run: bool | None = None,
    extra: dict[str, Any] | None = None,
) -> BarkResult:
    return send_push(
        title=title,
        body=body,
        url=url,
        group=group,
        level="critical",
        volume=10,
        device_key=device_key,
        secret_name=secret_name,
        key_file=key_file,
        api_base=api_base,
        timeout_seconds=timeout_seconds,
        dry_run=dry_run,
        extra=extra,
    )
