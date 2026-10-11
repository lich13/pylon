from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from starlette.testclient import TestClient

from pylon.outbox import Outbox
from pylon.tasks.daily_review import browse, feedback, store
from pylon.tasks.marketplace import web, web_auth
from pylon.tasks.intelligence import store as intelligence_store


BASE_URL = "https://example.invalid"
API = "/pylon/review/api"


@pytest.fixture
def review_db(tmp_path):
    box = Outbox(tmp_path / "daily-review-layered.sqlite3")
    with box.connect() as db:
        browse.initialize(db)
        yield db, box


@pytest.fixture
def web_harness(tmp_path):
    box = Outbox(tmp_path / "daily-review-layered-web.sqlite3")
    app = web.create_app(store=intelligence_store.Store(box))
    return TestClient(app, base_url=BASE_URL), box


def _first_seen(index: int, *, base: int = 0) -> str:
    value = datetime(2026, 10, 10, tzinfo=store.ZONE) + timedelta(seconds=base + index)
    return value.isoformat()


def _seed(
    db,
    box,
    source_id: str,
    *,
    status: str = "pending",
    index: int = 0,
    title: str | None = None,
    body: str | None = None,
    source: str = "v2ex",
    lane: str = "intel",
    categories: tuple[str, ...] = (),
    result: dict | None = None,
    delivered: bool = False,
):
    title = title or f"Layered fixture {source_id}"
    body = body or f"Synthetic review body for {source_id}."
    result = dict(result or {"items": []})
    if categories:
        result["categories"] = list(categories)
    snapshot_id = store.capture(
        db,
        source,
        source_id,
        lane=lane,
        title=title,
        body=body,
        url=f"https://example.invalid/review/{source}/{source_id}",
        first_seen=_first_seen(index),
    )
    if delivered:
        db.commit()
        event_id = box.enqueue(
            profile="webops",
            job_id="daily-review",
            kind="daily-review",
            payload={"text": f"fixture delivery {source_id}"},
            key=f"layered-{source_id}",
        )
        entry_id = store.record(
            db,
            source,
            source_id,
            lane=lane,
            status="queued",
            result={"decision": "push", **result},
            rule_version="fixture-v1",
            event_id=event_id,
            snapshot_id=snapshot_id,
        )
        db.commit()
        box.finish(event_id, "delivered", receipt={"ok": True})
        store.sync_deliveries(db)
    else:
        entry_id = store.record(
            db,
            source,
            source_id,
            lane=lane,
            status=status,
            result=result,
            rule_version="fixture-v1",
            snapshot_id=snapshot_id,
        )
    return {
        "id": entry_id,
        "source_id": source_id,
        "snapshot_id": snapshot_id,
        "decision_ids": [row["id"] for row in store.latest(db, entry_id)],
    }


def _feed_all(db, view_id: str, *, section: str = "main", **filters):
    query = {"view": view_id, "section": section, **filters}
    values = []
    cursor = None
    while True:
        page_query = {**query}
        if cursor:
            page_query["cursor"] = cursor
        page = browse.feed(db, page_query)
        values.extend(page["items"])
        next_cursor = page["next_cursor"]
        if not next_cursor:
            break
        assert next_cursor != cursor
        cursor = next_cursor
    return values


def _submit_whole_post(db, entry: dict):
    value = {
        "actions": ["facts"],
        "category": "fixture",
        "price": "",
        "region": "",
        "reason": "synthetic review completion",
        "scope": "item",
        "scope_value": "",
    }
    saved = feedback.save(
        db,
        [
            {
                "entry_id": entry["id"],
                "item_key": "",
                "snapshot_id": entry["snapshot_id"],
                "decision_ids": entry["decision_ids"],
            }
        ],
        value,
    )["saved"][0]
    return feedback.submit(db, [{"id": saved["id"], "revision": saved["revision"]}])


def _login(client, box):
    with box.connect() as db:
        token = web_auth.issue_login(db, profile="webops")
    response = client.post(f"{API}/login", json={"token": token})
    assert response.status_code == 200
    return response.json()["csrf"]


def _csrf_headers(csrf, *, origin=BASE_URL):
    return {"Origin": origin, "X-CSRF-Token": csrf}


