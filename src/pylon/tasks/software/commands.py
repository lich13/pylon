from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROFILE_ROOT = Path(os.environ.get("PYLON_PROFILE_ROOT", "/srv/pylon/profiles/webops"))
PYTHON = Path("/srv/pylon/venv/bin/python")
PENDING_PATH = PROFILE_ROOT / "cron_state" / "software_update_monitor_reply_pending.json"
MAX_REPLY_CHARS = 3500

ADD_RE = re.compile(r"^\s*(?:增加监控|加监控|监控这个网站)\s+(.+?)\s*$", re.IGNORECASE)
REMOVE_RE = re.compile(r"^\s*(?:移除监控|删除监控)\s*$", re.IGNORECASE)
NUMBER_RE = re.compile(r"^\s*(\d{1,4})\s*$")


@dataclass(frozen=True)
class MonitorCommand:
    kind: str
    value: str | None = None


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    data: dict[str, Any]
    output: str


def parse_command(text: str | None, *, has_pending: bool = False) -> MonitorCommand | None:
    value = text or ""
    if has_pending:
        number_match = NUMBER_RE.fullmatch(value)
        if number_match:
            return MonitorCommand("remove_number", number_match.group(1))

    add_match = ADD_RE.fullmatch(value)
    if add_match:
        source = add_match.group(1).strip()
        if source:
            return MonitorCommand("add", source)
        return None

    if REMOVE_RE.fullmatch(value):
        return MonitorCommand("remove_list")

    return None


def handle_add(value: str) -> str:
    result = run_monitor_json(["--add-monitor-url", value, "--json"])
    validation: dict[str, Any] | None = None
    if is_success_result(result):
        validation = run_monitor_json(["--validate-config", "--json"]).data
    return format_add_reply(result, validation)


def handle_remove_list(source) -> str:
    result = run_monitor_json(["--list-projects", "--json"])
    if not is_success_result(result):
        return f"列出监控失败: {short_error(result)}"

    projects = result.data.get("projects")
    if not isinstance(projects, list) or not projects:
        clear_pending(source)
        return "当前没有监控项目"

    set_pending_projects(source, projects)
    return format_project_list(projects)


def handle_list() -> str:
    result = run_monitor_json(["--list-projects", "--json"])
    if not is_success_result(result):
        return f"列出监控失败：{short_error(result)}"
    projects = result.data.get('projects')
    if not isinstance(projects, list) or not projects:
        return '当前没有监控项目'
    return format_project_list(projects, remove=False)


def handle_remove_number(source, number: str) -> str:
    pending = load_pending(PENDING_PATH)
    key = source_key(source)
    entry = pending.get(key)
    projects = entry.get("projects") if isinstance(entry, dict) else None
    if not isinstance(projects, list):
        return "请先发送: 移除监控"

    selected = find_project_by_number(projects, number)
    if selected is None:
        return "编号无效，请回复列表中的数字"

    identity = str(selected.get("id") or selected.get("project_id") or "")
    if not identity:
        return "监控列表已过期，请重新发送：移除监控"
    result = run_monitor_json(["--remove-project", identity, "--json"])
    validation: dict[str, Any] | None = None
    if is_success_result(result):
        validation = run_monitor_json(["--validate-config", "--json"]).data
        clear_pending(source)
    return format_remove_reply(result, validation)


def run_monitor_json(args: list[str]) -> CommandResult:
    env = os.environ.copy()
    env["PYLON_PROFILE_ROOT"] = str(PROFILE_ROOT)
    proc = subprocess.run(
        [str(PYTHON), "-m", "pylon.tasks.software.monitor", *args],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    output = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part.strip())
    data: dict[str, Any]
    try:
        parsed = json.loads(proc.stdout or "{}")
        data = parsed if isinstance(parsed, dict) else {"value": parsed}
    except Exception:
        data = {"error": output or "invalid JSON output"}
    return CommandResult(returncode=proc.returncode, data=data, output=output)


