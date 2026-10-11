from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from pylon.outbox import Outbox
from pylon.tasks.daily_review import feedback, migrate, notifications, store
from pylon.tasks.intelligence.store import Store as IntelligenceStore
from pylon.tasks.marketplace import web_auth
from pylon.tasks.marketplace.store import Store as MarketplaceStore


@pytest.fixture
def review_db(tmp_path):
    box = Outbox(tmp_path / "daily-review.sqlite3")
    with box.connect() as db:
        store.initialize(db)
        yield db, box


def capture(
    db,
    source_id: str,
    *,
    source: str = "linuxdo",
    lane: str = "intel",
    title: str | None = None,
    body: str | None = None,
    first_seen: str = "2026-10-10T12:00:00+08:00",
    **kwargs,
):
    source_id = str(source_id)
    return store.capture(
        db,
        source,
        source_id,
        lane=lane,
        title=title or f"Fixture review {source_id}",
        body=body or f"Synthetic review body for {source_id}.",
        url=f"https://example.invalid/review/{source}/{source_id}",
        first_seen=first_seen,
        **kwargs,
    )


def test_items_walks_all_pages_of_fifty(review_db):
    db, _ = review_db
    for index in range(101):
        capture(
            db,
            f"page-{index}",
            first_seen=f"2026-10-10T00:{index // 60:02d}:{index % 60:02d}+08:00",
        )

    pages = [store.items(db, {"page": page}) for page in (1, 2, 3)]
    ids = [item["id"] for page in pages for item in page["items"]]

    assert [len(page["items"]) for page in pages] == [50, 50, 1]
    assert pages[0]["total"] == pages[1]["total"] == pages[2]["total"] == 101
    assert pages[0]["pages"] == pages[1]["pages"] == pages[2]["pages"] == 3
    assert len(ids) == len(set(ids)) == 101


def test_capture_uses_beijing_date_boundary_and_keeps_unknown_restores(review_db):
    db, _ = review_db

    assert capture(db, "before-start", first_seen="2026-10-09T15:59:59Z") is None
    boundary = capture(db, "at-start", first_seen="2026-10-09T16:00:00Z")
    unknown = capture(db, "restored-without-date", first_seen=None, restored=True)

    assert boundary is not None
    assert unknown is not None
    rows = store.items(db, {"page": 1})
    assert rows["total"] == 2
    by_id = {row["id"]: row for row in rows["items"]}
    boundary_entry = db.execute(
        "SELECT entry_id FROM review_snapshots WHERE id=?", (boundary,)
    ).fetchone()[0]
    unknown_entry = db.execute(
        "SELECT entry_id FROM review_snapshots WHERE id=?", (unknown,)
    ).fetchone()[0]
    assert by_id[boundary_entry]["day"] == "2026-10-10"
    assert by_id[unknown_entry]["day"] == "unknown"
    assert db.execute(
        "SELECT count(*) FROM review_decisions WHERE entry_id=?", (unknown_entry,)
    ).fetchone()[0] == 0


def test_same_origin_across_lanes_and_exact_long_content_keep_provenance(review_db):
    db, _ = review_db
    title = "跨来源相同长标题 fixture"
    body = (
        "这是一个足够长的合成正文，用于验证跨站转载合并。"
        "其中包含稳定的商品条件、地区和价格证据，内容在两个来源中完全一致。"
        "这段补充仍属于同一份原文，确保 fixture 超过精确长内容合并阈值。"
    )

    first = capture(db, "linux-42", lane="intel", title=title, body=body)
    second = capture(db, "linux-42", lane="market", title=title, body=body)
    third = capture(db, "v2ex-9001", source="v2ex", lane="intel", title=title, body=body)

    first_entry = db.execute("SELECT entry_id FROM review_snapshots WHERE id=?", (first,)).fetchone()[0]
    assert db.execute("SELECT entry_id FROM review_snapshots WHERE id=?", (second,)).fetchone()[0] == first_entry
    assert db.execute("SELECT entry_id FROM review_snapshots WHERE id=?", (third,)).fetchone()[0] == first_entry

    detail = store.detail(db, first_entry)
    assert {(row["source"], row["source_id"]) for row in detail["origins"]} == {
        ("linuxdo", "linux-42"),
        ("v2ex", "v2ex-9001"),
    }
    assert {(row["source"], row["source_id"], row["lane"]) for row in detail["snapshots"]} == {
        ("linuxdo", "linux-42", "intel"),
        ("linuxdo", "linux-42", "market"),
        ("v2ex", "v2ex-9001", "intel"),
    }
    assert store.items(db, {"source": "v2ex"})["total"] == 1
    assert store.items(db, {"lane": "market"})["total"] == 1
    assert store.items(db, {"source": "v2ex", "lane": "intel"})["total"] == 1