def test_create_prioritizes_delivered_then_recalls_and_caps_daily_auto_slots(review_db):
    db, box = review_db
    delivered = [
        _seed(db, box, f"delivered-{index}", delivered=True, index=index)
        for index in range(2)
    ]
    candidates = [
        _seed(db, box, f"candidate-{index}", index=100 + index)
        for index in range(55)
    ]

    first = browse.create(db, {})
    first_items = _feed_all(db, first["view_id"])
    first_ids = [item["id"] for item in first_items]
    assert len(first_ids) == 52
    assert set(first_ids[:2]) == {entry["id"] for entry in delivered}
    assert all(first_ids.index(entry["id"]) < 2 for entry in delivered)
    assert sum(entry["id"] in first_ids for entry in candidates) == 50
    assert first["counts"] == {"main": 52, "stored": 5, "total": 57}

    outside = next(entry for entry in candidates if entry["id"] not in first_ids)
    assert browse.recall(db, [outside["id"]]) == {"changed": 1, "active": True}

    second = browse.create(db, {})
    second_items = _feed_all(db, second["view_id"])
    second_ids = [item["id"] for item in second_items]
    assert set(second_ids[:2]) == {entry["id"] for entry in delivered}
    assert second_ids[2] == outside["id"]
    assert sum(
        entry["id"] in second_ids and not next(item for item in second_items if item["id"] == entry["id"])["recalled"]
        for entry in candidates
    ) == 50
    assert second["counts"] == {"main": 53, "stored": 4, "total": 57}


def test_structured_match_price_advantage_and_conflict_outrank_free_keyword_score(review_db):
    db, box = review_db
    lexical = [
        _seed(
            db,
            box,
            f"lexical-{index}",
            index=100 + index,
            title=f"免费返现优惠教程 {index}",
            body="免费返现价格优惠教程部署北京全国邮寄今日限时。",
        )
        for index in range(50)
    ]
    matched = _seed(
        db,
        box,
        "structured-match",
        index=1,
        result={
            "items": [
                {
                    "item": {"model": "Matched fixture", "price": "10"},
                    "decision": "push",
                    "matched": True,
                }
            ]
        },
    )
    advantage = _seed(
        db,
        box,
        "structured-advantage",
        index=2,
        result={"items": [{"item": {"model": "Advantage"}, "opportunity": True, "decision": "push"}]},
    )
    conflict = _seed(
        db,
        box,
        "structured-conflict",
        index=3,
        result={"decision": "push", "policy_decision": "skip"},
    )

    view = browse.create(db, {})
    main_ids = {item["id"] for item in _feed_all(db, view["view_id"])}
    assert view["counts"]["main"] == 50
    assert {matched["id"], advantage["id"], conflict["id"]} <= main_ids
    assert sum(entry["id"] in main_ids for entry in lexical) == 47


def test_reviewed_slot_is_not_replaced_and_new_snapshot_reopens_it(review_db):
    db, box = review_db
    candidates = [
        _seed(db, box, f"slot-{index}", index=100 + index)
        for index in range(52)
    ]
    first = browse.create(db, {})
    first_ids = [item["id"] for item in _feed_all(db, first["view_id"])]
    selected = next(entry for entry in candidates if entry["id"] in first_ids)
    outside = next(entry for entry in candidates if entry["id"] not in first_ids)
    _submit_whole_post(db, selected)

    second = browse.create(db, {})
    second_items = _feed_all(db, second["view_id"])
    second_ids = [item["id"] for item in second_items]
    assert selected["id"] not in second_ids
    assert outside["id"] not in second_ids
    assert second["counts"]["main"] == 49

    changed_snapshot = store.capture(
        db,
        "v2ex",
        selected["source_id"],
        lane="intel",
        title=f"Layered fixture {selected['source_id']}",
        body="A newer source version reopens the reviewed slot.",
        url=f"https://example.invalid/review/v2ex/{selected['source_id']}",
        first_seen=_first_seen(100),
        fingerprint="fixture-v2",
    )
    assert changed_snapshot != selected["snapshot_id"]
    assert browse.updates(db, first["view_id"])["has_updates"] is True

    third = browse.create(db, {})
    third_ids = [item["id"] for item in _feed_all(db, third["view_id"])]
    assert selected["id"] in third_ids
    assert outside["id"] not in third_ids
    assert third["counts"]["main"] == 50


def test_view_cursor_has_no_overlap_or_omission_and_new_input_is_isolated(review_db):
    db, box = review_db
    entries = [_seed(db, box, f"cursor-{index}", index=index) for index in range(101)]
    entry_ids = [entry["id"] for entry in entries]
    assert browse.recall(db, entry_ids) == {"changed": 101, "active": True}

    view = browse.create(db, {"review": "all"})
    items = _feed_all(db, view["view_id"])
    ids = [item["id"] for item in items]
    assert len(ids) == len(set(ids)) == len(entry_ids)
    assert set(ids) == set(entry_ids)

    new_entry = _seed(db, box, "cursor-new-delivery", index=1000, delivered=True)
    assert browse.updates(db, view["view_id"])["has_updates"] is True
    assert [item["id"] for item in _feed_all(db, view["view_id"])] == ids

    fresh = browse.create(db, {"review": "all"})
    assert new_entry["id"] in {item["id"] for item in _feed_all(db, fresh["view_id"])}


