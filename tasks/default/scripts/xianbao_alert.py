#!/usr/bin/env python3
"""Small Telegram notifier for the Xianbao sidecar service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request


DEFAULT_PROFILE_ROOT = "/srv/hermes"
DEFAULT_ALERT_STATE = "/srv/hermes/data/xianbao_filter/alert_state.json"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        if key in os.environ:
            continue
        os.environ[key] = value.strip().strip("'\"")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def should_rate_limit(key: str, cooldown_seconds: int, state_path: Path) -> bool:
    if cooldown_seconds <= 0:
        return False
    now = int(time.time())
    data: dict[str, int] = {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    last = int(data.get(key, 0) or 0)
    if now - last < cooldown_seconds:
        return True
    data[key] = now
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(state_path)
    return False


def journal_tail(unit: str, lines: int) -> str:
    if lines <= 0:
        return ""
    try:
        result = subprocess.run(
            ["journalctl", "-u", unit, "-n", str(lines), "--no-pager", "-o", "short-iso"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except Exception as exc:
        return f"journal tail unavailable: {exc}"
    text = (result.stdout or result.stderr or "").strip()
    if len(text) > 2600:
        text = text[-2600:]
    return text


def cron_job_tail(profile_root: Path, job_id: str, lines: int) -> str:
    if not job_id or lines <= 0:
        return ""
    parts: list[str] = []
    output_dir = profile_root / "cron" / "output" / job_id
    try:
        files = sorted((p for p in output_dir.glob("*.md") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
    except Exception:
        files = []
    if files:
        try:
            text = files[0].read_text(encoding="utf-8", errors="replace").strip()
            if len(text) > 1600:
                text = text[-1600:]
            if text:
                parts.append("Recent cron output:\n" + text)
        except Exception as exc:
            parts.append(f"Recent cron output unavailable: {exc}")

    log_path = profile_root / "logs" / "agent.log"
    try:
        matched = [
            line.rstrip("\n")
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            if f"Job '{job_id}'" in line or f'Job "{job_id}"' in line or job_id in line
        ]
    except Exception:
        matched = []
    if matched:
        text = "\n".join(matched[-lines:]).strip()
        if len(text) > 1600:
            text = text[-1600:]
        if text:
            parts.append("Recent scheduler log:\n" + text)
    return "\n\n".join(parts)


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("XIANBAO_TELEGRAM_ALERT_CHAT_ID", "").strip() or os.getenv("TELEGRAM_HOME_CHANNEL", "").strip()
    if not token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN")
    if not chat_id:
        raise RuntimeError("missing TELEGRAM_HOME_CHANNEL or XIANBAO_TELEGRAM_ALERT_CHAT_ID")
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text[:3900],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=env_int("XIANBAO_TELEGRAM_ALERT_TIMEOUT_SECONDS", 15)) as resp:
        body = resp.read(4096)
    try:
        data = json.loads(body.decode("utf-8", errors="replace"))
    except Exception:
        data = {}
    if data and not data.get("ok", False):
        raise RuntimeError(f"telegram sendMessage failed: {data.get('description') or data}")


def is_normal_service_exit() -> bool:
    result = os.getenv("SERVICE_RESULT", "").strip()
    exit_code = os.getenv("EXIT_CODE", "").strip()
    exit_status = os.getenv("EXIT_STATUS", "").strip()
    if result == "success":
        return True
    return result == "" and exit_code in ("", "exited") and exit_status in ("", "0")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", default="alert")
    parser.add_argument("--unit", default=os.getenv("XIANBAO_ALERT_UNIT", "xianbao-hermes.service"))
    parser.add_argument("--job-id", default=os.getenv("XIANBAO_ALERT_JOB_ID", ""))
    parser.add_argument("--message", default="")
    parser.add_argument("--journal-lines", type=int, default=12)
    parser.add_argument("--cooldown-seconds", type=int, default=env_int("XIANBAO_ALERT_COOLDOWN_SECONDS", 900))
    parser.add_argument("--no-rate-limit", action="store_true")
    parser.add_argument("--profile-root", default=os.getenv("HERMES_HOME", DEFAULT_PROFILE_ROOT))
    parser.add_argument("--state", default=os.getenv("XIANBAO_ALERT_STATE", DEFAULT_ALERT_STATE))
    args = parser.parse_args()

    profile_root = Path(args.profile_root)
    load_env_file(profile_root / ".env")
    load_env_file(profile_root / "config" / "xianbao-filter.env")

    if args.kind == "service-exit" and is_normal_service_exit():
        print("normal service exit; alert skipped")
        return 0

    rate_key = f"{args.kind}:{args.unit}"
    if not args.no_rate_limit and should_rate_limit(rate_key, args.cooldown_seconds, Path(args.state)):
        print(f"alert suppressed by cooldown: {rate_key}")
        return 0

    host = socket.gethostname()
    message = args.message.strip() or "xianbao-hermes alert"
    tail = cron_job_tail(profile_root, args.job_id, args.journal_lines) if args.job_id else journal_tail(args.unit, args.journal_lines)
    text = f"[xianbao-hermes] {args.kind} on {host}\n{message}"
    if tail:
        text += "\n\nRecent journal:\n" + tail
    send_telegram(text)
    print("telegram alert sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
