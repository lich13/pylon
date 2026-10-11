from __future__ import annotations

import pytest

from pylon.outbox import Outbox
from pylon.tasks.daily_review import feedback, store


@pytest.fixture
def review_db(tmp_path):
    box = Outbox(tmp_path / "daily-review-feedback-resolution.sqlite3")
    with box.connect() as db:
        store.initialize(db)
        yield db, box


def make_entry(db, source_id: str):
    snapshot_id = store.capture(
        db,
        "v2ex",
        source_id,
        lane="market",
        title=f"Resolution fixture {source_id}",
        body=f"Synthetic resolution body for {source_id}.",
        url=f"https://example.invalid/resolution/{source_id}",
        first_seen="2026-10-10T13:00:00+08:00",
    )
    entry_id = store.record(
        db,
        "v2ex",
        source_id,
        lane="market",
        status="ready",
        result={
            "items": [
                {
                    "item": {"brand": "Fixture", "model": "Model-0", "category": "fixture"},
                    "decision": "hold",
                }
            ]
        },
        rule_version="resolution-v1",
        snapshot_id=snapshot_id,
    )
    decision_id = store.latest(db, entry_id)[-1]["id"]
    return entry_id, snapshot_id, decision_id


def feedback_value(reason: str = "synthetic resolution reason"):
    return {
        "actions": ["facts"],
        "category": "fixture",
        "price": "10",
        "region": "CN",
        "reason": reason,
        "scope": "item",
        "scope_value": "",
    }


def make_submitted_batch(db, count: int = 2):
    entries = [make_entry(db, f"batch-{index}") for index in range(count)]
    saved = []
    for entry_id, snapshot_id, decision_id in entries:
        saved.append(
            feedback.save(
                db,
                [
                    {
                        "entry_id": entry_id,
                        "item_key": "",
                        "snapshot_id": snapshot_id,
                        "decision_ids": [decision_id],
                    }
                ],
                feedback_value(),
            )["saved"][0]
        )
    submission = feedback.submit(
        db, [{"id": row["id"], "revision": row["revision"]} for row in saved]
    )
    return submission["batch_id"], entries, saved


def comparison(feedback_id: str, *, commit: str | None = None, status: str = "fixed"):
    row = {
        "feedback_id": feedback_id,
        "status": status,
        "before": "synthetic before",
        "after": "synthetic after",
        "evidence": "synthetic test evidence",
    }
    if commit is not None:
        row["commit"] = commit
    return row


def test_results_expose_each_feedback_and_batch_unresolved_filter(review_db):
    db, _ = review_db
    batch_id, entries, saved = make_submitted_batch(db)
    commit = "a" * 40

    initial = feedback.results(db, batch_id=batch_id)
    assert {row["id"] for row in initial} == {row["id"] for row in saved}
    assert {row["status"] for row in initial} == {"submitted"}
    assert feedback.results(db, entry_id=entries[0][0])[0]["id"] == saved[0]["id"]
    assert feedback.batches(db, unresolved=True)[0]["id"] == batch_id

    feedback.update_batch(
        db,
        batch_id,
        status="processing",
        result={
            "commit": commit,
            "comparisons": [comparison(saved[0]["id"])],
        },
    )
    partial = {row["id"]: row for row in feedback.results(db, batch_id=batch_id)}
    assert partial[saved[0]["id"]]["status"] == "fixed"
    assert partial[saved[1]["id"]]["status"] == "processing"
    assert feedback.batches(db)[0]["fixed_count"] == 1
    assert feedback.batches(db, unresolved=True)[0]["status"] == "processing"

    feedback.update_batch(
        db,
        batch_id,
        status="processing",
        result={
            "commit": commit,
            "comparisons": [comparison(saved[1]["id"], commit=commit)],
        },
    )
    final = {row["id"]: row for row in feedback.results(db, batch_id=batch_id)}
    assert {row["status"] for row in final.values()} == {"fixed"}
    assert feedback.batches(db, unresolved=True) == []
    batch = feedback.batches(db)[0]
    assert batch["status"] == "fixed"
    comparisons = {row["feedback_id"]: row for row in batch["result"]["comparisons"]}
    assert set(comparisons) == {row["id"] for row in saved}
    assert all(
        comparisons[row["id"]][key]
        for row in saved
        for key in ("feedback_id", "before", "after", "evidence", "commit")
    )


def test_fixed_partial_batch_is_rejected_without_writes(review_db):
    db, _ = review_db
    batch_id, _, saved = make_submitted_batch(db)
    before = db.execute(
        "SELECT status,result FROM review_batches WHERE id=?", (batch_id,)
    ).fetchone()
    before_resolution_count = db.execute("SELECT count(*) FROM review_resolutions").fetchone()[0]

    with pytest.raises(ValueError, match="未修复"):
        feedback.update_batch(
            db,
            batch_id,
            status="fixed",
            result={
                "commit": "b" * 40,
                "comparisons": [comparison(saved[0]["id"])],
            },
        )

    after = db.execute(
        "SELECT status,result FROM review_batches WHERE id=?", (batch_id,)
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert db.execute("SELECT count(*) FROM review_resolutions").fetchone()[0] == before_resolution_count


def test_fixed_requires_evidence_and_needs_info_is_unresolved(review_db):
    db, _ = review_db
    batch_id, _, saved = make_submitted_batch(db, count=1)
    feedback_id = saved[0]["id"]
    commit = "c" * 40

    invalid_comparisons = [
        {
            "before": "before",
            "after": "after",
            "evidence": "evidence",
            "commit": commit,
        },
        {
            "feedback_id": feedback_id,
            "before": "before",
            "after": "after",
            "evidence": "evidence",
        },
        {"feedback_id": feedback_id, "after": "after", "evidence": "evidence", "commit": commit},
        {"feedback_id": feedback_id, "before": "before", "evidence": "evidence", "commit": commit},
        {"feedback_id": feedback_id, "before": "before", "after": "after", "commit": commit},
    ]
    for invalid in invalid_comparisons:
        with pytest.raises(ValueError):
            feedback.update_batch(
                db,
                batch_id,
                status="fixed",
                result={"comparisons": [invalid]},
            )
    assert db.execute("SELECT count(*) FROM review_resolutions").fetchone()[0] == 0

    with pytest.raises(ValueError, match="证据"):
        feedback.update_batch(
            db,
            batch_id,
            status="processing",
            result={
                "commit": commit,
                "comparisons": [
                    {
                        "feedback_id": feedback_id,
                        "before": "before",
                        "after": "after",
                    }
                ],
            },
        )
    assert db.execute("SELECT count(*) FROM review_resolutions").fetchone()[0] == 0

    feedback.update_batch(
        db,
        batch_id,
        status="needs_info",
        result={"reason": "synthetic evidence is insufficient"},
    )
    result = feedback.results(db, batch_id=batch_id)[0]
    assert result["status"] == "needs_info"
    assert result["result"]["reason"] == "synthetic evidence is insufficient"
    assert feedback.batches(db, unresolved=True)[0]["status"] == "needs_info"
