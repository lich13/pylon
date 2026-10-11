from __future__ import annotations

import os

from pylon.outbox import Outbox
from pylon.shared import model_execution
from pylon.shared.model_requests import emit


def rows(box, component):
    with box.connect() as db:
        return [dict(row) for row in db.execute(
            "SELECT * FROM model_executions WHERE component=?", (component,)
        )]


def test_component_flock_owns_live_work_even_when_heartbeat_is_old(tmp_path, monkeypatch):
    box = Outbox(tmp_path / "outbox.sqlite3")
    component = "fixture:analysis"

    with model_execution.Execution(box, component) as execution:
        assert execution is not None
        execution.track("v2ex", "42", "fixture-fingerprint")
        emit("start", request_id="fixture-request")

        # Heartbeat age is diagnostic data. It must not cause a live owner to
        # be reclaimed while its component-level flock is still held.
        monkeypatch.setattr(model_execution.time, "time", lambda: 100000.0)
        assert model_execution.active(box, component) is True
        with model_execution.Execution(box, component) as contender:
            assert contender is None

        current = rows(box, component)[0]
        assert current["status"] == "running"
        assert current["owner"] == execution.owner
        assert current["pid"] == os.getpid()
        assert current["source"] == "v2ex"
        assert current["source_id"] == "42"
        assert current["fingerprint"] == "fixture-fingerprint"
        assert current["request_id"] == "fixture-request"

    assert model_execution.active(box, component) is False
    assert rows(box, component)[0]["status"] == "complete"


def test_execution_marks_failed_and_allows_a_new_owner_after_exception(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    component = "fixture:failure"

    try:
        with model_execution.Execution(box, component) as execution:
            assert execution is not None
            raise RuntimeError("fixture failure")
    except RuntimeError:
        pass

    assert rows(box, component)[0]["status"] == "failed"
    with model_execution.Execution(box, component) as replacement:
        assert replacement is not None
        assert model_execution.active(box, component) is True
    assert rows(box, component)[-1]["status"] == "complete"


def test_old_running_row_without_a_live_lock_is_reclaimable(tmp_path, monkeypatch):
    box = Outbox(tmp_path / "outbox.sqlite3")
    component = "fixture:dead-owner"
    with box.connect() as db:
        db.execute(model_execution.SCHEMA)
        db.execute(
            "INSERT INTO model_executions VALUES(?,?,?,?,?,?,?,?,?,?)",
            (component, "dead-owner-token", 999999, "running", 1.0, 1.0, "", "", "", ""),
        )

    monkeypatch.setattr(model_execution.time, "time", lambda: 999999.0)
    assert model_execution.active(box, component) is False
    with model_execution.Execution(box, component) as execution:
        assert execution is not None
        assert rows(box, component)[0]["owner"] == execution.owner
        assert rows(box, component)[0]["status"] == "running"

