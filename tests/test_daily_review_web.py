import json

import pytest
from starlette.testclient import TestClient

from pylon.outbox import Outbox
from pylon.tasks.daily_review import feedback, store as review_store
from pylon.tasks.intelligence import store as intelligence_store
from pylon.tasks.marketplace import web
from pylon.tasks.marketplace import web_auth


BASE_URL = "https://example.invalid"
API = "/pylon/review/api"


@pytest.fixture
def web_harness(tmp_path):
    box = Outbox(str(tmp_path / "review-web.sqlite3"))
    intel = intelligence_store.Store(box)
    app = web.create_app(store=intel)
    client = TestClient(app, base_url=BASE_URL)
    return client, box


def _login(client, box):
    with box.connect() as db:
        token = web_auth.issue_login(db, profile="webops")
    response = client.post(f"{API}/login", json={"token": token})
    assert response.status_code == 200
    return response.json()["csrf"]


def _csrf_headers(csrf, *, origin=BASE_URL):
    return {"Origin": origin, "X-CSRF-Token": csrf}


def _seed_ready(box):
    with box.connect() as db:
        snapshot = review_store.capture(
            db,
            source="v2ex",
            source_id="web-contract-1",
            lane="intel",
            title="Web contract fixture",
            body="Fixture body from example.invalid",
            url="https://example.invalid/item/web-contract-1",
            first_seen="2026-10-11T00:00:00+08:00",
        )
        entry_id = review_store.record(
            db,
            source="v2ex",
            source_id="web-contract-1",
            lane="intel",
            status="ready",
            result={
                "items": [
                    {
                        "item": {"model": "Fixture", "provider": "Example"},
                        "decision": "push",
                    }
                ]
            },
            rule_version="web-v1",
            snapshot_id=snapshot,
        )
        latest = review_store.latest(db, entry_id)
        return {
            "id": entry_id,
            "snapshot_id": snapshot,
            "decision_ids": [row["id"] for row in latest],
            "item_key": f"{latest[0]['id']}:0",
        }


def test_review_page_and_api_enforce_auth_csrf_and_safe_script_contract(web_harness):
    client, box = web_harness

    page = client.get("/pylon/review/")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert '<script src="review.js"' in page.text

    unauthorized = client.get(f"{API}/state")
    assert unauthorized.status_code == 401

    csrf = _login(client, box)
    state = client.get(f"{API}/state")
    assert state.status_code == 200
    assert state.json()["csrf"] == csrf

    missing_csrf = client.post(f"{API}/feedback", json={"actions": ["category"]})
    assert missing_csrf.status_code == 403

    wrong_origin = client.post(
        f"{API}/feedback",
        headers=_csrf_headers(csrf, origin="https://attacker.invalid"),
        json={"actions": ["category"]},
    )
    assert wrong_origin.status_code == 403

    script = client.get("/pylon/review/review.js")
    assert script.status_code == 200
    assert "const esc=" in script.text
    assert "textContent" in script.text


def test_feedback_submit_does_not_change_policy_or_emit_notification(web_harness):
    client, box = web_harness
    entry = _seed_ready(box)
    csrf = _login(client, box)
    headers = _csrf_headers(csrf)

    with box.connect() as db:
        before_events = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        before_versions = db.execute("SELECT COUNT(*) FROM intel_versions").fetchone()[0]
        before_decisions = db.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]

    feedback_response = client.post(
        f"{API}/feedback",
        headers=headers,
        json={
            "targets": [
                {
                    "entry_id": entry["id"],
                    "item_key": entry["item_key"],
                    "snapshot_id": entry["snapshot_id"],
                    "revision": 0,
                    "decision_ids": entry["decision_ids"],
                }
            ],
            "value": {"actions": ["category"], "scope": "item", "category": "product"},
        },
    )
    assert feedback_response.status_code == 200
    saved = feedback_response.json()["saved"][0]
    assert saved["entry_id"] == entry["id"]
    assert json.loads(saved["value"])["scope"] == "item"
    assert saved["item_key"] == entry["item_key"]

    submit_response = client.post(
        f"{API}/submit",
        headers=headers,
        json={"targets": [{"id": saved["id"], "revision": saved["revision"]}]},
    )
    assert submit_response.status_code == 200
    submitted = submit_response.json()
    assert submitted["count"] == 1
    assert submitted["batch_id"]

    batch = client.get(f"{API}/batch", params={"id": submitted["batch_id"]})
    assert batch.status_code == 200
    batch_feedback = batch.json()["feedback"][0]
    assert batch_feedback["value"]["snapshot"]["body"] == "Fixture body from example.invalid"
    assert batch_feedback["value"]["actions"] == ["category"]

    with box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before_events
        assert db.execute("SELECT COUNT(*) FROM intel_versions").fetchone()[0] == before_versions
        assert db.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0] == before_decisions

