"""Black-box contracts for moving gateway-owned state out of the Python DB."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from test_gateway_contract import ROOT, run_cli


LEGACY_SCHEMA = """
CREATE TABLE events (
    id TEXT PRIMARY KEY, profile TEXT NOT NULL, job_id TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
    updated_at REAL NOT NULL, receipt TEXT, error TEXT
);
CREATE TABLE updates (
    profile TEXT NOT NULL, update_id INTEGER NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending', error TEXT,
    kind TEXT NOT NULL DEFAULT 'message', chat_id TEXT, message_id INTEGER,
    PRIMARY KEY(profile, update_id)
);
CREATE TABLE runs (
    id TEXT PRIMARY KEY, job_id TEXT NOT NULL, started_at REAL NOT NULL,
    finished_at REAL, status TEXT NOT NULL, detail TEXT
);
CREATE TABLE run_events (
    run_id TEXT NOT NULL, event_id TEXT NOT NULL,
    PRIMARY KEY(run_id, event_id)
);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE market_web_tokens (
    hash TEXT PRIMARY KEY, profile TEXT NOT NULL,
    expires_at REAL NOT NULL, used_at REAL
);
CREATE TABLE market_web_sessions (
    hash TEXT PRIMARY KEY, profile TEXT NOT NULL,
    csrf TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE market_web_applies (draft TEXT PRIMARY KEY, version INTEGER NOT NULL);
CREATE TABLE operations (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
    status TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE software_reviews (
    id TEXT PRIMARY KEY, target TEXT NOT NULL, package_name TEXT NOT NULL,
    installed_version TEXT NOT NULL, candidate_version TEXT NOT NULL,
    source TEXT NOT NULL, risk TEXT NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', request_id TEXT,
    event_id TEXT, detail TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    selected INTEGER NOT NULL DEFAULT 0, selection_epoch INTEGER NOT NULL DEFAULT 0,
    batch_id TEXT, available INTEGER NOT NULL DEFAULT 1
);
"""

BOT = "webops"
LOGIN_PROFILES = ("default", "webops")
OWNER = "101"
TASK_ID = "0123456789abcdef0123456789abcdef"
JOB_EVENT_ID = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TASK_EVENT_ID = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
BOT_EVENT_ID = "cccccccccccccccccccccccccccccccc"
RUN_ID = "fedcba9876543210fedcba9876543210"
AGENT_COMMAND_KEY = f"agent-command:telegram:{BOT}:72"
BUSINESS_TABLES = {"market_web_applies", "operations", "software_reviews"}


def _home(tmp_path: Path, *, freeze: bool = True) -> Path:
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    secrets_dir = home / "gateway/secrets"
    secrets_dir.mkdir(parents=True)
    (home / "run").mkdir()
    if freeze:
        (home / "run/freeze").touch()

    (home / "config/bots.json").write_text(
        json.dumps(
            {
                "bots": {
                    profile: {
                        "owner": OWNER,
                        "credential": profile,
                        "username": profile,
                        "enabled": True,
                        "menus": [],
                    }
                    for profile in LOGIN_PROFILES
                },
                "routes": {},
                "workspaces": {},
                "public_origin": "",
            }
        ),
        encoding="utf-8",
    )
    for profile in LOGIN_PROFILES:
        secret = secrets_dir / profile
        secret.write_text(
            f"101:fixtureCredential{profile}0123456789ABCDE", encoding="utf-8"
        )
        os.chmod(secret, 0o600)
    return home


def _legacy_events() -> list[tuple]:
    receipt = json.dumps(
        [{"chat_id": OWNER, "message_id": 901, "date": 1_780_000_001}],
        separators=(",", ":"),
    )
    return [
        (
            JOB_EVENT_ID,
            BOT,
            "external_reports_job",
            "document",
            json.dumps({"path": "fixture.pdf", "sha256": "a" * 64}),
            "delivered",
            2,
            1_780_000_000.5,
            1_780_000_000.0,
            1_780_000_001.0,
            receipt,
            None,
        ),
        (
            TASK_EVENT_ID,
            BOT,
            "bot",
            "text",
            json.dumps({"text": "task reply fixture", "agent_task": TASK_ID}),
            "uncertain",
            1,
            0.0,
            1_780_000_002.0,
            1_780_000_003.0,
            None,
            "sender restarted before acknowledgement",
        ),
        (
            BOT_EVENT_ID,
            BOT,
            "bot",
            "text",
            json.dumps({"text": "gateway reply fixture"}),
            "pending",
            0,
            0.0,
            1_780_000_004.0,
            1_780_000_004.0,
            None,
            None,
        ),
    ]


def _create_legacy_db(path: Path, *, unfinished_run: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.executemany(
            "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", _legacy_events()
        )
        db.execute(
            "INSERT INTO updates VALUES(?,?,?,?,?,?,?,?)",
            (
                BOT,
                72,
                json.dumps(
                    {
                        "update_id": 72,
                        "callback_query": {"id": "callback-fixture"},
                    }
                ),
                "done",
                None,
                "callback_query",
                OWNER,
                901,
            ),
        )
        db.execute(
            "INSERT INTO runs VALUES(?,?,?,?,?,?)",
            (
                RUN_ID,
                "external_reports_job",
                1_780_000_000.0,
                None if unfinished_run else 1_780_000_005.0,
                "running" if unfinished_run else "succeeded",
                json.dumps({"task_id": TASK_ID}),
            ),
        )
        db.executemany(
            "INSERT INTO run_events VALUES(?,?)",
            [(RUN_ID, JOB_EVENT_ID), (RUN_ID, TASK_EVENT_ID)],
        )
        db.executemany(
            "INSERT INTO kv VALUES(?,?)",
            [
                ("offset:" + BOT, json.dumps(73)),
                ("commands_not_before", json.dumps(1_780_000_100.0)),
                (AGENT_COMMAND_KEY, json.dumps(TASK_ID)),
            ],
        )
        db.executemany(
            "INSERT INTO market_web_tokens VALUES(?,?,?,?)",
            [
                (f"token-hash-{profile}", profile, 1_800_000_000.0, 1_780_000_010.0)
                for profile in LOGIN_PROFILES
            ],
        )
        db.executemany(
            "INSERT INTO market_web_sessions VALUES(?,?,?,?)",
            [
                (
                    f"session-hash-{profile}",
                    profile,
                    f"csrf-{profile}",
                    1_800_000_000.0,
                )
                for profile in LOGIN_PROFILES
            ],
        )
        db.execute("INSERT INTO market_web_applies VALUES(?,?)", ("draft-fixture", 7))
        db.execute(
            "INSERT INTO operations VALUES(?,?,?,?,?,?)",
            (
                "operation-fixture",
                "agent_task",
                json.dumps({"task_id": TASK_ID}),
                "started",
                1_780_000_000.0,
                1_780_000_001.0,
            ),
        )
        db.execute(
            """INSERT INTO software_reviews(
                   id,target,package_name,installed_version,candidate_version,
                   source,risk,fingerprint,profile,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "review-fixture",
                "desktop",
                "package-fixture",
                "1.0",
                "1.1",
                "catalog",
                "low",
                "fingerprint-fixture",
                BOT,
                1_780_000_000.0,
                1_780_000_001.0,
            ),
        )


def _mapping(path: Path, *, invalid_owner_field: bool = False) -> Path:
    field = "agent_task[0]" if invalid_owner_field else "agent_task"
    value = {
        "jobs": {"external_reports_job": "reports_module"},
        "event_payload_owners": [
            {"job_id": "bot", "field": field, "module": "operations"}
        ],
        "legacy_login_workspace": "market",
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _snapshot(path: Path) -> tuple[set[str], dict[str, list[tuple]]]:
    with sqlite3.connect(path) as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        rows = {
            table: db.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            for table in sorted(tables)
        }
    return tables, rows


def _target_path(home: Path) -> Path:
    return home / "gateway/state.sqlite3"


def _initialize_target(home: Path) -> Path:
    target = _target_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(target) as db:
        db.executescript((ROOT / "rust/schema.sql").read_text(encoding="utf-8"))
    return target


def _migrate(home: Path, source: Path, mapping: Path):
    return run_cli(
        home,
        "migrate",
        "--source",
        str(source),
        "--mapping",
        str(mapping),
    )


def test_migration_requires_frozen_triggers_and_leaves_source_untouched(tmp_path):
    home = _home(tmp_path, freeze=False)
    source = tmp_path / "legacy.sqlite3"
    _create_legacy_db(source)
    mapping = _mapping(tmp_path / "mapping.json")
    source_before = _snapshot(source)

    result = _migrate(home, source, mapping)

    assert result.returncode != 0
    assert "freeze new triggers before state migration" in result.stderr
    assert not _target_path(home).exists()
    assert _snapshot(source) == source_before


def test_migration_refuses_unfinished_run_without_importing_rows(tmp_path):
    home = _home(tmp_path)
    source = tmp_path / "legacy.sqlite3"
    _create_legacy_db(source, unfinished_run=True)
    mapping = _mapping(tmp_path / "mapping.json")
    source_before = _snapshot(source)

    result = _migrate(home, source, mapping)

    assert result.returncode != 0
    assert "unfinished runs require reconciliation before migration" in result.stderr
    assert _snapshot(source) == source_before
    with sqlite3.connect(_target_path(home)) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM kv").fetchone()[0] == 0


@pytest.mark.parametrize("existing_state", ["complete", "events"])
def test_migration_refuses_to_overwrite_nonempty_target(tmp_path, existing_state):
    home = _home(tmp_path)
    source = tmp_path / "legacy.sqlite3"
    _create_legacy_db(source)
    mapping = _mapping(tmp_path / "mapping.json")
    source_before = _snapshot(source)
    target = _initialize_target(home)
    with sqlite3.connect(target) as db:
        if existing_state == "complete":
            db.execute("INSERT INTO kv VALUES(?,?)", ("migration:complete", "{}"))
        else:
            db.execute(
                """INSERT INTO events(
                       id,profile,job_id,kind,payload,status,attempts,next_attempt,
                       created_at,updated_at,receipt,error,module
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "dddddddddddddddddddddddddddddddd",
                    BOT,
                    "preexisting_job",
                    "text",
                    "{}",
                    "pending",
                    0,
                    0.0,
                    1_780_000_000.0,
                    1_780_000_000.0,
                    None,
                    None,
                    "gateway",
                ),
            )
    target_before = _snapshot(target)

    result = _migrate(home, source, mapping)

    assert result.returncode != 0
    assert _snapshot(source) == source_before
    assert _snapshot(target) == target_before
    if existing_state == "complete":
        assert "migration already completed; never overwrite live state" in result.stderr
    else:
        assert "target contains events; state merge required" in result.stderr


def test_migration_preserves_gateway_state_sessions_and_business_db_boundary(tmp_path):
    home = _home(tmp_path)
    source = tmp_path / "legacy.sqlite3"
    _create_legacy_db(source)
    mapping = _mapping(tmp_path / "mapping.json")
    source_before = _snapshot(source)

    result = _migrate(home, source, mapping)

    assert result.returncode == 0, result.stderr
    assert _snapshot(source) == source_before
    target = _target_path(home)
    with sqlite3.connect(target) as db:
        db.row_factory = sqlite3.Row
        events = {
            row["id"]: dict(row)
            for row in db.execute(
                """SELECT id,profile,job_id,kind,payload,status,attempts,next_attempt,
                          created_at,updated_at,receipt,error,module
                   FROM events"""
            )
        }
        legacy = {row[0]: row for row in _legacy_events()}
        for event_id, old in legacy.items():
            migrated = events[event_id]
            assert tuple(migrated[key] for key in (
                "id", "profile", "job_id", "kind", "payload", "status", "attempts",
                "next_attempt", "created_at", "updated_at", "receipt", "error",
            )) == old
        assert events[JOB_EVENT_ID]["module"] == "reports_module"
        assert events[TASK_EVENT_ID]["module"] == "operations"
        assert events[BOT_EVENT_ID]["module"] == "gateway"
        assert events[TASK_EVENT_ID]["status"] == "uncertain"
        assert json.loads(events[TASK_EVENT_ID]["payload"])["agent_task"] == TASK_ID

        assert [tuple(row) for row in db.execute("SELECT * FROM updates").fetchall()] == [
            (
                BOT,
                72,
                json.dumps(
                    {
                        "update_id": 72,
                        "callback_query": {"id": "callback-fixture"},
                    }
                ),
                "done",
                None,
                "callback_query",
                OWNER,
                901,
            )
        ]
        assert [tuple(row) for row in db.execute("SELECT * FROM runs").fetchall()] == [
            (
                RUN_ID,
                "external_reports_job",
                1_780_000_000.0,
                1_780_000_005.0,
                "succeeded",
                json.dumps({"task_id": TASK_ID}),
            )
        ]
        assert [
            tuple(row) for row in db.execute("SELECT * FROM run_events ORDER BY event_id")
        ] == [
            (RUN_ID, JOB_EVENT_ID),
            (RUN_ID, TASK_EVENT_ID),
        ]

        migrated_kv = dict(db.execute("SELECT key,value FROM kv"))
        assert migrated_kv["offset:" + BOT] == json.dumps(73)
        assert migrated_kv["commands_not_before"] == json.dumps(1_780_000_100.0)
        assert AGENT_COMMAND_KEY not in migrated_kv

        assert [
            tuple(row)
            for row in db.execute("SELECT * FROM web_tokens ORDER BY bot")
        ] == [
            (
                f"token-hash-{profile}",
                OWNER,
                profile,
                "market",
                1_800_000_000.0,
                1_780_000_010.0,
            )
            for profile in LOGIN_PROFILES
        ]
        assert [
            tuple(row)
            for row in db.execute("SELECT * FROM web_sessions ORDER BY bot")
        ] == [
            (
                f"session-hash-{profile}",
                OWNER,
                profile,
                f"csrf-{profile}",
                1_800_000_000.0,
            )
            for profile in LOGIN_PROFILES
        ]

    target_tables, _ = _snapshot(target)
    assert BUSINESS_TABLES.isdisjoint(target_tables)
    source_tables, source_rows = source_before
    assert BUSINESS_TABLES <= source_tables
    assert source_rows["operations"]
    assert source_rows["software_reviews"]
    assert source_rows["market_web_applies"]
    source_kv = dict(source_rows["kv"])
    assert source_kv[AGENT_COMMAND_KEY] == json.dumps(TASK_ID)


def test_migration_rejects_non_identifier_payload_owner_field(tmp_path):
    home = _home(tmp_path)
    source = tmp_path / "legacy.sqlite3"
    _create_legacy_db(source)
    mapping = _mapping(tmp_path / "mapping.json", invalid_owner_field=True)
    source_before = _snapshot(source)

    result = _migrate(home, source, mapping)

    assert result.returncode != 0
    assert _snapshot(source) == source_before
    with sqlite3.connect(_target_path(home)) as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM kv").fetchone()[0] == 0
