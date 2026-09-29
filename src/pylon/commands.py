from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from .config import home, jobs, profile_env, profile_root, redact
from .dagu import request, suspend
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
    if job_id not in jobs():
        raise ValueError("任务不存在")
    if job_id == "63c6ab6c4af3":
        subprocess.run(["sudo", "-n", "/usr/local/sbin/chiphell-proxyctl", "pause" if paused else "resume"],
                       check=True, capture_output=True, text=True, timeout=150)
    else:
        suspend(job_id, paused)
    Outbox().put("paused:" + job_id, paused)
    return f"{jobs()[job_id]['name']}已{'暂停' if paused else '恢复'}"


def status_text() -> str:
    box = Outbox()
    lines = []
    try:
        remote = request("GET", "/dags")
        paused = {row.get('dag', {}).get('name', row.get('fileName')): bool(row.get('suspended')) for row in remote.get('dags', [])}
    except Exception:
        paused = {}
    names = {"ok": "正常", "no_change": "无变化", "running": "运行中", "failed": "业务失败",
             "delivery_pending": "待补发", "interrupted": "执行中断"}
    with box.connect() as db:
        for index, (key, job) in enumerate(jobs().items(), 1):
            row = db.execute("SELECT status FROM runs WHERE job_id=? ORDER BY started_at DESC LIMIT 1", (key,)).fetchone()
            state = names.get(row[0], row[0]) if row else "未执行"
            if paused.get(key, box.get("paused:" + key, False)):
                state = "已暂停 / " + state
            lines.append(f"{index}. {job['name']}：{state}")
    counts = box.counts()
    unresolved = sum(counts.get(s, 0) for s in ('pending', 'sending', 'uncertain', 'failed'))
    lines.append(f"待处理通知 {unresolved}；回执不确定 {counts.get('uncertain', 0)}")
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
    # Load the deterministic parser in a dedicated command process. No gateway
    # or model is imported and no Telegram update is passed to a shell.
    root = profile_root("webops")
    os.environ.update(task_environment("webops"))
    spec = importlib.util.spec_from_file_location("pylon_monitor_commands", root / "plugins/software-update-monitor-reply/__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    source = SimpleNamespace(platform="telegram", chat_id=chat_id, thread_id=None)
    parsed = module.parse_command(text, has_pending=module.source_key(source) in module.load_pending(module.PENDING_PATH))
    if parsed is None:
        return None
    with (home() / "run/fa27c561ded6.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return "软件更新检查正在运行，请稍后重试"
        if parsed.kind == "add":
            return module.handle_add(parsed.value)
        if parsed.kind == "remove_list":
            return module.handle_remove_list(source)
        return module.handle_remove_number(source, parsed.value)


def handle(profile: str, text: str) -> str:
    value = text.strip()
    if value in {"/status", "任务状态"}:
        return status_text()
    control = re.fullmatch(r"/(pause|resume)\s+(.+)", value)
    if control:
        return control_task(resolve_job(control[2].strip()), control[1] == "pause")
    gmail = re.fullmatch(r"/gmail(?:\s+(list|accept|ignore)(?:\s+(gmail-nt-[A-Za-z0-9_-]+))?)?", value)
    if gmail:
        action, candidate = gmail[1] or "list", gmail[2]
        if action != "list" and not candidate:
            return "用法：/gmail accept 候选ID 或 /gmail ignore 候选ID"
        arguments = ["--list-candidates"] if action == "list" else [f"--{action}-candidate", candidate]
        result = locked_command("b5d6e708bf6e", [str(home() / "venv/bin/python"),
                   str(profile_root("default") / "scripts/gmail_cleanup_delete.py"), *arguments, "--format", "text"], "default")
        if action == "accept" and result.startswith("已加入"):
            result += "\n仅影响后续定时清理，本次未删除邮件。"
        return result
    result = monitor_command(value, profile_env(profile)["TELEGRAM_HOME_CHANNEL"])
    if result is not None:
        return result
    return ("/status\n/pause 任务编号\n/resume 任务编号\n/gmail list\n/gmail accept 候选ID\n/gmail ignore 候选ID\n"
            "增加监控 URL\n加监控 owner/repo\n移除监控 → 回复编号")


if __name__ == "__main__":
    try:
        print(handle(sys.argv[1], sys.stdin.read()))
    except Exception as exc:
        print("操作失败：" + redact(f"{type(exc).__name__}: {exc}"))
        raise SystemExit(1)
