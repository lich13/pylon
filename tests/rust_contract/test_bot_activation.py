"""Black-box contracts for bots that require their owner's /start."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from test_gateway_contract import (
    FakeTelegram,
    event_row,
    event_status,
    open_context,
    rpc_value,
    running_gateway,
    seed_home,
    wait_for,
)


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory(prefix="pylon-activation-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


def _kv(home: Path, key: str) -> str | None:
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        row = database.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _submit_text(home: Path, context: str, event_id: str, text: str) -> dict:
    return rpc_value(
        home,
        "delivery.submit",
        {
            "id": event_id,
            "kind": "text",
            "job_id": "job_alpha",
            "topic": "topic_alpha",
            "payload": {"text": text},
        },
        context,
    )


def test_require_start_gates_pending_delivery_until_owner_start_and_survives_restart(
    workdir, telegram
):
    home = workdir / "home"
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    config_path = home / "config/bots.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["bots"]["bot_one"]["require_start"] = True
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")

    now = int(time.time())
    telegram.set_updates(
        tokens["bot_one"],
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 11,
                    "date": now,
                    "chat": {"id": 909, "type": "private"},
                    "from": {"id": 909, "is_bot": False},
                    "text": "/start",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 12,
                    "date": now,
                    "chat": {"id": -101, "type": "group"},
                    "from": {"id": 101, "is_bot": False},
                    "text": "/start",
                },
            },
        ],
    )

    with running_gateway(home, telegram) as gateway:
        context = open_context(home, "alpha", "job_alpha")["token"]
        prestart_text = "pending until owner's private start"
        submitted = _submit_text(home, context, "activation-before-start", prestart_text)
        assert submitted["profile"] == "bot_one"

        wait_for(lambda: _kv(home, "offset:bot_one") == "3")
        assert _kv(home, "bot_started:bot_one") is None
        pending = event_row(home, "activation-before-start")
        assert pending["status"] == "pending"
        assert pending["attempts"] == 0
        assert telegram.send_count(prestart_text + "\n") == 0

        telegram.set_updates(
            tokens["bot_one"],
            [
                {
                    "update_id": 3,
                    "message": {
                        "message_id": 13,
                        "date": now,
                        "chat": {"id": 101, "type": "private"},
                        "from": {"id": 101, "is_bot": False},
                        "text": "/start",
                    },
                },
            ],
        )

        wait_for(lambda: _kv(home, "bot_started:bot_one") == "true")
        delivered = wait_for(
            lambda: event_status(home, "activation-before-start", "delivered")
        )
        assert delivered["attempts"] == 1
        assert telegram.send_count(prestart_text + "\n") == 1

        gateway.stop()
        assert _kv(home, "bot_started:bot_one") == "true"
        gateway.start()
        assert _kv(home, "bot_started:bot_one") == "true"

        restarted_context = open_context(home, "alpha", "job_alpha")["token"]
        postrestart_text = "delivery after activation restart"
        _submit_text(home, restarted_context, "activation-after-restart", postrestart_text)
        restarted_delivery = wait_for(
            lambda: event_status(home, "activation-after-restart", "delivered")
        )
        assert restarted_delivery["attempts"] == 1
        assert telegram.send_count(postrestart_text + "\n") == 1
        assert telegram.send_count(prestart_text + "\n") == 1
