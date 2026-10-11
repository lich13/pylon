from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pylon.outbox import Outbox
from pylon.tasks.daily_review import feedback, store


ZONE = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def review_db(tmp_path):
    box = Outbox(tmp_path / "daily-review-feedback.sqlite3")
    with box.connect() as db:
        store.initialize(db)
        yield db, box


def make_entry(db, source_id: str, *, item_count: int = 2, body: str | None = None):
    snapshot_id = store.capture(
        db,
        "v2ex",
        source_id,
        lane="market",
        title=f"Feedback fixture {source_id}",
        body=body or f"Feedback body for {source_id}.",
        url=f"https://example.invalid/feedback/{source_id}",
        first_seen="2026-10-10T13:00:00+08:00",
    )
    items = [
        {
            "item": {"brand": "Fixture", "model": f"Model-{index}", "category": "fixture"},
            "decision": "push" if index == 0 else "hold",
        }
        for index in range(item_count)
    ]
    entry_id = store.record(
        db,
        "v2ex",
        source_id,
        lane="market",
        status="ready",
        result={"items": items, "categories": ["fixture"]},
        rule_version="v1",
        snapshot_id=snapshot_id,
    )
    decision_id = db.execute(
        "SELECT id FROM review_decisions WHERE entry_id=? ORDER BY id DESC LIMIT 1", (entry_id,)
    ).fetchone()[0]
    return entry_id, snapshot_id, decision_id


def feedback_value(*, actions=None, scope="item", scope_value="", reason="fixture reason"):
    return {
        "actions": actions or ["category", "facts"],
        "category": "fixture",
        "price": "10",
        "region": "CN",
        "reason": reason,
        "scope": scope,
        "scope_value": scope_value,
    }


def test_feedback_validation_requires_coherent_combinations_and_explicit_scope_value():
    with pytest.raises(ValueError, match="判断正确"):
        feedback.validate(feedback_value(actions=["correct", "category"]))
    with pytest.raises(ValueError, match="互相冲突"):
        feedback.validate(feedback_value(actions=["should_push", "not_interested"]))
    with pytest.raises(ValueError, match="扩大的适用范围"):
        feedback.validate(feedback_value(scope="brand"))

    default_item = feedback.validate(feedback_value(scope="item", scope_value="ignored"))
    assert default_item["scope"] == "item"
    assert default_item["scope_value"] == ""

    expanded = feedback.validate(feedback_value(scope="brand", scope_value="Fixture"))
    assert expanded["scope"] == "brand"
    assert expanded["scope_value"] == "Fixture"
    assert expanded["actions"] == ["category", "facts"]


def test_save_supports_batch_and_exact_item_location_with_default_item_scope(review_db):
    db, _ = review_db
    entry_id, snapshot_id, decision_id = make_entry(db, "batch-items", item_count=2)
    targets = [
        {"entry_id": entry_id, "item_key": f"{decision_id}:0", "snapshot_id": snapshot_id},
        {"entry_id": entry_id, "item_key": f"{decision_id}:1", "snapshot_id": snapshot_id},
    ]

    result = feedback.save(db, targets, feedback_value())

    assert len(result["saved"]) == 2
    assert {row["item_key"] for row in result["saved"]} == {
        f"{decision_id}:0",
        f"{decision_id}:1",
    }
    assert {row["snapshot_id"] for row in result["saved"]} == {snapshot_id}
    assert all(json.loads(row["value"])["scope"] == "item" for row in result["saved"])
    assert len(store.detail(db, entry_id)["drafts"]) == 2


def test_autosave_uses_optimistic_revision_and_discard_removes_only_current_draft(review_db):
    db, _ = review_db
    entry_id, snapshot_id, _ = make_entry(db, "autosave", item_count=1)
    target = {"entry_id": entry_id, "item_key": "", "snapshot_id": snapshot_id}

    first = feedback.save(db, [target], feedback_value(reason="device A"))["saved"][0]
    second = feedback.save(
        db,
        [{**target, "revision": first["revision"]}],
        feedback_value(reason="device B"),
    )["saved"][0]
    assert first["revision"] == 1
    assert second["revision"] == 2

    with pytest.raises(feedback.Conflict, match="另一设备"):
        feedback.save(
            db,
            [{**target, "revision": first["revision"]}],
            feedback_value(reason="stale device A"),
        )

    assert feedback.discard(db, [{"id": second["id"], "revision": second["revision"]}]) == {"discarded": 1}
    assert store.detail(db, entry_id)["drafts"] == []