def test_prune_uses_first_archive_time_for_unknown_dates_and_keeps_unresolved_feedback(
    review_db,
):
    db, _ = review_db
    now = datetime(2026, 10, 11, tzinfo=store.ZONE).timestamp()
    cutoff = now - 90 * 86400

    expired_snapshot = capture(db, "unknown-expired", first_seen=None, restored=True)
    boundary_snapshot = capture(db, "unknown-boundary", first_seen=None, restored=True)
    protected_snapshot = capture(db, "unknown-protected", first_seen=None, restored=True)
    entries = {
        source_id: db.execute(
            "SELECT entry_id FROM review_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone()[0]
        for source_id, snapshot_id in (
            ("unknown-expired", expired_snapshot),
            ("unknown-boundary", boundary_snapshot),
            ("unknown-protected", protected_snapshot),
        )
    }
    db.execute(
        "UPDATE review_snapshots SET captured_at=? WHERE id=?",
        (cutoff - 1, expired_snapshot),
    )
    db.execute(
        "UPDATE review_snapshots SET captured_at=? WHERE id=?",
        (cutoff, boundary_snapshot),
    )
    db.execute(
        "UPDATE review_snapshots SET captured_at=? WHERE id=?",
        (cutoff - 1, protected_snapshot),
    )
    feedback.save(
        db,
        [
            {
                "entry_id": entries["unknown-protected"],
                "item_key": "",
                "snapshot_id": protected_snapshot,
                "decision_ids": [],
            }
        ],
        {
            "actions": ["facts"],
            "category": "",
            "price": "",
            "region": "",
            "reason": "待核对的历史日期 fixture",
            "scope": "item",
            "scope_value": "",
        },
    )

    for entry_id in entries.values():
        row = db.execute(
            "SELECT day,date_quality FROM review_entries WHERE id=?", (entry_id,)
        ).fetchone()
        assert (row["day"], row["date_quality"]) == ("unknown", "unknown")

    assert store.prune(db, now=now) == 1
    assert db.execute(
        "SELECT 1 FROM review_entries WHERE id=?", (entries["unknown-expired"],)
    ).fetchone() is None
    assert db.execute(
        "SELECT 1 FROM review_entries WHERE id=?", (entries["unknown-boundary"],)
    ).fetchone() is not None
    assert db.execute(
        "SELECT 1 FROM review_entries WHERE id=?", (entries["unknown-protected"],)
    ).fetchone() is not None


def test_late_decision_is_appended_and_previous_evidence_remains(review_db):
    db, _ = review_db
    snapshot = capture(db, "late-decision")
    entry_id = db.execute("SELECT entry_id FROM review_snapshots WHERE id=?", (snapshot,)).fetchone()[0]

    store.record(
        db,
        "linuxdo",
        "late-decision",
        lane="intel",
        status="ready",
        result={"decision": "hold", "reason": "first pass"},
        rule_version="v1",
        decided_at=1,
        snapshot_id=snapshot,
    )
    store.record(
        db,
        "linuxdo",
        "late-decision",
        lane="intel",
        status="skipped",
        result={"decision": "exclude", "reason": "late correction"},
        rule_version="v2",
        decided_at=2,
        snapshot_id=snapshot,
    )

    decisions = db.execute(
        "SELECT status,rule_version,data FROM review_decisions WHERE entry_id=? ORDER BY id",
        (entry_id,),
    ).fetchall()
    # Capture appends an explicit pending decision before analysis; later
    # decisions remain immutable rows rather than replacing that evidence.
    assert len(decisions) == 3
    assert [(row["status"], row["rule_version"]) for row in decisions] == [
        ("pending", ""),
        ("pending", "v1"),
        ("excluded", "v2"),
    ]
    assert json.loads(decisions[1]["data"])["reason"] == "first pass"
    assert store.latest(db, entry_id)[0]["rule_version"] == "v2"


def test_delivery_state_is_read_from_outbox_and_failed_or_filtered_items_are_visible(review_db):
    db, box = review_db
    delivered_snapshot = capture(db, "delivered", title="Delivered fixture")
    db.commit()
    delivered_event = box.enqueue(
        profile="webops",
        job_id="daily-review",
        kind="daily-review",
        payload={"text": "fixture delivery", "private": "must not render"},
        key="delivered",
    )
    delivered_entry = store.record(
        db,
        "linuxdo",
        "delivered",
        lane="intel",
        status="queued",
        result={"decision": "push"},
        rule_version="v1",
        event_id=delivered_event,
        snapshot_id=delivered_snapshot,
    )
    assert store.detail(db, delivered_entry)["status"] == "queued"
    db.commit()
    box.finish(delivered_event, "delivered", receipt={"ok": True})

    failed_snapshot = capture(db, "failed", title="Failed fixture")
    db.commit()
    failed_event = box.enqueue(
        profile="webops",
        job_id="daily-review",
        kind="daily-review",
        payload={"text": "fixture failure"},
        key="failed",
    )
    failed_entry = store.record(
        db,
        "linuxdo",
        "failed",
        lane="intel",
        status="queued",
        result={"categories": ["fixture-failure"]},
        rule_version="v1",
        event_id=failed_event,
        snapshot_id=failed_snapshot,
    )
    db.commit()
    box.finish(failed_event, "failed", error="synthetic failure")

    filtered_snapshot = capture(db, "filtered", title="Filtered fixture")
    filtered_entry = store.record(
        db,
        "linuxdo",
        "filtered",
        lane="intel",
        status="skipped",
        result={"categories": ["fixture-filter"]},
        rule_version="v1",
        snapshot_id=filtered_snapshot,
    )

    assert store.items(db, {"status": "delivered"})["items"][0]["id"] == delivered_entry
    failed = store.items(db, {"status": "failed", "category": "fixture-failure"})
    assert failed["total"] == 1 and failed["items"][0]["id"] == failed_entry
    excluded = store.items(db, {"status": "excluded", "q": "Filtered"})
    assert excluded["total"] == 1 and excluded["items"][0]["id"] == filtered_entry

    delivered_decision = next(
        decision
        for decision in store.detail(db, delivered_entry)["decisions"]
        if decision["event_id"] == delivered_event
    )
    notification = delivered_decision["notification"]
    assert notification["status"] == "delivered"
    assert notification["text"] == "fixture delivery"
    assert "private" not in notification


def test_digest_waits_for_beijing_nine_and_deduplicates_the_same_day(review_db, monkeypatch):
    db, box = review_db
    capture(db, "digest-entry", first_seen="2026-10-10T12:00:00+08:00")
    db.commit()
    monkeypatch.setattr(web_auth, "public_url", lambda: "https://example.invalid/")
    before_nine = datetime.fromisoformat("2026-10-11T08:59:00+08:00").timestamp()
    at_nine = datetime.fromisoformat("2026-10-11T09:00:00+08:00").timestamp()

    assert notifications.digest(box, now=before_nine) == {"queued_days": []}
    assert box.counts() == {}
    assert notifications.digest(box, now=at_nine) == {"queued_days": ["2026-10-10"]}
    assert notifications.digest(box, now=at_nine) == {"queued_days": []}
    with box.connect() as check:
        events = check.execute(
            "SELECT job_id,payload FROM events WHERE job_id='daily-review'"
        ).fetchall()
    assert len(events) == 1
    assert "2026-10-10 每日审核" in json.loads(events[0]["payload"])["text"]


def test_empty_review_day_is_silent(review_db, monkeypatch):
    _, box = review_db
    monkeypatch.setattr(web_auth, "public_url", lambda: "https://example.invalid/")
    now = datetime.fromisoformat("2026-10-11T09:00:00+08:00").timestamp()

    assert notifications.digest(box, now=now) == {"queued_days": []}
    assert box.counts() == {}


def test_migration_excludes_pre_start_inputs_and_marks_unknown_retained_judgments(tmp_path):
    native_path = tmp_path / "xianbao-state.sqlite3"
    native = sqlite3.connect(native_path)
    native.executescript(
        """
        CREATE TABLE seen_posts (
            id TEXT, title TEXT, url TEXT, first_seen_at TEXT, last_seen_at TEXT,
            rules_hash TEXT, fingerprint TEXT, reason TEXT, tier TEXT,
            score REAL, decision_keep INTEGER
        );
        CREATE TABLE article_cache (url TEXT PRIMARY KEY, body TEXT);
        """
    )
    native.execute(
        "INSERT INTO seen_posts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "before-start",
            "Before start fixture",
            "https://example.invalid/before-start",
            "2026-10-09T12:00:00+08:00",
            "2026-10-09T12:00:00+08:00",
            "rules-old",
            "fp-old",
            "old retained judgment",
            "D",
            1,
            0,
        ),
    )
    native.execute(
        "INSERT INTO seen_posts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            "unknown-date",
            "Unknown date fixture",
            "https://example.invalid/unknown-date",
            None,
            "2026-10-11T12:00:00+08:00",
            "rules-new",
            "fp-new",
            "date was not retained",
            "D",
            2,
            1,
        ),
    )
    native.execute(
        "INSERT INTO article_cache VALUES(?,?)",
        ("https://example.invalid/unknown-date", "Synthetic retained article body."),
    )
    native.commit()
    native.close()

    box = Outbox(tmp_path / "migrated.sqlite3")
    result = migrate.migrate(box, native_path=native_path)
    assert result["xianbao_retained"] == 1
    with box.connect() as db:
        row = db.execute(
            "SELECT id,day,date_quality,status FROM review_entries"
        ).fetchone()
        decision = db.execute("SELECT status,data,restored FROM review_decisions").fetchone()
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert row["day"] == "unknown"
    assert row["date_quality"] == "unknown"
    assert row["status"] == "unknown"
    assert decision["status"] == "unknown"
    assert decision["restored"] == 1
    assert json.loads(decision["data"])["original_input_version_unknown"] is True


