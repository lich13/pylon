from __future__ import annotations

import json

import pytest

from pylon.outbox import Outbox
from pylon.tasks.daily_review import feedback, store


@pytest.fixture
def review_db(tmp_path):
    box = Outbox(tmp_path / "daily-review-compact.sqlite3")
    with box.connect() as db:
        store.initialize(db)
        yield db, box


def capture_entry(
    db,
    source_id: str,
    *,
    body: str | None = None,
    result: dict | None = None,
    title: str | None = None,
    first_seen: str = "2026-10-10T12:00:00+08:00",
    lane: str = "market",
    rule_version: str = "fixture-v1",
    fingerprint: str | None = None,
):
    snapshot_id = store.capture(
        db,
        "v2ex",
        source_id,
        lane=lane,
        title=title or f"Compact fixture {source_id}",
        body=body or f"Synthetic compact body for {source_id}.",
        url=f"https://example.invalid/daily-review/{source_id}",
        first_seen=first_seen,
        fingerprint=fingerprint,
    )
    entry_id = store.record(
        db,
        "v2ex",
        source_id,
        lane=lane,
        status="ready",
        result=result or {"items": []},
        rule_version=rule_version,
        snapshot_id=snapshot_id,
    )
    decision_id = store.latest(db, entry_id)[-1]["id"]
    return entry_id, snapshot_id, decision_id


def feedback_value(reason: str = "synthetic correction"):
    return {
        "actions": ["facts"],
        "category": "fixture",
        "price": "10",
        "region": "CN",
        "reason": reason,
        "scope": "item",
        "scope_value": "",
    }


def submit_feedback(db, *, entry_id, snapshot_id, decision_ids, item_key=""):
    saved = feedback.save(
        db,
        [
            {
                "entry_id": entry_id,
                "item_key": item_key,
                "snapshot_id": snapshot_id,
                "decision_ids": decision_ids,
            }
        ],
        feedback_value(),
    )["saved"][0]
    return feedback.submit(db, [{"id": saved["id"], "revision": saved["revision"]}])


def test_compact_items_prioritize_delivered_before_priority(review_db):
    db, box = review_db
    pending_entry, _, _ = capture_entry(
        db,
        "priority-pending",
        body="免费返现价格优惠教程部署北京全国邮寄今日限时",
        result={"facts": [{"value": "high priority synthetic opportunity"}]},
    )
    delivered_snapshot = store.capture(
        db,
        "v2ex",
        "delivered-first",
        lane="intel",
        title="Delivered compact fixture",
        body="Plain delivered fixture body.",
        url="https://example.invalid/daily-review/delivered-first",
        first_seen="2026-10-10T12:01:00+08:00",
    )
    db.commit()
    event_id = box.enqueue(
        profile="webops",
        job_id="daily-review",
        kind="daily-review",
        payload={"text": "synthetic delivery"},
        key="delivered-first",
    )
    delivered_entry = store.record(
        db,
        "v2ex",
        "delivered-first",
        lane="intel",
        status="queued",
        result={"decision": "push"},
        rule_version="fixture-v1",
        event_id=event_id,
        snapshot_id=delivered_snapshot,
    )
    db.commit()
    box.finish(event_id, "delivered", receipt={"ok": True})

    rows = store.items(db, {"review": "all", "page": 1})["items"]

    assert rows[0]["id"] == delivered_entry
    assert rows[0]["status"] == "delivered"
    assert rows[1]["id"] == pending_entry

    delivered_decision = store.latest(db, delivered_entry)[-1]["id"]
    submit_feedback(
        db,
        entry_id=delivered_entry,
        snapshot_id=delivered_snapshot,
        decision_ids=[delivered_decision],
    )
    assert delivered_entry not in {
        row["id"] for row in store.items(db, {"review": "pending", "page": 1})["items"]
    }

    # A newer model judgment must reopen the already reviewed entry for review,
    # while the immutable delivered receipt remains its visible status.
    newer_snapshot = store.capture(
        db,
        "v2ex",
        "delivered-first",
        lane="intel",
        title="Delivered compact fixture",
        body="A newer synthetic judgment version with additional evidence.",
        url="https://example.invalid/daily-review/delivered-first",
        first_seen="2026-10-10T12:01:00+08:00",
        fingerprint="fixture-delivered-v2",
    )
    store.record(
        db,
        "v2ex",
        "delivered-first",
        lane="intel",
        status="excluded",
        result={"decision": "skip", "reason": "new synthetic judgment"},
        rule_version="fixture-v2",
        snapshot_id=newer_snapshot,
    )
    reopened = store.items(db, {"review": "pending", "page": 1})["items"]
    reopened_row = next(row for row in reopened if row["id"] == delivered_entry)
    assert reopened_row["status"] == "delivered"
    assert reopened_row["reviewed"] is False


