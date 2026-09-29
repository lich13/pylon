from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .outbox import Outbox

_deferred: dict[Path, object] = {}


def enabled() -> bool:
    return bool(os.environ.get("PYLON_OUTBOX"))


def silent(text: str) -> bool:
    text = text.strip()
    if not text or text in {"[SILENT]", "SILENT", "NO_REPLY", "NO REPLY"}:
        return True
    try:
        value = json.loads(text)
        return isinstance(value, dict) and value.get("wakeAgent") is False
    except (ValueError, TypeError):
        return False


def context() -> dict:
    return {"profile": os.environ.get("PYLON_PROFILE", "webops"),
            "job_id": os.environ.get("PYLON_JOB_ID", "manual"),
            "run_id": os.environ.get("PYLON_RUN_ID", "")}


def text_key(text: str, job_id: str) -> str:
    digest = hashlib.sha256(text.encode()).hexdigest()
    # Feed items deduplicate across a restart; daily summaries and operational
    # reminders may intentionally have the same body on different days/slots.
    if job_id == "b5d6e708bf6e":
        suffix = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    elif job_id in {"f3ec3ec24210", "e961da51fe69", "manual", "xianbao-hermes-poll"}:
        suffix = str(int(time.time()) // 300)
    else:
        suffix = ""
    return f"{job_id}:{suffix}:{digest}"


def publish(text: str, *, key: str | None = None, profile: str | None = None) -> str | None:
    if not enabled() or silent(text):
        return None
    ctx = context()
    if profile:
        ctx["profile"] = profile
    return Outbox().enqueue(kind="text", payload={"text": text}, key=key or text_key(text, ctx["job_id"]), **ctx)


def publish_document(path: Path, caption: str, *, key: str) -> str | None:
    if not enabled():
        return None
    return Outbox().document(path, payload={"caption": caption}, key=key, **context())


def publish_bark(payload: dict, api_base: str) -> str | None:
    if not enabled():
        return None
    ctx = context()
    base_key = os.environ.get("PYLON_BARK_KEY")
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    key = base_key or f"{ctx['job_id']}:{digest}:{int(time.time()) // 900}"
    return Outbox().enqueue(kind="bark", payload={"body": payload, "api_base": api_base}, key=key, **ctx)


def result(status: str, detail: str = ""):
    target = os.environ.get("PYLON_RESULT_FILE")
    if target:
        path = Path(target)
        previous = json.loads(path.read_text()) if path.exists() else {}
        if previous.get("status") == "failed" and status != "failed":
            return
        path.write_text(json.dumps({"status": status, "detail": detail}, ensure_ascii=False))


def defer_json(path: Path, value) -> bool:
    if enabled() and os.environ.get("PYLON_JOB_ID") == "b5d6e708bf6e":
        _deferred[path] = value
        return True
    return False


def flush_deferred():
    for path, value in _deferred.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".pylon-tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    _deferred.clear()