def test_all_collapsed_groups_are_filterable_and_cover_the_frozen_view(review_db):
    db, box = review_db
    delivered = _seed(db, box, "group-delivered", index=1, delivered=True)
    excluded = _seed(
        db,
        box,
        "group-excluded",
        status="excluded",
        index=2,
        title="Needle excluded fixture",
        body="Excluded body with filter needle.",
        categories=("fixture-category",),
    )
    unresolved = [
        _seed(db, box, f"group-unresolved-{status}", status=status, index=3 + pos)
        for pos, status in enumerate(("needs_info", "failed", "unknown"))
    ]
    duplicate = _seed(db, box, "group-duplicate", status="duplicate", index=7)
    other = [
        _seed(db, box, f"group-other-{index}", index=100 + index)
        for index in range(55)
    ]

    view = browse.create(db, {"review": "all"})
    sections = ["main", *browse.GROUPS]
    by_section = {
        section: _feed_all(db, view["view_id"], section=section)
        for section in sections
    }
    section_ids = [item["id"] for items in by_section.values() for item in items]
    assert len(section_ids) == len(set(section_ids)) == view["counts"]["total"]
    main_ids = {item["id"] for item in by_section["main"]}
    assert delivered["id"] in main_ids
    assert sum(entry["id"] in main_ids for entry in other) == 50
    assert excluded["id"] in {item["id"] for item in by_section["excluded"]}
    assert {entry["id"] for entry in unresolved} <= {
        item["id"] for item in by_section["unresolved"]
    }
    assert duplicate["id"] in {item["id"] for item in by_section["duplicate"]}
    assert all(len(group["titles"]) <= 3 for group in view["groups"])

    for query in (
        {"source": "v2ex"},
        {"category": "fixture-category"},
        {"q": "needle"},
    ):
        filtered = browse.feed(
            db,
            {"view": view["view_id"], "section": "excluded", **query},
        )
        assert filtered["total"] == 1
        assert [item["id"] for item in filtered["items"]] == [excluded["id"]]


def test_recall_is_idempotent_capped_and_has_no_outbox_or_policy_side_effects(review_db):
    db, box = review_db
    entries = [
        _seed(db, box, f"recall-{index}", status="excluded", index=index)
        for index in range(201)
    ]
    target_ids = [entry["id"] for entry in entries[:200]]
    before_events = db.execute("SELECT count(*) FROM events").fetchone()[0]
    before_decisions = db.execute("SELECT count(*) FROM review_decisions").fetchone()[0]
    before_feedback = db.execute("SELECT count(*) FROM review_feedback").fetchone()[0]
    before_status = db.execute(
        "SELECT status FROM review_entries WHERE id=?", (entries[0]["id"],)
    ).fetchone()[0]

    assert browse.recall(db, target_ids) == {"changed": 200, "active": True}
    assert browse.recall(db, target_ids) == {"changed": 0, "active": True}
    assert browse.recall(db, target_ids, active=False) == {"changed": 200, "active": False}
    assert browse.recall(db, target_ids, active=False) == {"changed": 0, "active": False}

    with pytest.raises(ValueError):
        browse.recall(db, [])
    with pytest.raises(ValueError):
        browse.recall(db, target_ids + [entries[200]["id"]])
    with pytest.raises(ValueError):
        browse.recall(db, [target_ids[0], target_ids[0]])
    with pytest.raises(ValueError):
        browse.recall(db, ["missing-entry"])
    with pytest.raises(ValueError):
        browse.recall(db, [target_ids[0]], active=1)

    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == before_events
    assert db.execute("SELECT count(*) FROM review_decisions").fetchone()[0] == before_decisions
    assert db.execute("SELECT count(*) FROM review_feedback").fetchone()[0] == before_feedback
    assert db.execute(
        "SELECT status FROM review_entries WHERE id=?", (entries[0]["id"],)
    ).fetchone()[0] == before_status