def test_cap_legacy_retries_marks_failure_without_model_or_delivery_side_effect(tmp_path):
    box = Outbox(tmp_path / "legacy-retry.sqlite3")
    intel = IntelligenceStore(box)
    source_id = "legacy-retry"
    intel.ingest(
        "v2ex",
        source_id,
        title="Legacy retry fixture",
        body="Synthetic legacy retry body retained for migration.",
        url=f"https://example.invalid/legacy/{source_id}",
        context={"route": "candidate", "first_seen": "2026-10-11T12:00:00+08:00"},
    )
    with box.connect() as db:
        db.execute(
            "UPDATE intel_items SET status='retry',attempts=3,phase='value',error='legacy model failure',result=? WHERE source_id=?",
            (json.dumps({"old": "result"}), source_id),
        )

    assert migrate.cap_legacy_retries(box) == 1
    with box.connect() as db:
        item = db.execute(
            "SELECT status,result,next_attempt,error FROM intel_items WHERE source_id=?", (source_id,)
        ).fetchone()
        decisions = db.execute(
            "SELECT status,data,restored FROM review_decisions WHERE entry_id=(SELECT entry_id FROM review_origins WHERE source_id=?) ORDER BY id",
            (source_id,),
        ).fetchall()
        events = db.execute("SELECT count(*) FROM events").fetchone()[0]
    assert item["status"] == "failed"
    assert item["result"] is None
    assert item["next_attempt"] == 0
    assert "旧版本累计重试 3 次" in item["error"]
    assert decisions[-1]["status"] == "failed"
    assert decisions[-1]["restored"] == 1
    assert events == 0

    baseline = len(decisions)
    for _ in range(2):
        migrate.migrate(box)
        assert migrate.cap_legacy_retries(box) == 0
    with box.connect() as db:
        repeated = db.execute(
            "SELECT status,data,restored FROM review_decisions WHERE entry_id=(SELECT entry_id FROM review_origins WHERE source_id=?) ORDER BY id",
            (source_id,),
        ).fetchall()
    assert len(repeated) == baseline


