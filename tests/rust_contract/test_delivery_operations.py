"""Operator delivery resolution and per-bot online checks (v0.5.26)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from test_gateway_contract import BIN, ROOT, FakeTelegram, Gateway, event_status, seed_home, wait_for

BODY = "fixture private body"


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory(prefix="pylon-deliveries-") as directory:
        yield Path(directory) / "home"


def cli(home: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BIN), "--home", str(home), "--offline", *args],
        cwd=ROOT,
        env=env or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def insert_uncertain(home: Path, event_id: str) -> None:
    now = time.time()
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        database.execute(
            "INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,receipt,error,module)"
            " VALUES(?,?,?,?,?,'uncertain',1,0,?,?,?,?,?)",
            (
                event_id,
                "fixture_bot",
                "fixture-job",
                "text",
                json.dumps({"text": BODY}),
                now,
                now,
                json.dumps([{"message_id": 9, "chat_id": 777001}]),
                "Telegram 回执未确认",
                "fixture",
            ),
        )


def test_operator_resolutions_are_metadata_only_and_bounded(home, telegram):
    seed_home(home, telegram, [("fixture_bot", "101")], [], {})
    with Gateway(home, telegram):
        pass
    for event_id in ("operator-a", "operator-b"):
        insert_uncertain(home, event_id)

    listed = cli(home, "deliveries", "list", "--status", "uncertain")
    assert listed.returncode == 0, listed.stderr
    assert BODY not in listed.stdout and "777001" not in listed.stdout
    value = json.loads(listed.stdout)
    assert value["count"] == 2
    assert {row["id"] for row in value["deliveries"]} == {"operator-a", "operator-b"}
    assert all("payload" not in row and "receipt" not in row for row in value["deliveries"])

    refused = cli(home, "deliveries", "resend", "operator-a")
    assert refused.returncode != 0
    assert "accept-duplicate-risk" in refused.stderr

    confirmed = cli(home, "deliveries", "confirm", "operator-a")
    assert confirmed.returncode == 0, confirmed.stderr
    result = json.loads(confirmed.stdout)
    assert result["status"] == "delivered"
    assert result["resolution"]["resolution"] == "confirm"
    assert "777001" not in confirmed.stdout

    again = cli(home, "deliveries", "discard", "operator-a")
    assert again.returncode != 0
    assert "only uncertain deliveries can be resolved" in again.stderr

    resent = cli(home, "deliveries", "resend", "operator-b", "--accept-duplicate-risk")
    assert resent.returncode == 0, resent.stderr
    assert json.loads(resent.stdout)["resolution"]["resend_count"] == 1
    twice = cli(home, "deliveries", "resend", "operator-b", "--accept-duplicate-risk")
    assert twice.returncode != 0
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        status = database.execute("SELECT status FROM events WHERE id='operator-b'").fetchone()[0]
    assert status == "pending"


def test_bots_check_reports_every_bot_and_fails_on_any(home, telegram):
    tokens = seed_home(home, telegram, [("fixture_bot", "101"), ("other", "102")], [], {})
    env = os.environ.copy()
    env["PYLON_TEST_TELEGRAM"] = telegram.url
    result = cli(home, "bots", "check", "--online", env=env)
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 1
    by_id = {line["id"]: line for line in lines}
    assert by_id["fixture_bot"]["verified"] is True
    assert by_id["fixture_bot"]["error_class"] is None
    assert by_id["other"]["verified"] is False
    assert by_id["other"]["error_class"] == "username_mismatch"
    assert all(isinstance(line["elapsed_ms"], int) for line in lines)

    telegram.close()
    result = cli(home, "bots", "check", "--online", env=env)
    lines = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 1
    assert [line["error_class"] for line in lines] == ["connect", "connect"]
    output = result.stdout + result.stderr
    assert all(token.split(":", 1)[1] not in output for token in tokens.values())


def insert_event(home: Path, event_id: str, profile: str, status: str, receipt=None, created=None) -> None:
    now = created or time.time()
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        database.execute(
            "INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at,receipt,error,module)"
            " VALUES(?,?,?,?,?,?,0,0,?,?,?,NULL,?)",
            (
                event_id,
                profile,
                "fixture-job",
                "text",
                json.dumps({"text": f"{BODY} {event_id}"}),
                status,
                now,
                now,
                None if receipt is None else json.dumps(receipt),
                "fixture",
            ),
        )


def test_retarget_moves_removed_bot_backlog_and_delivers_once(home, telegram):
    seed_home(home, telegram, [("fixture_bot", "101"), ("elsewhere", "202")], [], {})
    with Gateway(home, telegram):
        pass
    insert_event(home, "history", "removed", "delivered", [{"message_id": 3, "chat_id": 101}])
    insert_event(home, "orphan-a", "removed", "pending", created=time.time() - 60)
    insert_event(home, "orphan-b", "removed", "pending")

    wrong_chat = cli(home, "deliveries", "retarget", "--from-bot", "removed", "--to", "elsewhere")
    assert wrong_chat.returncode != 0
    assert "different chat" in wrong_chat.stderr
    assert "101" not in wrong_chat.stderr + wrong_chat.stdout
    assert "202" not in wrong_chat.stderr + wrong_chat.stdout
    configured_source = cli(home, "deliveries", "retarget", "--from-bot", "elsewhere", "--to", "fixture_bot")
    assert configured_source.returncode != 0
    unknown_target = cli(home, "deliveries", "retarget", "orphan-a", "--to", "missing")
    assert unknown_target.returncode != 0

    moved = cli(home, "deliveries", "retarget", "--from-bot", "removed", "--to", "fixture_bot")
    assert moved.returncode == 0, moved.stderr
    assert BODY not in moved.stdout and "101" not in moved.stdout
    value = json.loads(moved.stdout)
    assert value["count"] == 2
    assert [row["id"] for row in value["deliveries"]] == ["orphan-a", "orphan-b"]
    assert all(row["profile"] == "fixture_bot" for row in value["deliveries"])
    assert all(row["resolution"]["retargeted_from"] == "removed" for row in value["deliveries"])
    assert all("receipt" not in row for row in value["deliveries"])

    with Gateway(home, telegram):
        for event_id in ("orphan-a", "orphan-b"):
            delivered = wait_for(lambda: event_status(home, event_id, "delivered"))
            assert delivered["receipt"][0]["chat_id"] == 101
    with telegram._lock:
        texts = [
            body.get("text", "").rstrip("\n")
            for _, method, body in telegram._calls
            if method == "sendMessage"
        ]
    for event_id in ("orphan-a", "orphan-b"):
        assert texts.count(f"{BODY} {event_id}") == 1
    again = cli(home, "deliveries", "retarget", "orphan-a", "--to", "fixture_bot")
    assert again.returncode != 0
