"""Preserve user history before uninstalling Hermes; never copy discarded profiles."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

DISCARDED = {"weixin", "wechat", "quant"}
TABLES = ("schema_version", "sessions", "messages", "system_prompts", "session_model_usage")
DOCUMENTS = ("SOUL.md", "memories", "docs", "notes", "research", "workspace", "outputs", "plans", "projects")
ROOTS = {
    "default": Path("/srv/hermes"),
    "webops": Path("/srv/hermes/profiles/webops"),
    "legacy-root": Path("/root/.hermes"),
}


def readonly(path: Path):
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def discarded_metadata(value: dict, excluded: set[str]) -> bool:
    for key in ("id", "session_id", "parent_session_id"):
        if isinstance(value.get(key), str) and value[key] in excluded:
            return True
    for key in ("source", "platform", "profile_name", "transport_profile", "created_source", "handoff_platform"):
        if str(value.get(key, "")).lower() in DISCARDED:
            return True
    if set(str(value.get("session_key", "")).lower().split(":")) & DISCARDED:
        return True
    origin = value.get("origin_json")
    if isinstance(origin, str) and origin:
        parsed = json.loads(origin)
        if isinstance(parsed, dict) and discarded_metadata(parsed, excluded):
            return True
    return False


def excluded_sessions(roots: list[Path], discarded_roots: list[Path]) -> set[str]:
    rows = []
    excluded = set()
    for root in roots + discarded_roots:
        with closing(readonly(root / "state.db")) as db:
            records = [dict(row) for row in db.execute("SELECT * FROM sessions")]
        rows.extend(records)
        if root in discarded_roots:
            excluded.update(row["id"] for row in records)
    while True:
        found = {row["id"] for row in rows if discarded_metadata(row, excluded)}
        if found <= excluded:
            return excluded
        excluded.update(found)


def row_digest(rows) -> tuple[int, str]:
    digest, count = hashlib.sha256(), 0
    for row in rows:
        encoded = json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":"),
                             default=lambda value: {"bytes": value.hex()}).encode()
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        count += 1
    return count, digest.hexdigest()


def archive_database(source: Path, destination: Path, excluded: set[str]) -> dict:
    # Copy selected rows into a new DB, so deleted rows and FTS/freelist pages
    # from Weixin/quant never enter the retained history file.
    with closing(readonly(source)) as src, closing(sqlite3.connect(destination)) as dst:
        keep = [row[0] for row in src.execute("SELECT id FROM sessions ORDER BY id") if row[0] not in excluded]
        for db in (src, dst):
            db.execute("CREATE TEMP TABLE retained_sessions(id TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO retained_sessions VALUES(?)", ((value,) for value in keep))
        queries = {
            "schema_version": "SELECT * FROM schema_version ORDER BY version",
            "sessions": "SELECT * FROM sessions WHERE id IN retained_sessions ORDER BY id",
            "messages": "SELECT * FROM messages WHERE session_id IN retained_sessions ORDER BY id",
            "system_prompts": "SELECT * FROM system_prompts WHERE hash IN (SELECT system_prompt_hash FROM sessions WHERE id IN retained_sessions) ORDER BY hash",
            "session_model_usage": "SELECT * FROM session_model_usage WHERE session_id IN retained_sessions ORDER BY session_id,model,billing_provider,billing_base_url,billing_mode,task",
        }
        copied = {}
        for table in TABLES:
            schema = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not schema:
                continue
            dst.execute(schema[0])
            query = queries[table]
            cursor = src.execute(query)
            placeholders = ",".join("?" for _ in cursor.description)
            dst.executemany(f"INSERT INTO {table} VALUES({placeholders})", cursor)
            before, after = row_digest(src.execute(query)), row_digest(dst.execute(query))
            if before != after:
                raise RuntimeError(f"history rows changed: {table}")
            copied[table] = before[0]
        for table in TABLES:
            for row in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,)):
                dst.execute(row[0])
        dst.commit()
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("history database integrity check failed")
    destination.chmod(0o600)
    return copied


def session_file(source: Path, excluded: set[str]) -> bytes | None:
    data = source.read_bytes()
    if any(ident in source.name for ident in excluded):
        return None
    if source.name == "sessions.json":
        entries = json.loads(data)
        if not isinstance(entries, dict):
            raise ValueError("unexpected session index format")
        retained = {}
        for key, value in entries.items():
            if set(key.lower().split(":")) & DISCARDED:
                continue
            if isinstance(value, str) and value in excluded:
                continue
            if isinstance(value, dict) and discarded_metadata(value, excluded):
                continue
            retained[key] = value
        return data if retained == entries else json.dumps(retained, ensure_ascii=False, indent=2).encode()
    if source.suffix == ".json":
        records = [json.loads(data)]
    elif source.suffix == ".jsonl":
        records = [json.loads(line) for line in data.splitlines() if line.strip()]
    else:
        raise ValueError(f"unclassified session file: {source.name}")
    if any(isinstance(row, dict) and discarded_metadata(row, excluded) for row in records):
        return None
    return data


def copy_checked(source: Path, destination: Path, content: bytes | None = None):
    if source.is_symlink():
        raise ValueError(f"history symlink requires review: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    expected = source.read_bytes() if content is None else content
    destination.write_bytes(expected)
    destination.chmod(0o600)
    if destination.read_bytes() != expected:
        raise RuntimeError(f"history file changed: {source.name}")


def archive_profile(source: Path, destination: Path, excluded: set[str]) -> dict:
    if destination.exists():
        raise FileExistsError(f"history already preserved: {destination}")
    stage = destination.with_name("." + destination.name + ".staging")
    stage.mkdir(mode=0o700)
    result = {"tables": archive_database(source / "state.db", stage / "state.sqlite3", excluded),
              "session_files": 0, "excluded_session_files": 0, "documents": 0}
    for path in sorted((source / "sessions").iterdir()):
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"unclassified history path: {path}")
        data = session_file(path, excluded)
        if data is None:
            result["excluded_session_files"] += 1
            continue
        copy_checked(path, stage / "sessions" / path.name, data)
        result["session_files"] += 1
    for name in DOCUMENTS:
        path = source / name
        files = sorted(path.rglob("*")) if path.is_dir() else [path]
        for child in files:
            if child.is_file() and not child.name.endswith(".lock"):
                copy_checked(child, stage / "documents" / child.relative_to(source))
                result["documents"] += 1
    os.replace(stage, destination)
    return result


def main():
    for service in ("hermes-default.service", "hermes-webops.service", "hermes-quant.service"):
        pid = subprocess.check_output(["systemctl", "show", service, "--property=MainPID", "--value"], text=True).strip()
        if pid != "0":
            raise RuntimeError(f"old gateway still running: {service}")
    excluded = excluded_sessions(list(ROOTS.values()), [Path("/srv/hermes/profiles/quant")])
    target = Path("/srv/pylon/history/hermes")
    target.mkdir(mode=0o700)
    for name, source in ROOTS.items():
        result = archive_profile(source, target / name, excluded)
        print(json.dumps({"profile": name, **result}, ensure_ascii=False))
    print(json.dumps({"excluded_session_ids": len(excluded)}))


if __name__ == "__main__":
    main()
