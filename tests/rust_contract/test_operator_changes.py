"""Operator changes to bots, routes and deliveries (v0.5.28)."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

import pytest

from test_delivery_operations import BODY, cli, insert_event
from test_gateway_contract import FakeTelegram, Gateway, _fake_token, seed_home

OWNER = "777001"


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory(prefix="pylon-ops-") as directory:
        yield Path(directory) / "home"


def setup(home, telegram, routes=None):
    tokens = seed_home(
        home, telegram, [("main_bot", OWNER), ("spare_bot", OWNER), ("other_bot", "777002")], [],
        routes if routes is not None else {"personal": "main_bot", "trading": "main_bot"},
    )
    with Gateway(home, telegram):
        pass
    return tokens


def config(home):
    return json.loads((home / "config/bots.json").read_text())


def clean(result, tokens):
    output = result.stdout + result.stderr
    assert OWNER not in output and "777002" not in output
    assert BODY not in output
    assert all(token.split(":", 1)[1] not in output for token in tokens.values())
    return output


def value(result):
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def backups(home):
    root = home / "backups"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def test_dry_runs_write_nothing_and_mask_private_values(home, telegram):
    tokens = setup(home, telegram)
    before = (home / "config/bots.json").read_bytes()
    commands = [
        ["bots", "disable", "main_bot", "--reroute-to", "spare_bot", "--dry-run"],
        ["bots", "remove", "main_bot", "--reroute-to", "spare_bot", "--dry-run"],
        ["bots", "enable", "spare_bot", "--dry-run"],
        ["bots", "rotate", "main_bot", "--dry-run"],
        ["bots", "add", "new_bot", "--owner", "123456", "--dry-run"],
        ["bots", "prune-state", "--dry-run"],
        ["bots", "menu-resync", "--all", "--dry-run"],
        ["routes", "set", "personal", "spare_bot", "--dry-run"],
        ["routes", "unset", "trading", "--dry-run"],
        ["routes", "override", "personal", "--to", "spare_bot", "--for", "30m", "--dry-run"],
        ["deliveries", "defer", "main_bot", "--for", "10m", "--dry-run"],
        ["deliveries", "purge-bot", "main_bot", "--discard", "--dry-run"],
    ]
    for command in commands:
        result = cli(home, *command)
        output = clean(result, tokens)
        assert "123456" not in output
        preview = value(result)
        assert preview["dry_run"] is True and preview["backup"] is None, command
    remove = value(cli(home, "bots", "remove", "main_bot", "--reroute-to", "spare_bot", "--dry-run"))
    assert remove["credentials"] == {"write": 0, "move_to_backup": 1}
    assert any(line.startswith('-     "main_bot"') for line in remove["config_diff"])
    assert remove["rerouted_topics"] == ["personal", "trading"]
    add = value(cli(home, "bots", "add", "new_bot", "--owner", "123456", "--dry-run"))
    assert '+       "owner": "***",' in add["config_diff"]
    assert (home / "config/bots.json").read_bytes() == before
    assert backups(home) == []
    assert not (home / "logs/ops.jsonl").exists()


def test_disable_refuses_routed_bot_and_reroutes_on_request(home, telegram):
    tokens = setup(home, telegram)
    before = (home / "config/bots.json").read_bytes()
    refused = cli(home, "bots", "disable", "main_bot")
    assert refused.returncode != 0
    assert "pass --reroute-to" in refused.stderr
    assert json.loads(refused.stdout)["routed_topics"] == ["personal", "trading"]
    assert (home / "config/bots.json").read_bytes() == before

    done = value(cli(home, "bots", "disable", "main_bot", "--reroute-to", "spare_bot"))
    clean(cli(home, "bots", "list"), tokens)
    current = config(home)
    assert current["bots"]["main_bot"]["enabled"] is False
    assert current["routes"] == {"personal": "spare_bot", "trading": "spare_bot"}
    assert "delivery" not in current and "route_overrides" not in current
    backup = home / "backups" / done["backup"].rsplit("/", 1)[1]
    assert backup.name.startswith("gateway-ops-") and backup.name.endswith("-bots-disable")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o700
    assert (backup / "bots.json").read_bytes() == before
    assert stat.S_IMODE((backup / "bots.json").stat().st_mode) == 0o600
    audit = (home / "logs/ops.jsonl").read_text().splitlines()
    assert json.loads(audit[-1])["op"] == "bots-disable" and OWNER not in audit[-1]

    restored = value(cli(home, "ops", "restore", str(backup)))
    assert restored["ok"] is True
    assert (home / "config/bots.json").read_bytes().strip() != b""
    assert config(home)["routes"]["personal"] == "main_bot"
    assert config(home)["bots"]["main_bot"]["enabled"] is True
    outside = cli(home, "ops", "restore", str(home / "config"))
    assert outside.returncode != 0 and "invalid backup directory" in outside.stderr


def test_remove_moves_credential_and_pending_work(home, telegram):
    tokens = setup(home, telegram, {"personal": "main_bot"})
    insert_event(home, "history", "main_bot", "delivered", [{"message_id": 3, "chat_id": int(OWNER)}])
    insert_event(home, "queued-a", "main_bot", "pending")
    insert_event(home, "queued-b", "main_bot", "pending")
    secret = home / "gateway/secrets/bot-main_bot"
    token = secret.read_text()

    missing = cli(home, "bots", "remove", "main_bot", "--reroute-to", "spare_bot")
    assert missing.returncode != 0 and "--pending retarget" in missing.stderr
    wrong = cli(home, "bots", "remove", "main_bot", "--reroute-to", "spare_bot", "--pending", "retarget", "--to", "other_bot")
    assert wrong.returncode != 0 and "different chat" in wrong.stderr
    clean(wrong, tokens)
    assert secret.exists() and backups(home) == []

    removed = cli(
        home, "bots", "remove", "main_bot", "--reroute-to", "spare_bot",
        "--pending", "retarget", "--to", "spare_bot", "--purge-state",
    )
    clean(removed, tokens)
    result = value(removed)
    assert result["database"]["retargeted"] == 2
    assert "main_bot" not in config(home)["bots"]
    assert config(home)["routes"] == {"personal": "spare_bot"}
    assert not secret.exists()
    backup = Path(result["backup"])
    assert (backup / "secrets/bot-main_bot").read_text() == token
    assert stat.S_IMODE((backup / "secrets/bot-main_bot").stat().st_mode) == 0o600
    with sqlite3.connect(home / "gateway/state.sqlite3") as database:
        profiles = dict(database.execute("SELECT id,profile FROM events WHERE id LIKE 'queued-%'"))
        connection = database.execute("SELECT count(*) FROM kv WHERE key LIKE '%main_bot%' AND key NOT LIKE 'delivery-resolution:%'").fetchone()[0]
    assert profiles == {"queued-a": "spare_bot", "queued-b": "spare_bot"}
    assert connection == 0
    plan = json.loads((backup / "db-plan.json").read_text())
    assert {row["id"] for row in plan["deliveries"]} == {"queued-a", "queued-b"}

    restored = value(cli(home, "ops", "restore", str(backup)))
    assert restored["database_not_reverted"] is True
    assert secret.read_text() == token and "main_bot" in config(home)["bots"]


def test_prune_state_keeps_history_and_shared_rows(home, telegram):
    tokens = setup(home, telegram)
    now = time.time()
    with sqlite3.connect(home / "gateway/state.sqlite3") as database:
        for key in [
            "connection:gone_bot", "menu:gone_bot", "menu-state:gone_bot", "offset:gone_bot",
            "bot_started:gone_bot", "worker:gone_bot:poll", "delivery-timing:gone_bot:telegram",
            "event-gone-msg:gone_bot:1", "uncertain:summary:2026-11-03", "delivery-resolution:x",
            "connection:main_bot",
        ]:
            database.execute("INSERT OR REPLACE INTO kv(key,value) VALUES(?,?)", (key, json.dumps({"at": now})))
        database.execute("INSERT INTO updates(profile,update_id,payload,status) VALUES('gone_bot',1,'{}','done')")
        database.execute("INSERT INTO updates(profile,update_id,payload,status) VALUES('gone_bot',2,'{}','pending')")
    preview = value(cli(home, "bots", "prune-state", "--dry-run"))
    assert preview["stale_bots"] == ["gone_bot"]
    assert sorted(preview["database"]["delete_kv"]) == sorted([
        "bot_started:gone_bot", "connection:gone_bot", "delivery-timing:gone_bot:telegram",
        "menu-state:gone_bot", "menu:gone_bot", "offset:gone_bot", "worker:gone_bot:poll",
    ])
    assert preview["updates"] == {"gone_bot": 1} and preview["updates_kept"] == {"gone_bot": 1}
    applied = cli(home, "bots", "prune-state")
    clean(applied, tokens)
    result = value(applied)
    assert result["database"]["kv_deleted"] == 7 and result["database"]["updates_deleted"] == 1
    with sqlite3.connect(home / "gateway/state.sqlite3") as database:
        keys = {row[0] for row in database.execute("SELECT key FROM kv")}
        updates = database.execute("SELECT status FROM updates WHERE profile='gone_bot'").fetchall()
    assert {"event-gone-msg:gone_bot:1", "uncertain:summary:2026-11-03", "delivery-resolution:x", "connection:main_bot"} <= keys
    assert not any(key.endswith(":gone_bot") or ":gone_bot:" in key and not key.startswith("event-gone") for key in keys)
    assert updates == [("pending",)]
    again = value(cli(home, "bots", "prune-state"))
    assert again["changed"] is False and again["backup"] is None


def test_routes_alias_overrides_and_deferral(home, telegram):
    tokens = setup(home, telegram)
    value(cli(home, "routes", "personal", "spare_bot"))
    rows = {row["topic"]: row for row in value(cli(home, "routes", "list"))["routes"]}
    assert rows["personal"]["effective"] == "spare_bot" and rows["trading"]["effective"] == "main_bot"

    needs_end = cli(home, "routes", "override", "trading", "--to", "spare_bot")
    assert needs_end.returncode != 0 and "--revert-when-healthy" in needs_end.stderr
    too_long = cli(home, "routes", "override", "trading", "--to", "spare_bot", "--for", "8d")
    assert too_long.returncode != 0 and "invalid duration" in too_long.stderr
    value(cli(home, "routes", "override", "trading", "--to", "spare_bot", "--for", "30m", "--reason", "drill"))
    override = config(home)["route_overrides"]["trading"]
    assert override["bot"] == "spare_bot" and override["reason"] == "drill"
    assert time.time() + 1700 < override["until"] <= time.time() + 1800
    rows = {row["topic"]: row for row in value(cli(home, "routes", "list"))["routes"]}
    assert rows["trading"]["effective"] == "spare_bot" and rows["trading"]["override"]["active"] is True
    value(cli(home, "routes", "clear", "trading"))
    assert "route_overrides" not in config(home)

    value(cli(home, "deliveries", "defer", "main_bot", "--for", "10m"))
    assert config(home)["delivery"]["paused"]["main_bot"]["until"] > time.time() + 500
    listed = {row["id"]: row for row in value(cli(home, "bots", "list"))["bots"]}
    assert listed["main_bot"]["paused_until"] is not None
    clean(cli(home, "bots", "list"), tokens)
    value(cli(home, "deliveries", "undefer", "main_bot"))
    assert "delivery" not in config(home)
    not_deferred = cli(home, "deliveries", "undefer", "main_bot")
    assert not_deferred.returncode != 0 and "not deferred" in not_deferred.stderr


def test_rotate_and_add_check_tokens_before_writing(home, telegram):
    tokens = setup(home, telegram)
    secret = home / "gateway/secrets/bot-main_bot"
    old = secret.read_text()
    prefix = old.split(":", 1)[0]
    reasonless = cli(home, "bots", "add", "new_bot", "--owner", "123", "--skip-getme")
    assert reasonless.returncode != 0
    other = _fake_token("unrelated", "999")
    mismatch = cli(home, "bots", "rotate", "main_bot", "--skip-getme", "--reason", "drill", "--token-file", "/dev/null")
    assert mismatch.returncode != 0
    token_file = home.parent / "token"
    token_file.write_text(other)
    token_file.chmod(0o600)
    wrong = cli(home, "bots", "rotate", "main_bot", "--skip-getme", "--reason", "drill", "--token-file", str(token_file))
    assert wrong.returncode != 0 and "prefix does not match" in wrong.stderr
    fresh = f"{prefix}:{'R' * 35}"
    token_file.write_text(fresh)
    rotated = cli(home, "bots", "rotate", "main_bot", "--skip-getme", "--reason", "drill", "--token-file", str(token_file))
    output = clean(rotated, tokens)
    assert fresh.split(":", 1)[1] not in output
    result = value(rotated)
    assert secret.read_text() == fresh
    assert (Path(result["backup"]) / "secrets/bot-main_bot").read_text() == old
    same = cli(home, "bots", "rotate", "main_bot", "--skip-getme", "--reason", "drill", "--token-file", str(token_file))
    assert same.returncode != 0 and "credential unchanged" in same.stderr
    token_file.unlink()
