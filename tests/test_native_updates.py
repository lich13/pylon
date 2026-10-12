"""Gateway tests for the fixed Codex/Grok native update boundary."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from pylon import software_updates as updates
from pylon.admin import software as admin
from pylon.outbox import Outbox


@pytest.fixture
def root_db(tmp_path, monkeypatch):
    """Use a private root state directory and never invoke systemctl."""
    monkeypatch.setattr(admin, "ROOT", tmp_path / "privileged")
    launch = Mock(return_value="")
    monkeypatch.setattr(admin, "capture", launch)
    monkeypatch.setattr(admin, "active", lambda _: False)
    return launch


def get_run(run_id: str) -> dict:
    with admin.database() as db:
        return dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def test_duplicate_requests_merge_across_profiles_and_survive_reopen(root_db):
    first = admin.start("codex", "default", "telegram:default:1")
    second = admin.start("codex", "webops", "telegram:webops:2")

    assert first["id"] == second["id"]
    assert admin.start("codex", "default", "telegram:default:1")["id"] == first["id"]
    root_db.assert_called_once()

    admin.finish(first["id"], "updated", before="1.0.0", after="1.0.1")
    assert {row["profile"] for row in admin.results()} == {"default", "webops"}
    admin.main(["ack", "telegram:default:1"])
    assert [row["profile"] for row in admin.results()] == ["webops"]
    assert admin.start("codex", "default", "telegram:default:1")["status"] == "updated"
    root_db.assert_called_once()


def test_concurrent_requests_launch_one_worker(root_db):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(
            lambda number: admin.start("codex", "webops", "parallel:" + str(number)),
            range(8),
        ))

    assert len({row["id"] for row in rows}) == 1
    root_db.assert_called_once()


def test_request_update_queued_reply_says_checking(monkeypatch):
    call = Mock(return_value=json.dumps({
        "id": "run-1",
        "request_id": "request-1",
        "profile": "webops",
        "target": "codex",
        "status": "queued",
        "created_at": 1,
    }))
    monkeypatch.setattr(updates.scheduler, "admin", call)
    monkeypatch.setenv("PYLON_COMMAND_ID", "request-1")

    reply = updates.request_update("codex", "webops")

    assert "正在检查" in reply
    call.assert_called_once_with("software", "update", "codex", "webops", "request-1")


@pytest.mark.parametrize("installed_version", ["1.0.45", "1.0.46"])
def test_current_or_newer_stable_does_not_install(root_db, monkeypatch, installed_version):
    row = admin.start("grok", "webops", "current")
    monkeypatch.setattr(admin, "installed", Mock(return_value=installed_version))
    monkeypatch.setattr(admin, "latest", lambda *_: ("1.0.45", "official"))
    busy = Mock(side_effect=AssertionError("current versions must not enter install checks"))
    monkeypatch.setattr(admin, "busy_processes", busy)
    native = Mock(side_effect=AssertionError("current versions must not install"))
    monkeypatch.setattr(admin, "native_update", native)

    admin.execute("grok")

    result = get_run(row["id"])
    assert result["status"] == "current"
    assert result["after_version"] == installed_version
    native.assert_not_called()
    busy.assert_not_called()


def test_upstream_check_failure_does_not_install(root_db, monkeypatch):
    row = admin.start("grok", "webops", "upstream-failure")
    monkeypatch.setattr(admin, "installed", Mock(return_value="1.0.44"))
    monkeypatch.setattr(admin, "latest", Mock(side_effect=RuntimeError("HTTP 429")))
    native = Mock(side_effect=AssertionError("upstream failure must not install"))
    monkeypatch.setattr(admin, "native_update", native)

    admin.execute("grok")

    assert get_run(row["id"])["status"] == "failed"
    native.assert_not_called()


@pytest.mark.parametrize("error,expected", [
    (RuntimeError("systemctl unavailable"), "failed"),
    (subprocess.TimeoutExpired(["systemctl"], 15), "uncertain"),
])
def test_service_start_failure_is_recorded_without_replaying_install(root_db, error, expected):
    root_db.side_effect = error

    row = admin.start("codex", "default", "start-failure")

    assert row["status"] == expected
    assert get_run(row["id"])["status"] == expected
    assert admin.results()[0]["status"] == expected
    root_db.assert_called_once()


def test_start_failure_does_not_overwrite_worker_completion(root_db, monkeypatch):
    def launch_then_fail(_argv):
        with admin.database() as db:
            run_id = db.execute(
                "SELECT id FROM runs WHERE target='codex' AND status='queued'"
            ).fetchone()[0]
        admin.finish(run_id, "updated", before="1.0.0", after="1.0.1")
        raise RuntimeError("start reply lost after worker completed")

    monkeypatch.setattr(admin, "capture", launch_then_fail)

    row = admin.start("codex", "default", "start-race")

    assert row["status"] == "updated"
    assert get_run(row["id"])["status"] == "updated"


def test_session_occupancy_prevents_native_update(root_db, monkeypatch):
    row = admin.start("grok", "webops", "session-busy")
    monkeypatch.setattr(admin, "installed", Mock(return_value="1.0.44"))
    monkeypatch.setattr(admin, "latest", lambda *_: ("1.0.45", "official"))
    monkeypatch.setattr(admin, "busy_processes", Mock(return_value=[77]))
    native = Mock(side_effect=AssertionError("session occupancy must not install"))
    monkeypatch.setattr(admin, "native_update", native)

    admin.execute("grok")

    assert get_run(row["id"])["status"] == "busy"
    native.assert_not_called()


def test_install_lock_conflict_prevents_native_update(root_db, monkeypatch):
    row = admin.start("grok", "webops", "lock-busy")
    monkeypatch.setattr(admin, "installed", Mock(return_value="1.0.44"))
    monkeypatch.setattr(admin, "latest", lambda *_: ("1.0.45", "official"))
    monkeypatch.setattr(admin, "busy_processes", lambda _: [])
    native = Mock(side_effect=AssertionError("lock conflict must not install"))
    monkeypatch.setattr(admin, "native_update", native)

    lock_path = admin.ROOT / "install.lock"
    lock_path.touch()
    lock_stat = lock_path.stat()
    real_flock = admin.fcntl.flock

    def reject_install_lock(fd, operation):
        stat = os.fstat(fd.fileno() if hasattr(fd, "fileno") else fd)
        if operation & fcntl.LOCK_NB and (stat.st_dev, stat.st_ino) == (lock_stat.st_dev, lock_stat.st_ino):
            raise BlockingIOError("fixture install lock")
        return real_flock(fd, operation)

    monkeypatch.setattr(admin.fcntl, "flock", reject_install_lock)
    admin.execute("grok")

    assert get_run(row["id"])["status"] == "busy"
    native.assert_not_called()


def test_results_does_not_mark_worker_completion_uncertain(root_db, monkeypatch):
    row = admin.start("grok", "webops", "results-race")
    with admin.database() as db:
        db.execute(
            "UPDATE runs SET status='running',created_at=0 WHERE id=?",
            (row["id"],),
        )

    def active_then_finish(_target):
        admin.finish(row["id"], "updated", before="1.0.0", after="1.0.1")
        return False

    monkeypatch.setattr(admin, "active", active_then_finish)

    rows = admin.results()

    assert len(rows) == 1
    assert rows[0]["id"] == row["id"]
    assert rows[0]["status"] == "updated"


@pytest.mark.parametrize("after,exit_code,status", [
    ("1.0.45", 0, "updated"),
    ("1.0.46", 0, "updated"),
    ("1.0.44", 0, "failed"),
    ("1.0.45", 1, "failed"),
])
def test_success_requires_zero_exit_and_installed_version_at_least_target(
    root_db, monkeypatch, after, exit_code, status
):
    row = admin.start("grok", "webops", "version-check")
    monkeypatch.setattr(admin, "installed", Mock(side_effect=["1.0.44", after]))
    monkeypatch.setattr(admin, "latest", lambda *_: ("1.0.45", "official"))
    monkeypatch.setattr(admin, "busy_processes", lambda _: [])
    native = Mock(return_value=(exit_code, "installer output"))
    monkeypatch.setattr(admin, "native_update", native)

    admin.execute("grok")

    assert get_run(row["id"])["status"] == status
    admin.execute("grok")
    native.assert_called_once_with("grok")


def test_timeout_is_uncertain_and_never_replayed(root_db, monkeypatch):
    row = admin.start("grok", "webops", "native-timeout")
    monkeypatch.setattr(admin, "installed", Mock(side_effect=["1.0.44", "1.0.44"]))
    monkeypatch.setattr(admin, "latest", lambda *_: ("1.0.45", "official"))
    monkeypatch.setattr(admin, "busy_processes", lambda _: [])
    native = Mock(side_effect=TimeoutError("timeout"))
    monkeypatch.setattr(admin, "native_update", native)

    admin.execute("grok")
    assert get_run(row["id"])["status"] == "uncertain"

    admin.execute("grok")
    native.assert_called_once_with("grok")


@pytest.mark.parametrize("target,stable", [("codex", False), ("grok", True)])
def test_native_command_is_fixed_sudo_and_stable(monkeypatch, target, stable):
    process = Mock(returncode=0)
    process.communicate.return_value = ("ok", None)
    popen = Mock(return_value=process)
    monkeypatch.setattr(admin.subprocess, "Popen", popen)

    assert admin.native_update(target) == (0, "ok")

    expected = ["/usr/bin/sudo", "-n", "-H", "--", f"/usr/local/bin/{target}", "update"]
    if stable:
        expected.append("--stable")
    assert popen.call_args.args[0] == expected
    assert not popen.call_args.kwargs.get("shell")


def test_process_check_ignores_browser_and_unrelated_services(tmp_path):
    for pid, executable in [
        (21, "/root/.grok/downloads/grok-1.0.44-linux-x86_64"),
        (22, "/root/.grok/linux-runtime/chrome-linux64/chrome"),
        (23, "/root/.codex/packages/standalone/releases/version/bin/codex"),
        (24, "/usr/local/bin/nexushub-webd"),
    ]:
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / "exe").symlink_to(executable)

    assert admin.busy_processes("grok", tmp_path) == [21]
    assert admin.busy_processes("codex", tmp_path) == [23]


def _result_row(run_id: str, profile: str, request_id: str) -> dict:
    return {
        "id": run_id,
        "request_id": request_id,
        "profile": profile,
        "target": "codex",
        "status": "updated",
        "before_version": "1.0.0",
        "after_version": "1.0.1",
        "created_at": 1,
        "finished_at": 2,
    }


def test_reconcile_persists_before_ack_and_restart_does_not_duplicate(tmp_path, monkeypatch):
    box = Outbox(tmp_path / "outbox.db")
    row = _result_row("run-1", "webops", "request-1")
    ack_allowed = False

    def call(*args):
        nonlocal ack_allowed
        if args[1] == "results":
            return json.dumps([row])
        if not ack_allowed:
            raise RuntimeError("connection interrupted")
        return "{}"

    monkeypatch.setattr(updates.scheduler, "admin", call)
    with pytest.raises(RuntimeError):
        updates.reconcile(box)
    assert box.counts() == {"pending": 1}

    ack_allowed = True
    assert updates.reconcile(box) == 1
    assert box.counts() == {"pending": 1}


def test_reconcile_deduplicates_by_profile_and_run_id(tmp_path, monkeypatch):
    box = Outbox(tmp_path / "outbox.db")
    rows = [
        _result_row("run-1", "default", "request-default"),
        _result_row("run-1", "webops", "request-webops"),
    ]
    monkeypatch.setattr(
        updates.scheduler,
        "admin",
        lambda *args: json.dumps(rows) if args[1] == "results" else "{}",
    )

    assert updates.reconcile(box) == 2
    assert updates.reconcile(box) == 2
    with box.connect() as db:
        events = [dict(row) for row in db.execute(
            "SELECT profile,payload FROM events WHERE job_id='software-update' ORDER BY profile"
        )]
    assert [row["profile"] for row in events] == ["default", "webops"]
    assert len(events) == 2