def test_compact_items_support_fifty_and_one_hundred_page_sizes(review_db):
    db, _ = review_db
    for index in range(101):
        capture_entry(
            db,
            f"page-{index}",
            first_seen=f"2026-10-10T00:{index // 60:02d}:{index % 60:02d}+08:00",
        )

    page_50 = store.items(db, {"page": 1, "page_size": 50})
    page_100 = store.items(db, {"page": 1, "page_size": 100})
    second_100 = store.items(db, {"page": 2, "page_size": 100})

    assert (page_50["page_size"], len(page_50["items"])) == (50, 50)
    assert (page_100["page_size"], len(page_100["items"])) == (100, 100)
    assert (second_100["page"], len(second_100["items"])) == (2, 1)
    assert page_100["total"] == page_50["total"] == 101
    with pytest.raises(ValueError, match="50 或 100"):
        store.items(db, {"page_size": 25})


def test_compact_items_redact_full_body_and_product_arrays(review_db):
    db, _ = review_db
    entry_id, _, _ = capture_entry(
        db,
        "compact-redaction",
        body="Synthetic public body; hidden full body should not be returned.",
        result={
            "items": [
                {
                    "item": {
                        "model": "Fixture-A",
                        "price": "10",
                        "currency": "CNY",
                        "description": "secret product detail",
                    },
                    "decision": "push",
                },
                {"item": {"model": "Fixture-B", "price": "20", "currency": "USD"}},
            ],
            "facts": [{"value": "short synthetic summary"}],
        },
    )

    row = next(item for item in store.items(db, {"review": "all"})["items"] if item["id"] == entry_id)

    assert "body" not in row
    assert "latest" not in row
    assert row["product_count"] == 2
    assert row["price_summary"] == "10 元 / 20 美元"
    assert row["summary"] == "short synthetic summary"
    assert "secret product detail" not in json.dumps(row, ensure_ascii=False)


def test_pending_review_hides_only_matching_submitted_whole_post(review_db):
    db, _ = review_db
    item_result = {
        "items": [
            {"item": {"model": "Fixture-A", "category": "fixture"}, "decision": "push"},
            {"item": {"model": "Fixture-B", "category": "fixture"}, "decision": "hold"},
        ]
    }

    hidden_entry, hidden_snapshot, hidden_decision = capture_entry(
        db, "whole-post", result=item_result
    )
    submit_feedback(
        db,
        entry_id=hidden_entry,
        snapshot_id=hidden_snapshot,
        decision_ids=[hidden_decision],
    )

    item_entry, item_snapshot, item_decision = capture_entry(
        db, "item-only", result=item_result
    )
    submit_feedback(
        db,
        entry_id=item_entry,
        snapshot_id=item_snapshot,
        decision_ids=[item_decision],
        item_key=f"{item_decision}:0",
    )

    stale_entry, stale_snapshot, stale_decision = capture_entry(db, "stale-snapshot", result=item_result)
    submit_feedback(
        db,
        entry_id=stale_entry,
        snapshot_id=stale_snapshot,
        decision_ids=[stale_decision],
    )
    new_snapshot = store.capture(
        db,
        "v2ex",
        "stale-snapshot",
        lane="market",
        title="Compact fixture stale-snapshot",
        body="Synthetic changed body for stale snapshot.",
        url="https://example.invalid/daily-review/stale-snapshot",
        first_seen="2026-10-10T12:00:00+08:00",
        fingerprint="fixture-new-snapshot",
    )
    assert new_snapshot != stale_snapshot

    new_judgment_entry, new_judgment_snapshot, old_decision = capture_entry(
        db, "new-judgment", result=item_result
    )
    submit_feedback(
        db,
        entry_id=new_judgment_entry,
        snapshot_id=new_judgment_snapshot,
        decision_ids=[old_decision],
    )
    store.record(
        db,
        "v2ex",
        "new-judgment",
        lane="market",
        status="ready",
        result={"items": item_result["items"], "facts": [{"value": "new judgment"}]},
        rule_version="fixture-v2",
        snapshot_id=new_judgment_snapshot,
    )

    pending = {
        row["id"]: row for row in store.items(db, {"review": "pending"})["items"]
    }
    assert hidden_entry not in pending
    assert {item_entry, stale_entry, new_judgment_entry} <= set(pending)
    assert pending[item_entry]["reviewed"] is False
    assert pending[item_entry]["feedback_state"] == "submitted"
    assert pending[stale_entry]["feedback_state"] == "updated"
    assert pending[new_judgment_entry]["feedback_state"] == "updated"

    all_rows = {row["id"]: row for row in store.items(db, {"review": "all"})["items"]}
    assert all_rows[hidden_entry]["reviewed"] is True
    assert all_rows[hidden_entry]["feedback_state"] == "submitted"
