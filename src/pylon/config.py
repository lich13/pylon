from __future__ import annotations

import json
import os
import re
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("PYLON_HOME", "/srv/pylon"))


def profile_root(profile: str) -> Path:
    if profile not in {"default", "webops"}:
        raise ValueError("unknown profile")
    return home() / "profiles" / profile


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def profile_env(profile: str) -> dict[str, str]:
    return read_env(profile_root(profile) / ".env")


def jobs() -> dict[str, dict]:
    from .extensions import snapshot
    path = home() / "jobs.json"
    catalog = json.loads(path.read_text()) if path.exists() else {}
    for mod in snapshot().values():
        for key, job in mod.manifest.get('jobs', {}).items():
            if key in catalog:
                raise ValueError('duplicate task: ' + key)
            catalog[key] = job
    return catalog


def redact(value: str) -> str:
    value = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[BOT_TOKEN]", str(value))
    value = re.sub(r"https://api\.day\.app/[A-Za-z0-9_-]+", "https://api.day.app/[REDACTED]", value)
    value = re.sub(r"(?i)(authorization|api[_-]?key|access_token|refresh_token|password)([=:]\s*)[^\s,;]+", r"\1\2[REDACTED]", value)
    return value
