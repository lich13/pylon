"""The former CHH category workflow is fully retired."""

import importlib.util
import json
import sqlite3

from pylon import commands
from pylon.outbox import Outbox
from pylon.tasks.marketplace import control
from pylon.tasks.marketplace.history import history_posts
from pylon.tasks.marketplace.store import Store


def test_retired_module_is_absent_and_historical_reader_has_a_new_owner(tmp_path):
    assert importlib.util.find_spec("pylon.tasks.chiphell.categories") is None
    queue = tmp_path / "delivery.sqlite3"
    with sqlite3.connect(queue) as db:
        db.execute("CREATE TABLE delivery_queue (dedupe_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER)")
        db.execute("INSERT INTO delivery_queue VALUES (?,?,?,?)", (
            "synthetic", json.dumps({"text": "## 交易\n1. [[上海]iPhone 15](https://example.invalid/thread-7) 摘要：全新"}), 1, None,
        ))
    before = queue.stat().st_mtime_ns
    assert history_posts(queue) == [{
        "title": "[上海]iPhone 15", "url": "https://example.invalid/thread-7", "body_excerpt": "全新",
    }]
    assert queue.stat().st_mtime_ns == before


def test_old_ch1_ch2_callbacks_are_inert_and_do_not_create_policy_or_events(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_HOME", str(tmp_path))
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "outbox.sqlite3"))
    box = Outbox(tmp_path / "outbox.sqlite3")
    Store(box)
    box.put("chiphell:unknown-review:fixture", {"status": "asked"})
    with box.connect() as db:
        control.baseline(db)
        before = list(db.iterdump())
    monkeypatch.setattr(commands, "profile_env", lambda _: {"TELEGRAM_HOME_CHANNEL": "123"})
    payloads = (
        {"data": "ch1|mute|phone_apple|retired", "from": {"id": 123},
         "message": {"message_id": 88, "chat": {"id": 123, "type": "private"}}},
        {"data": "ch2|ignore|unknown-item|retired", "from": {"id": 123},
         "message": {"message_id": 89, "chat": {"id": 123, "type": "private"}}},
    )
    for profile in ("default", "webops"):
        for payload in payloads:
            assert "旧 CHH 分类入口已移除" in commands.handle_callback(profile, payload)
    assert box.counts() == {}
    with box.connect() as db:
        assert list(db.iterdump()) == before