def test_submit_is_idempotent_once_and_freezes_the_displayed_snapshot(review_db):
    db, _ = review_db
    entry_id, snapshot_id, _ = make_entry(db, "submit-freeze", item_count=1, body="original body")
    target = {"entry_id": entry_id, "item_key": "", "snapshot_id": snapshot_id}
    saved = feedback.save(db, [target], feedback_value(reason="before submit"))["saved"][0]
    submission = feedback.submit(db, [{"id": saved["id"], "revision": saved["revision"]}])
    repeated = feedback.submit(db, [{"id": saved["id"], "revision": saved["revision"]}])

    assert submission["status"] == "submitted"
    assert submission["count"] == 1
    assert repeated == {
        "batch_id": submission["batch_id"],
        "status": "submitted",
        "repeated": True,
    }
    assert db.execute("SELECT count(*) FROM review_batches").fetchone()[0] == 1
    row = db.execute("SELECT batch_id,value FROM review_feedback WHERE id=?", (saved["id"],)).fetchone()
    frozen = json.loads(row["value"])["snapshot"]
    assert row["batch_id"] == submission["batch_id"]
    assert frozen["body"] == "original body"
    assert frozen["snapshots"][0]["data"]["body"] == "original body"

    store.capture(
        db,
        "v2ex",
        "submit-freeze",
        lane="market",
        title="Feedback fixture submit-freeze",
        body="changed body after submission",
        url="https://example.invalid/feedback/submit-freeze",
        first_seen="2026-10-10T13:00:00+08:00",
        fingerprint="changed-fingerprint",
    )
    assert json.loads(db.execute("SELECT value FROM review_feedback WHERE id=?", (saved["id"],)).fetchone()[0])["snapshot"]["body"] == "original body"


def test_submit_rejects_a_draft_when_original_snapshot_changed(review_db):
    db, _ = review_db
    entry_id, snapshot_id, _ = make_entry(db, "submit-conflict", item_count=1, body="first source body")
    target = {"entry_id": entry_id, "item_key": "", "snapshot_id": snapshot_id}
    saved = feedback.save(db, [target], feedback_value())["saved"][0]

    store.capture(
        db,
        "v2ex",
        "submit-conflict",
        lane="market",
        title="Feedback fixture submit-conflict",
        body="second source body",
        url="https://example.invalid/feedback/submit-conflict",
        first_seen="2026-10-10T13:00:00+08:00",
        fingerprint="new-source-fingerprint",
    )

    with pytest.raises(feedback.Conflict, match="已更新的原文"):
        feedback.submit(db, [{"id": saved["id"], "revision": saved["revision"]}])
    assert db.execute("SELECT count(*) FROM review_batches").fetchone()[0] == 0


def test_prune_removes_old_unreferenced_rows_but_protects_unresolved_feedback(review_db):
    db, _ = review_db
    old_stamp = datetime(2026, 6, 1, tzinfo=ZONE).timestamp()
    now = datetime(2026, 10, 11, tzinfo=ZONE).timestamp()

    disposable, _, _ = make_entry(db, "old-disposable", item_count=1)
    draft_entry, draft_snapshot, _ = make_entry(db, "old-draft", item_count=1)
    draft_target = {"entry_id": draft_entry, "item_key": "", "snapshot_id": draft_snapshot}
    feedback.save(db, [draft_target], feedback_value())

    pending_entry, pending_snapshot, _ = make_entry(db, "old-pending", item_count=1)
    pending_target = {"entry_id": pending_entry, "item_key": "", "snapshot_id": pending_snapshot}
    pending_saved = feedback.save(db, [pending_target], feedback_value())["saved"][0]
    pending_batch = feedback.submit(db, [{"id": pending_saved["id"], "revision": pending_saved["revision"]}])
    feedback.update_batch(db, pending_batch["batch_id"], status="needs_info", result={"reason": "waiting"})

    fixed_entry, fixed_snapshot, _ = make_entry(db, "old-fixed", item_count=1)
    fixed_target = {"entry_id": fixed_entry, "item_key": "", "snapshot_id": fixed_snapshot}
    fixed_saved = feedback.save(db, [fixed_target], feedback_value())["saved"][0]
    fixed_batch = feedback.submit(db, [{"id": fixed_saved["id"], "revision": fixed_saved["revision"]}])
    feedback.update_batch(
        db,
        fixed_batch["batch_id"],
        status="fixed",
        result={"commit": "a" * 40, "comparisons": [{"before": "x", "after": "y"}]},
    )

    for entry_id in (disposable, draft_entry, pending_entry, fixed_entry):
        db.execute(
            "UPDATE review_entries SET first_seen=?,day=? WHERE id=?",
            (old_stamp, "2026-06-01", entry_id),
        )

    assert store.prune(db, now=now) == 2
    assert db.execute("SELECT 1 FROM review_entries WHERE id=?", (disposable,)).fetchone() is None
    assert db.execute("SELECT 1 FROM review_entries WHERE id=?", (fixed_entry,)).fetchone() is None
    assert db.execute("SELECT 1 FROM review_entries WHERE id=?", (draft_entry,)).fetchone() is not None
    assert db.execute("SELECT 1 FROM review_entries WHERE id=?", (pending_entry,)).fetchone() is not None