def test_marketplace_poll_does_not_reset_a_completed_review_on_duplicate_listing(tmp_path):
    box = Outbox(tmp_path / "market-poll.sqlite3")
    market = MarketplaceStore(box)
    post = {
        "id": "12345",
        "title": "Synthetic completed listing",
        "url": "https://example.invalid/t/12345",
        "body_full": "A complete synthetic listing body with enough public detail for the fixture.",
        "body_complete": True,
        "first_seen": "2026-10-10T12:00:00+08:00",
    }
    assert market.ingest("v2ex", [post]) == 1
    with box.connect() as db:
        snapshot_id = db.execute(
            "SELECT id FROM review_snapshots WHERE source='v2ex' AND source_id='12345' AND lane='market'"
        ).fetchone()[0]
        entry_id = store.record(
            db,
            "v2ex",
            "12345",
            lane="market",
            status="skipped",
            result={"reason": "synthetic completed review"},
            rule_version="fixture-v1",
            snapshot_id=snapshot_id,
        )
        before = db.execute(
            "SELECT count(*) FROM review_decisions WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]

    assert market.ingest("v2ex", [post]) == 0
    with box.connect() as db:
        after = db.execute(
            "SELECT count(*) FROM review_decisions WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]
        latest = store.latest(db, entry_id)
    assert after == before
    assert latest[-1]["status"] == "excluded"
