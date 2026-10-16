"""Black-box diagnostics contracts against a large isolated run history."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from test_gateway_contract import (
    Gateway,
    FakeTelegram,
    assert_rpc_rejected,
    open_context,
    rpc,
    rpc_value,
    seed_home,
)


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory(prefix="pylon-diagnostics-", dir="/tmp") as directory:
        yield Path(directory)


def _grant_diagnostics(home: Path, module_id: str, capabilities: list[str]) -> None:
    path = home / "modules" / module_id / "module.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["capabilities"] = capabilities
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_run_history(home: Path) -> int:
    jobs = (
        "fixture-old-latest",
        "fixture-new-failed",
        "fixture-unknown",
        "fixture-history",
    )
    count = 65_536
    rows = (
        (f"history-{index:06d}", jobs[index % len(jobs)], float(index + 1), float(index + 1), "ok", "")
        for index in range(count)
    )
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=10) as database:
        database.executemany(
            "INSERT INTO runs(id,job_id,started_at,finished_at,status,detail) VALUES(?,?,?,?,?,?)",
            rows,
        )
        database.executemany(
            "INSERT INTO runs(id,job_id,started_at,finished_at,status,detail) VALUES(?,?,?,?,?,?)",
            [
                ("fixture-old-failed", "fixture-old-latest", 70_000.0, 70_001.0, "failed", "fixture"),
                ("fixture-latest-ok", "fixture-old-latest", 70_002.0, 70_003.0, "ok", "fixture"),
                ("fixture-new-failed", "fixture-new-failed", 70_004.0, 70_005.0, "failed", "fixture"),
                ("fixture-unknown", "fixture-unknown", 70_006.0, 70_007.0, "unknown", "fixture"),
            ],
        )
        database.commit()
    return count + 4


def _diagnostics_call(home: Path, barrier: threading.Barrier) -> tuple[dict, float]:
    barrier.wait(timeout=2)
    started = time.monotonic()
    result = rpc(home, "diagnostics")
    return result, time.monotonic() - started


def _health_call(home: Path, barrier: threading.Barrier) -> dict:
    barrier.wait(timeout=2)
    return rpc_value(home, "health")


def _release_poll_call(barrier: threading.Barrier, release_poll: threading.Event) -> None:
    barrier.wait(timeout=2)
    release_poll.set()


def test_diagnostics_handles_large_history_and_keeps_gateway_workers_available(workdir, telegram):
    home = workdir / "home"
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [
            {"id": "ordinary", "topic": "topic_alpha", "job_id": "job_ordinary"},
            {"id": "observer", "topic": "topic_alpha", "job_id": "job_observer"},
        ],
        {"topic_alpha": "bot_one"},
    )
    _grant_diagnostics(home, "observer", ["diagnostics:read"])
    poll_started = threading.Event()
    release_poll = threading.Event()
    original_result = telegram._result

    def hold_first_poll(token, method, body):
        if method == "getUpdates" and not poll_started.is_set():
            poll_started.set()
            release_poll.wait(timeout=5)
        return original_result(token, method, body)

    telegram._result = hold_first_poll

    with Gateway(home, telegram):
        try:
            assert poll_started.wait(timeout=2), "gateway did not start Telegram polling"
            inserted = _seed_run_history(home)

            barrier = threading.Barrier(4)
            with ThreadPoolExecutor(max_workers=3) as workers:
                diagnostics_future = workers.submit(_diagnostics_call, home, barrier)
                health_future = workers.submit(_health_call, home, barrier)
                release_future = workers.submit(_release_poll_call, barrier, release_poll)
                barrier.wait(timeout=2)
                response, elapsed = diagnostics_future.result(timeout=2.5)
                health = health_future.result(timeout=2.5)
                release_future.result(timeout=2.5)

            assert response["ok"] is True, response
            assert elapsed < 2.0, f"HTTP diagnostics exceeded its 2s boundary: {elapsed:.3f}s"
            value = response["value"]
            assert health["api_version"] == 2
            assert value["maintenance"] is False
            assert len(value["latest_runs"]) == 4

            latest = {row["job_id"]: row for row in value["latest_runs"]}
            assert latest["fixture-old-latest"]["id"] == "fixture-latest-ok"
            assert latest["fixture-old-latest"]["status"] == "ok"
            assert latest["fixture-new-failed"]["id"] == "fixture-new-failed"
            assert latest["fixture-new-failed"]["status"] == "failed"
            assert latest["fixture-unknown"]["id"] == "fixture-unknown"
            assert latest["fixture-unknown"]["status"] == "unknown"
            failures = {row["id"] for row in value["recent_failures"]}
            assert {"fixture-old-failed", "fixture-new-failed"} <= failures

            ordinary = open_context(home, "ordinary", "job_ordinary")["token"]
            denied = rpc(home, "diagnostics", context=ordinary)
            assert_rpc_rejected(denied)

            observer = open_context(home, "observer", "job_observer")["token"]
            allowed = rpc(home, "diagnostics", context=observer)
            assert allowed["ok"] is True, allowed
            assert len(allowed["value"]["latest_runs"]) == 4

            after_polls = telegram.count(tokens["bot_one"], "getUpdates")
            assert after_polls >= 1
            print(
                f"diagnostics_elapsed={elapsed:.3f}s runs={inserted} "
                f"latest={len(value['latest_runs'])} recent_failures={len(value['recent_failures'])} "
                f"telegram_polls={after_polls}"
            )
        finally:
            release_poll.set()