def format_add_reply(result: CommandResult, validation: dict[str, Any] | None) -> str:
    data = result.data
    if data.get("unsupported"):
        return f"不支持，未添加: {short_text(data.get('error') or result.output)}"
    if not is_success_result(result):
        return f"添加失败: {short_error(result)}"

    if data.get("added") or data.get("updated"):
        status = "已添加"
    elif data.get("existing"):
        status = "已存在"
    else:
        status = "已处理"

    names = source_names(data)
    lines = [f"{status}: {names or '软件更新监控源'}"]
    if data.get("classification"):
        names = {'github_software': 'GitHub 软件', 'rss': 'RSS 订阅', 'website': '网站'}
        lines.append('类型：' + names.get(data['classification'], str(data['classification'])))
    lines.append(validate_line(validation))
    return "\n".join(lines)


def format_remove_reply(result: CommandResult, validation: dict[str, Any] | None) -> str:
    if not is_success_result(result):
        return f"移除失败: {short_error(result)}"

    data = result.data
    project = data.get("project") if isinstance(data.get("project"), dict) else {}
    name = str(project.get("name") or project.get("id") or "监控项目")
    source_ids = []
    for key in ("removed_source_ids", "disabled_source_ids"):
        values = data.get(key)
        if isinstance(values, list):
            source_ids.extend(str(value) for value in values if str(value).strip())

    lines = [f"已移除监控: {name}"]
    if source_ids:
        lines.append("监控源 ID：" + ", ".join(dict.fromkeys(source_ids)))
    lines.append(validate_line(validation))
    return "\n".join(lines)


def format_project_list(projects: list[Any], *, remove: bool = True) -> str:
    lines = ["当前监控项目，回复数字移除：" if remove else '当前监控项目：']
    for project in projects:
        if not isinstance(project, dict):
            continue
        number = project.get("number")
        name = project.get("name") or project.get("id") or "未命名"
        lines.append(f"{number}. {name}")
    return "\n".join(lines)


def validate_line(validation: dict[str, Any] | None) -> str:
    if not isinstance(validation, dict):
        return "配置校验：未运行"
    return "配置校验：通过" if validation.get("ok") else "配置校验：失败"


def source_names(data: dict[str, Any]) -> str:
    sources = data.get("sources")
    names: list[str] = []
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                name = str(source.get("name") or source.get("id") or "").strip()
                if name:
                    names.append(name)
    if not names:
        for key in ("added", "updated", "existing"):
            values = data.get(key)
            if isinstance(values, list):
                names.extend(str(value) for value in values if str(value).strip())
    return ", ".join(dict.fromkeys(names))


def is_success_result(result: CommandResult) -> bool:
    return result.returncode == 0 and result.data.get("ok") is not False


def short_error(result: CommandResult) -> str:
    return short_text(result.data.get("error") or result.output or f"exit {result.returncode}")


def short_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def find_project_by_number(projects: list[Any], number: str) -> dict[str, Any] | None:
    try:
        target = int(number)
    except ValueError:
        return None
    for project in projects:
        if isinstance(project, dict) and project.get("number") == target:
            return project
    return None


def source_key(source) -> str:
    platform = getattr(source, "platform", None)
    platform_value = getattr(platform, "value", platform)
    chat_id = getattr(source, "chat_id", "") or ""
    thread_id = getattr(source, "thread_id", None)
    key = f"{str(platform_value).lower()}:{chat_id}"
    if thread_id:
        key += f":{thread_id}"
    return key


def load_pending(path: str | Path = PENDING_PATH) -> dict[str, Any]:
    pending_path = Path(path)
    if not pending_path.exists():
        return {}
    try:
        with pending_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_pending(data: dict[str, Any], path: str | Path = PENDING_PATH) -> None:
    from pylon.shared.state_json import save_atomic
    save_atomic(Path(path), data)


def set_pending_projects(source, projects: list[dict[str, Any]]) -> None:
    pending = load_pending(PENDING_PATH)
    pending[source_key(source)] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "projects": projects,
    }
    save_pending(pending, PENDING_PATH)


def clear_pending(source) -> None:
    pending = load_pending(PENDING_PATH)
    key = source_key(source)
    if key in pending:
        pending.pop(key, None)
        save_pending(pending, PENDING_PATH)


def split_reply(message: str, limit: int = MAX_REPLY_CHARS) -> list[str]:
    if len(message) <= limit:
        return [message]
    chunks: list[str] = []
    remaining = message
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks
