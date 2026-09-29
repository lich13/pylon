#!/usr/bin/env python3
"""Replay missed software-update monitor deliveries without invoking an agent."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROFILE_ROOT = Path("/srv/hermes/profiles/webops")
AGENT_ROOT = Path("/srv/hermes/hermes-agent")
HERMES_PYTHON = AGENT_ROOT / "venv" / "bin" / "python"
MONITOR_JOB_ID = "fa27c561ded6"
JOBS_FILE = PROFILE_ROOT / "cron" / "jobs.json"
LOG_FILE = PROFILE_ROOT / "logs" / "agent.log"
OUTPUT_DIR = PROFILE_ROOT / "cron" / "output" / MONITOR_JOB_ID
STATE_FILE = PROFILE_ROOT / "cron_state" / "software_update_monitor_delivery_replay.json"
ITEMS_DIR = PROFILE_ROOT / "data" / "software_update_monitor" / "items"
MONITOR_SCRIPT = PROFILE_ROOT / "scripts" / "software_update_monitor.py"

DEFAULT_WINDOW_SECONDS = 90
REPLAY_HEADER = "【补发】软件更新监控投递失败后重放"
ITEM_REPLAY_HEADER = "【补发】软件更新监控历史条目重放"

DELIVERY_ERROR_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}).*?"
    r"Job '(?P<job_id>[^']+)': delivery error: (?P<message>.*)$"
)
OUTPUT_NAME_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\.md$")


@dataclass(frozen=True)
class DeliveryError:
    job_id: str
    when: datetime
    message: str


@dataclass(frozen=True)
class ReplayCandidate:
    path: Path
    content: str
    digest: str
    error_time: datetime
    error_message: str


@dataclass(frozen=True)
class ItemReplayCandidate:
    source_id: str
    item_id: str
    title: str
    message: str
    digest: str
    mode: str

    @property
    def key(self) -> str:
        return f"{self.source_id}:{self.item_id}"


@dataclass(frozen=True)
class SendResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def parse_log_timestamp(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=timezone.utc)


def parse_output_timestamp(path: Path) -> datetime | None:
    match = OUTPUT_NAME_RE.match(path.name)
    if not match:
        return None
    return datetime.strptime(match.group("ts"), "%Y-%m-%d_%H-%M-%S").replace(tzinfo=timezone.utc)


def parse_delivery_errors(path: Path | None = None) -> list[DeliveryError]:
    path = path or LOG_FILE
    if not path.exists():
        return []

    errors: list[DeliveryError] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = DELIVERY_ERROR_RE.match(line)
        if not match:
            continue
        job_id = match.group("job_id")
        if job_id != MONITOR_JOB_ID:
            continue
        errors.append(
            DeliveryError(
                job_id=job_id,
                when=parse_log_timestamp(match.group("ts")),
                message=match.group("message").strip(),
            )
        )
    return errors


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def is_replayable_output(content: str) -> bool:
    stripped = content.strip()
    if not stripped:
        return False
    if "[SILENT]" in stripped.upper():
        return False
    return "## " in stripped and "Job ID:" in stripped


def find_replay_candidates(
    errors: list[DeliveryError],
    *,
    window_seconds: int = DEFAULT_WINDOW_SECONDS,
) -> list[ReplayCandidate]:
    if not errors or not OUTPUT_DIR.exists():
        return []

    candidates: dict[Path, ReplayCandidate] = {}
    output_paths = sorted(OUTPUT_DIR.glob("*.md"))
    for error in errors:
        for path in output_paths:
            output_time = parse_output_timestamp(path)
            if output_time is None:
                continue
            if abs((error.when - output_time).total_seconds()) > window_seconds:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            if not is_replayable_output(content):
                continue
            digest = sha256_text(content)
            existing = candidates.get(path)
            if existing and existing.error_time <= error.when:
                continue
            candidates[path] = ReplayCandidate(
                path=path,
                content=content,
                digest=digest,
                error_time=error.when,
                error_message=error.message,
            )
    return [candidates[path] for path in sorted(candidates)]


def load_state(path: Path | None = None) -> dict[str, Any]:
    path = path or STATE_FILE
    if not path.exists():
        return {"replayed": {}, "replayed_items": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"replayed": {}, "replayed_items": {}}
    if not isinstance(state, dict):
        return {"replayed": {}, "replayed_items": {}}
    replayed = state.get("replayed")
    if not isinstance(replayed, dict):
        state["replayed"] = {}
    replayed_items = state.get("replayed_items")
    if not isinstance(replayed_items, dict):
        state["replayed_items"] = {}
    return state


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    path = path or STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def was_replayed(state: dict[str, Any], candidate: ReplayCandidate) -> bool:
    entry = state.get("replayed", {}).get(str(candidate.path))
    return isinstance(entry, dict) and entry.get("sha256") == candidate.digest


def mark_replayed(state: dict[str, Any], candidate: ReplayCandidate, result: SendResult) -> None:
    state.setdefault("replayed", {})[str(candidate.path)] = {
        "sha256": candidate.digest,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "error_time": candidate.error_time.isoformat(),
        "error_message": candidate.error_message,
        "returncode": result.returncode,
    }


def was_item_replayed(state: dict[str, Any], candidate: ItemReplayCandidate) -> bool:
    entry = state.get("replayed_items", {}).get(candidate.key)
    return isinstance(entry, dict)


def was_item_key_replayed(state: dict[str, Any], source_id: str, item_id: str) -> bool:
    key = f"{source_id}:{item_id}"
    return isinstance(state.get("replayed_items", {}).get(key), dict)


def mark_item_replayed(state: dict[str, Any], candidate: ItemReplayCandidate, result: SendResult) -> None:
    state.setdefault("replayed_items", {})[candidate.key] = {
        "sha256": candidate.digest,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "source_id": candidate.source_id,
        "item_id": candidate.item_id,
        "title": candidate.title,
        "mode": candidate.mode,
        "returncode": result.returncode,
    }


def load_jobs(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or JOBS_FILE
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [job for job in data if isinstance(job, dict)]
    if isinstance(data, dict):
        jobs = data.get("jobs", [])
        if isinstance(jobs, list):
            return [job for job in jobs if isinstance(job, dict)]
    return []


def resolve_delivery_target(path: Path | None = None) -> str | None:
    for job in load_jobs(path):
        if job.get("id") == MONITOR_JOB_ID:
            deliver = str(job.get("deliver") or "").strip()
            return deliver if deliver and deliver != "local" else None
    return None


def build_replay_message(candidate: ReplayCandidate) -> str:
    return f"{REPLAY_HEADER}\n\n{candidate.content.strip()}\n"


def send_text(message: str, *, target: str) -> SendResult:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(PROFILE_ROOT)
    args = [
        str(HERMES_PYTHON),
        "-m",
        "hermes_cli.main",
        "send",
        "--to",
        target,
        "--file",
        "-",
        "--json",
    ]
    completed = subprocess.run(
        args,
        input=message,
        text=True,
        capture_output=True,
        env=env,
        timeout=60,
    )
    return SendResult(
        ok=completed.returncode == 0,
        stdout=completed.stdout,
        stderr=completed.stderr,
        returncode=completed.returncode,
    )


def send_candidate(candidate: ReplayCandidate, *, target: str) -> SendResult:
    return send_text(build_replay_message(candidate), target=target)


def send_item_candidate(candidate: ItemReplayCandidate, *, target: str) -> SendResult:
    return send_text(candidate.message, target=target)


class OriginalTextTranslator:
    def translate(self, text: str, **_: Any) -> str:
        return text


def build_item_replay_translator(monitor: Any, translation_state: dict[str, Any] | None = None) -> Any:
    translator_cls = getattr(monitor, "DeepLWebTranslator", None)
    cache_path = getattr(monitor, "TRANSLATION_CACHE_FILE", None)
    if translator_cls is None or cache_path is None:
        return OriginalTextTranslator()
    return translator_cls(cache_path=cache_path, translation_state=translation_state)


def load_monitor_module() -> Any:
    script_path = MONITOR_SCRIPT if MONITOR_SCRIPT.exists() else Path(__file__).with_name("software_update_monitor.py")
    spec = importlib.util.spec_from_file_location("software_update_monitor_replay_import", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load monitor script: {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PROFILE_ROOT = PROFILE_ROOT
    module.STATE_FILE = PROFILE_ROOT / "cron_state" / "software_update_monitor.json"
    module.DATA_ROOT = PROFILE_ROOT / "data" / "software_update_monitor"
    module.TRANSLATION_CACHE_FILE = module.DATA_ROOT / "translation_cache.json"
    module.ITEMS_DIR = ITEMS_DIR
    module.LOCK_PATH = PROFILE_ROOT / "cron_state" / "software_update_monitor.lock"
    module.CUSTOM_SOURCES_FILE = PROFILE_ROOT / "config" / "software_update_monitor_sources.json"
    return module


def title_matches_regex(title: str, title_regex: str | None) -> bool:
    if not title_regex:
        return True
    try:
        return bool(re.search(title_regex, title or "", re.I | re.S))
    except re.error:
        return False


def item_id_matches(item_id: str, expected: str | None) -> bool:
    if not expected:
        return True
    return item_id == expected


def build_item_replay_message(
    source: dict[str, Any],
    item: dict[str, Any],
    monitor: Any,
    *,
    translation_state: dict[str, Any] | None = None,
) -> str:
    body = monitor.format_alerts(
        [{"source": source, "item": item}],
        translator=build_item_replay_translator(monitor, translation_state=translation_state),
    ).strip()
    return f"{ITEM_REPLAY_HEADER}\n\n{body}\n"


def build_item_candidate(
    source: dict[str, Any],
    item: dict[str, Any],
    *,
    monitor: Any,
    mode: str,
    translation_state: dict[str, Any] | None = None,
) -> ItemReplayCandidate | None:
    source_id = str(source.get("id") or item.get("source_id") or "").strip()
    item_id = str(item.get("id") or "").strip()
    if not source_id or not item_id:
        return None
    title = str(item.get("title") or "").strip() or item_id
    message = build_item_replay_message(source, item, monitor, translation_state=translation_state)
    return ItemReplayCandidate(
        source_id=source_id,
        item_id=item_id,
        title=title,
        message=message,
        digest=sha256_text(message),
        mode=mode,
    )


def load_raw_item_payloads() -> list[dict[str, Any]]:
    if not ITEMS_DIR.exists():
        return []
    payloads: list[dict[str, Any]] = []
    for path in sorted(ITEMS_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("item"), dict):
            payloads.append(payload)
    return payloads


def find_raw_item_candidates(
    *,
    source_id: str,
    item_id: str | None = None,
    title_regex: str | None = None,
    monitor: Any | None = None,
    state: dict[str, Any] | None = None,
    translation_state: dict[str, Any] | None = None,
) -> list[ItemReplayCandidate]:
    monitor = monitor or load_monitor_module()
    candidates: list[ItemReplayCandidate] = []
    for payload in load_raw_item_payloads():
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        current_source_id = str(source.get("id") or item.get("source_id") or "")
        current_item_id = str(item.get("id") or "")
        title = str(item.get("title") or "")
        if current_source_id != source_id:
            continue
        if not item_id_matches(current_item_id, item_id):
            continue
        if not title_matches_regex(title, title_regex):
            continue
        if state is not None and was_item_key_replayed(state, current_source_id, current_item_id):
            continue
        candidate = build_item_candidate(
            source,
            item,
            monitor=monitor,
            mode="raw_item",
            translation_state=translation_state,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def find_live_feed_item_candidates(
    *,
    source_id: str,
    item_id: str | None = None,
    title_regex: str | None = None,
    monitor: Any | None = None,
    state: dict[str, Any] | None = None,
    translation_state: dict[str, Any] | None = None,
) -> list[ItemReplayCandidate]:
    monitor = monitor or load_monitor_module()
    sources = [source for source in monitor.load_sources() if str(source.get("id") or "") == source_id]
    if not sources:
        return []
    source = sources[0]
    raw = monitor.fetch_url(source["url"])
    candidates: list[ItemReplayCandidate] = []
    for item in monitor.parse_source_items(source, raw):
        current_item_id = str(item.get("id") or "")
        title = str(item.get("title") or "")
        if hasattr(monitor, "item_allowed_by_title_filters") and not monitor.item_allowed_by_title_filters(source, item):
            continue
        if not item_id_matches(current_item_id, item_id):
            continue
        if not title_matches_regex(title, title_regex):
            continue
        if state is not None and was_item_key_replayed(state, source_id, current_item_id):
            continue
        candidate = build_item_candidate(
            source,
            item,
            monitor=monitor,
            mode="live_feed",
            translation_state=translation_state,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def find_item_replay_candidates(
    *,
    source_id: str,
    item_id: str | None = None,
    title_regex: str | None = None,
    from_live_feed: bool = False,
    state: dict[str, Any] | None = None,
    translation_state: dict[str, Any] | None = None,
) -> list[ItemReplayCandidate]:
    if from_live_feed:
        return find_live_feed_item_candidates(
            source_id=source_id,
            item_id=item_id,
            title_regex=title_regex,
            state=state,
            translation_state=translation_state,
        )
    return find_raw_item_candidates(
        source_id=source_id,
        item_id=item_id,
        title_regex=title_regex,
        state=state,
        translation_state=translation_state,
    )


def item_candidate_summary(candidate: ItemReplayCandidate) -> dict[str, str]:
    return {
        "source_id": candidate.source_id,
        "item_id": candidate.item_id,
        "title": candidate.title,
        "mode": candidate.mode,
    }


def run_item_replay(
    *,
    source_id: str,
    item_id: str | None = None,
    title_regex: str | None = None,
    from_live_feed: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not source_id:
        return {"ok": False, "sent": 0, "candidates": 0, "error": "source-id is required"}
    if not item_id and not title_regex:
        return {"ok": False, "sent": 0, "candidates": 0, "error": "item-id or title-regex is required"}

    target = resolve_delivery_target()
    if not target:
        return {"ok": False, "sent": 0, "candidates": 0, "error": "monitor delivery target not found"}

    state = load_state()
    translation_state = state.setdefault("translation", {})
    try:
        candidates = [
            candidate
            for candidate in find_item_replay_candidates(
                source_id=source_id,
                item_id=item_id,
                title_regex=title_regex,
                from_live_feed=from_live_feed,
                state=state,
                translation_state=translation_state,
            )
        ]
    except Exception as exc:
        return {"ok": False, "sent": 0, "candidates": 0, "error": repr(exc)[:500]}

    if dry_run:
        return {
            "ok": True,
            "sent": 0,
            "candidates": len(candidates),
            "dry_run": True,
            "items": [item_candidate_summary(candidate) for candidate in candidates],
        }

    sent = 0
    errors: list[dict[str, Any]] = []
    for candidate in candidates:
        result = send_item_candidate(candidate, target=target)
        if result.ok:
            mark_item_replayed(state, candidate, result)
            save_state(state)
            sent += 1
            continue
        errors.append(
            {
                "source_id": candidate.source_id,
                "item_id": candidate.item_id,
                "title": candidate.title,
                "returncode": result.returncode,
                "stdout": result.stdout[-500:],
                "stderr": result.stderr[-500:],
            }
        )

    return {
        "ok": not errors,
        "sent": sent,
        "candidates": len(candidates),
        "items": [item_candidate_summary(candidate) for candidate in candidates],
        "errors": errors,
    }


def run_replay(*, dry_run: bool = False, window_seconds: int = DEFAULT_WINDOW_SECONDS) -> dict[str, Any]:
    target = resolve_delivery_target()
    if not target:
        return {"ok": False, "sent": 0, "candidates": 0, "error": "monitor delivery target not found"}

    state = load_state()
    candidates = [
        candidate
        for candidate in find_replay_candidates(parse_delivery_errors(), window_seconds=window_seconds)
        if not was_replayed(state, candidate)
    ]
    if dry_run:
        return {
            "ok": True,
            "sent": 0,
            "candidates": len(candidates),
            "dry_run": True,
            "paths": [str(candidate.path) for candidate in candidates],
        }

    sent = 0
    errors: list[dict[str, Any]] = []
    for candidate in candidates:
        result = send_candidate(candidate, target=target)
        if result.ok:
            mark_replayed(state, candidate, result)
            save_state(state)
            sent += 1
            continue
        errors.append(
            {
                "path": str(candidate.path),
                "returncode": result.returncode,
                "stdout": result.stdout[-500:],
                "stderr": result.stderr[-500:],
            }
        )

    return {
        "ok": not errors,
        "sent": sent,
        "candidates": len(candidates),
        "errors": errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay missed software update monitor Telegram deliveries.")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates without sending.")
    parser.add_argument("--json", action="store_true", help="Emit JSON summary.")
    parser.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--source-id", help="Replay a saved/live monitor item from this source id.")
    parser.add_argument("--item-id", help="Replay a saved/live monitor item with this exact item id.")
    parser.add_argument("--title-regex", help="Replay saved/live monitor items whose title matches this regex.")
    parser.add_argument("--from-live-feed", action="store_true", help="Fetch the current source feed instead of saved raw items.")
    args = parser.parse_args(argv)

    if args.source_id or args.item_id or args.title_regex or args.from_live_feed:
        summary = run_item_replay(
            source_id=args.source_id or "",
            item_id=args.item_id,
            title_regex=args.title_regex,
            from_live_feed=args.from_live_feed,
            dry_run=args.dry_run,
        )
    else:
        summary = run_replay(dry_run=args.dry_run, window_seconds=args.window_seconds)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    elif summary.get("sent"):
        print(f"replayed {summary['sent']} software update monitor delivery")
    elif summary.get("errors"):
        print(f"replay failed: {summary['errors'][0].get('stderr') or summary['errors'][0].get('stdout')}")
    else:
        print("[SILENT]")
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