def test_frozen_view_recall_resume_cursor_and_owner_survive_database_reopen(tmp_path):
    box = Outbox(tmp_path / "daily-review-reopen.sqlite3")
    with box.connect() as db:
        browse.initialize(db)
        entries = [
            _seed(db, box, f"reopen-{index}", index=index)
            for index in range(51)
        ]
        duplicate = _seed(db, box, "reopen-duplicate", status="duplicate", index=200)
        entry_ids = [entry["id"] for entry in entries]
        assert browse.recall(db, entry_ids) == {"changed": 51, "active": True}
        view = browse.create(db, {"review": "all"})
        first_page = browse.feed(db, {"view": view["view_id"], "section": "main"})
        assert first_page["next_cursor"]
        view_id = view["view_id"]
        first_ids = [item["id"] for item in first_page["items"]]

    with box.connect() as db:
        resumed = browse.resume(db, view_id)
        assert resumed["view_id"] == view_id
        assert resumed["counts"] == {"main": 51, "stored": 1, "total": 52}
        assert {item["id"] for item in resumed["items"]} == set(first_ids)
        assert all(item["recalled"] for item in resumed["items"])

        tail = browse.feed(
            db,
            {
                "view": view_id,
                "section": "main",
                "cursor": first_page["next_cursor"],
            },
        )
        assert len(tail["items"]) == 1
        assert tail["items"][0]["id"] not in first_ids
        duplicate_page = browse.feed(db, {"view": view_id, "section": "duplicate"})
        assert [item["id"] for item in duplicate_page["items"]] == [duplicate["id"]]
        with pytest.raises(ValueError):
            browse.resume(db, view_id, owner="other")
        with pytest.raises(ValueError):
            browse.feed(db, {"view": view_id, "section": "main"}, owner="other")


def test_old_store_items_contract_and_lazy_detail_remain_available(review_db):
    db, box = review_db
    entry = _seed(
        db,
        box,
        "compatibility",
        index=1,
        title="Compatibility fixture",
        body="Full body is loaded only through detail.",
        result={"items": [{"item": {"model": "Fixture"}, "decision": "push"}]},
    )
    page = store.items(db, {"page": 1, "page_size": 50})
    assert page["page"] == 1
    assert page["page_size"] == 50
    assert page["total"] == 1
    item = page["items"][0]
    assert item["id"] == entry["id"]
    assert "body" not in item

    view = browse.create(db, {})
    browse_item = view["items"][0]
    assert "body" not in browse_item
    assert store.detail(db, entry["id"])["body"] == "Full body is loaded only through detail."


def test_web_browse_feed_recall_updates_auth_csrf_and_owner_permissions(web_harness):
    client, box = web_harness
    with box.connect() as db:
        entry = _seed(db, box, "web-layered", index=1, status="excluded")
        foreign_view = browse.create(db, {"review": "all"}, owner="other")
        before_events = db.execute("SELECT count(*) FROM events").fetchone()[0]

    assert client.get(f"{API}/feed", params={"view": foreign_view["view_id"]}).status_code == 401
    assert client.post(f"{API}/browse", json={"review": "all"}).status_code == 401
    assert client.get(f"{API}/browse-updates", params={"view": foreign_view["view_id"]}).status_code == 401

    csrf = _login(client, box)
    headers = _csrf_headers(csrf)
    assert client.get(f"{API}/browse").status_code == 405
    assert client.post(f"{API}/feed", json={}).status_code == 405
    assert client.post(f"{API}/browse", headers={"Origin": BASE_URL}, json={"review": "all"}).status_code == 403

    created = client.post(f"{API}/browse", headers=headers, json={"review": "all"})
    assert created.status_code == 200
    view = created.json()
    assert view["view_id"]

    feed_response = client.get(
        f"{API}/feed",
        params={"view": view["view_id"], "section": "main"},
    )
    assert feed_response.status_code == 200
    assert feed_response.json()["items"][0]["id"] == entry["id"]
    assert "body" not in feed_response.json()["items"][0]
    assert client.get(
        f"{API}/browse-updates", params={"view": view["view_id"]}
    ).json() == {"has_updates": False}

    assert client.get(
        f"{API}/feed", params={"view": foreign_view["view_id"]}
    ).status_code == 400
    assert client.post(
        f"{API}/recall",
        headers=headers,
        json={"targets": [entry["id"]], "active": True},
    ).json() == {"changed": 1, "active": True}
    assert client.post(
        f"{API}/recall",
        headers=headers,
        json={"targets": [entry["id"]], "active": True},
    ).json() == {"changed": 0, "active": True}
    assert client.post(
        f"{API}/recall",
        headers={"Origin": BASE_URL},
        json={"targets": [entry["id"]], "active": True},
    ).status_code == 403

    with box.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == before_events

