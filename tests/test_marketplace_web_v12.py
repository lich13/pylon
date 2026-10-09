"""Web owner controls for the v12 marketplace policy surface.

These tests use an isolated Outbox/Store and synthetic marketplace posts.  They
exercise the HTTP boundary only; no browser or production profile is involved.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass

import pytest
from starlette.testclient import TestClient

from pylon.outbox import Outbox
from pylon.tasks.marketplace import control, web, web_auth
from pylon.tasks.marketplace.store import Store


API = "/pylon/market/api"
BASE_URL = "https://example.invalid"


@dataclass
class WebHarness:
    store: Store
    app: object
    client: TestClient

    @property
    def origin(self) -> str:
        return BASE_URL


@pytest.fixture
def web_harness(tmp_path, monkeypatch) -> WebHarness:
    """Build a private, deterministic web app backed by one SQLite Outbox."""
    monkeypatch.setenv("PYLON_HOME", str(tmp_path))
    outbox_path = tmp_path / "data" / "outbox.sqlite3"
    monkeypatch.setenv("PYLON_OUTBOX", str(outbox_path))
    config = tmp_path / "config"
    config.mkdir(parents=True)
    (config / "marketplace.json").write_text(
        json.dumps(
            {
                "version": 3,
                "enabled": True,
                "categories": [],
                "groups": [],
                "types": [],
                "blocked_groups": [],
                "blocked_categories": [],
                "filters": {},
            },
            ensure_ascii=False,
        )
    )
    (config / "marketplace-wanted.json").write_text(
        json.dumps(
            {"version": 2, "enabled": False, "targets": [], "blocked_groups": []},
            ensure_ascii=False,
        )
    )

    store = Store(Outbox(outbox_path))
    with store.box.connect() as db:
        control.baseline(db)

    app = web.create_app(store=store)
    client = TestClient(app, base_url=BASE_URL)
    return WebHarness(store=store, app=app, client=client)


def _issue_token(harness: WebHarness) -> str:
    with harness.store.box.connect() as db:
        return web_auth.issue_login(db, profile="webops")


def _login(harness: WebHarness, client: TestClient | None = None) -> str:
    client = client or harness.client
    response = client.post(f"{API}/login", json={"token": _issue_token(harness)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload.get("csrf"), str) and payload["csrf"]
    return payload["csrf"]


def _headers(harness: WebHarness, csrf: str) -> dict[str, str]:
    return {"Origin": harness.origin, "X-CSRF-Token": csrf}


def _state(client: TestClient) -> dict:
    response = client.get(f"{API}/state")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert {"active", "draft", "catalog", "groups", "fields", "csrf"} <= set(payload)
    assert {"version", "content"} <= set(payload["active"])
    assert {"id", "base", "revision", "content", "preview", "status"} <= set(payload["draft"])
    return payload


def _draft_update(
    harness: WebHarness,
    client: TestClient,
    csrf: str,
    state: dict,
    content: dict,
):
    draft = state["draft"]
    response = client.post(
        f"{API}/draft",
        json={"id": draft["id"], "revision": draft["revision"], "content": content},
        headers=_headers(harness, csrf),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert {"active", "draft", "catalog", "groups", "fields", "csrf"} <= set(payload)
    assert payload["draft"]["id"] == draft["id"]
    assert payload["draft"]["revision"] == draft["revision"] + 1
    return payload


def _select_phone(content: dict) -> dict:
    updated = copy.deepcopy(content)
    updated.setdefault("sale", {})["categories"] = ["phone.smartphone"]
    return updated


def _seed_post(store: Store, source_id: str) -> None:
    assert store.ingest(
        "v2ex",
        [
            {
                "id": source_id,
                "title": "出 iPhone 15 Pro",
                "url": f"https://example.invalid/t/{source_id}",
                "body_full": "iPhone 15 Pro 256G 4800元出",
                "body_complete": True,
            }
        ],
    ) == 1


def test_public_shell_has_no_private_session_content(web_harness: WebHarness):
    response = web_harness.client.get("/pylon/market/")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "market_web_tokens" not in response.text

    csrf = _login(web_harness)
    assert csrf not in response.text


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("GET", f"{API}/state", None),
        ("POST", f"{API}/draft", {"id": "missing", "revision": 1, "content": {}}),
        ("POST", f"{API}/preview", {"id": "missing", "revision": 1}),
        ("POST", f"{API}/apply", {"id": "missing", "revision": 1, "hash": "0" * 64}),
        ("POST", f"{API}/logout", None),
    ],
)
def test_data_routes_require_a_web_session(web_harness: WebHarness, method, path, payload):
    response = web_harness.client.request(method, path, json=payload)

    assert response.status_code == 401


def test_login_token_is_hashed_single_use_and_expires(web_harness: WebHarness):
    token = _issue_token(web_harness)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with web_harness.store.box.connect() as db:
        row = db.execute(
            "SELECT hash, expires_at, used_at FROM market_web_tokens WHERE hash=?",
            (token_hash,),
        ).fetchone()
    assert row is not None
    assert row["hash"] == token_hash
    assert token not in str(dict(row))
    assert row["expires_at"] > time.time()
    assert row["used_at"] is None

    first_client = TestClient(web_harness.app, base_url=BASE_URL)
    first = first_client.post(f"{API}/login", json={"token": token})
    assert first.status_code == 200, first.text
    cookie = first.headers.get("set-cookie", "").lower()
    assert "httponly" in cookie
    assert "secure" in cookie

    second_client = TestClient(web_harness.app, base_url=BASE_URL)
    assert second_client.post(f"{API}/login", json={"token": token}).status_code == 401
    with web_harness.store.box.connect() as db:
        assert db.execute(
            "SELECT used_at FROM market_web_tokens WHERE hash=?", (token_hash,)
        ).fetchone()[0] is not None

    expired = _issue_token(web_harness)
    expired_hash = hashlib.sha256(expired.encode()).hexdigest()
    with web_harness.store.box.connect() as db:
        db.execute(
            "UPDATE market_web_tokens SET expires_at=? WHERE hash=?",
            (time.time() - 1, expired_hash),
        )
    expired_client = TestClient(web_harness.app, base_url=BASE_URL)
    assert expired_client.post(f"{API}/login", json={"token": expired}).status_code == 401


def test_login_state_cookie_and_csrf_are_required_for_mutations(web_harness: WebHarness):
    csrf = _login(web_harness)
    before = _state(web_harness.client)
    content = _select_phone(before["draft"]["content"])

    missing_csrf = web_harness.client.post(
        f"{API}/draft",
        json={"id": before["draft"]["id"], "revision": before["draft"]["revision"], "content": content},
        headers={"Origin": web_harness.origin},
    )
    assert missing_csrf.status_code == 403

    wrong_origin = web_harness.client.post(
        f"{API}/draft",
        json={"id": before["draft"]["id"], "revision": before["draft"]["revision"], "content": content},
        headers={"Origin": "https://attacker.example", "X-CSRF-Token": csrf},
    )
    assert wrong_origin.status_code == 403
    assert _state(web_harness.client)["draft"]["revision"] == before["draft"]["revision"]

    assert _draft_update(web_harness, web_harness.client, csrf, before, content)["csrf"] == csrf


def test_logout_revokes_the_session(web_harness: WebHarness):
    csrf = _login(web_harness)
    assert web_harness.client.get(f"{API}/state").status_code == 200

    logout = web_harness.client.post(
        f"{API}/logout",
        headers=_headers(web_harness, csrf),
    )
    assert logout.status_code == 200
    assert web_harness.client.get(f"{API}/state").status_code == 401


def test_stale_draft_revision_is_rejected_with_conflict(web_harness: WebHarness):
    csrf = _login(web_harness)
    original = _state(web_harness.client)
    content = _select_phone(original["draft"]["content"])
    updated = _draft_update(web_harness, web_harness.client, csrf, original, content)

    stale = web_harness.client.post(
        f"{API}/draft",
        json={
            "id": original["draft"]["id"],
            "revision": original["draft"]["revision"],
            "content": original["draft"]["content"],
        },
        headers=_headers(web_harness, csrf),
    )
    assert stale.status_code == 409
    assert _state(web_harness.client)["draft"]["revision"] == updated["draft"]["revision"]


def test_draft_and_session_survive_store_restart(web_harness: WebHarness, tmp_path):
    csrf = _login(web_harness)
    original = _state(web_harness.client)
    updated = _draft_update(
        web_harness,
        web_harness.client,
        csrf,
        original,
        _select_phone(original["draft"]["content"]),
    )

    restarted_store = Store(Outbox(web_harness.store.box.path))
    restarted_app = web.create_app(store=restarted_store)
    restarted_client = TestClient(restarted_app, base_url=BASE_URL)
    restarted_client.cookies.update(web_harness.client.cookies)

    recovered = _state(restarted_client)
    assert recovered["draft"] == updated["draft"]
    assert recovered["active"] == updated["active"]
    assert recovered["csrf"] == csrf


def test_preview_hash_remains_applyable_when_backend_adds_a_post(web_harness: WebHarness):
    csrf = _login(web_harness)
    initial = _state(web_harness.client)
    _seed_post(web_harness.store, "1201")
    updated = _draft_update(
        web_harness,
        web_harness.client,
        csrf,
        initial,
        _select_phone(initial["draft"]["content"]),
    )
    draft = updated["draft"]
    preview = web_harness.client.post(
        f"{API}/preview",
        json={"id": draft["id"], "revision": draft["revision"]},
        headers=_headers(web_harness, csrf),
    )
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert isinstance(preview_payload.get("hash"), str) and preview_payload["hash"]

    _seed_post(web_harness.store, "1202")
    applied = web_harness.client.post(
        f"{API}/apply",
        json={"id": draft["id"], "revision": draft["revision"], "hash": preview_payload["hash"]},
        headers=_headers(web_harness, csrf),
    )
    assert applied.status_code == 200, applied.text
    assert isinstance(applied.json().get("version"), int)


def test_apply_rejects_a_concurrent_policy_change(web_harness: WebHarness):
    csrf = _login(web_harness)
    initial = _state(web_harness.client)
    updated = _draft_update(
        web_harness,
        web_harness.client,
        csrf,
        initial,
        _select_phone(initial["draft"]["content"]),
    )
    draft = updated["draft"]
    preview = web_harness.client.post(
        f"{API}/preview",
        json={"id": draft["id"], "revision": draft["revision"]},
        headers=_headers(web_harness, csrf),
    )
    assert preview.status_code == 200, preview.text
    preview_hash = preview.json()["hash"]

    with web_harness.store.box.connect() as db:
        base, active_content = control.baseline(db)
        concurrent = copy.deepcopy(active_content)
        concurrent.setdefault("sale", {})["categories"] = ["component.gpu"]
        concurrent_version = control.activate(db, concurrent, base, "concurrent-test")

    response = web_harness.client.post(
        f"{API}/apply",
        json={"id": draft["id"], "revision": draft["revision"], "hash": preview_hash},
        headers=_headers(web_harness, csrf),
    )
    assert response.status_code == 409
    with web_harness.store.box.connect() as db:
        assert db.execute("SELECT version FROM market_active WHERE id=1").fetchone()[0] == concurrent_version
        assert db.execute("SELECT COUNT(*) FROM market_versions").fetchone()[0] == 2


def test_apply_is_idempotent_and_does_not_create_another_active_version(web_harness: WebHarness):
    csrf = _login(web_harness)
    initial = _state(web_harness.client)
    updated = _draft_update(
        web_harness,
        web_harness.client,
        csrf,
        initial,
        _select_phone(initial["draft"]["content"]),
    )
    draft = updated["draft"]
    preview = web_harness.client.post(
        f"{API}/preview",
        json={"id": draft["id"], "revision": draft["revision"]},
        headers=_headers(web_harness, csrf),
    )
    assert preview.status_code == 200, preview.text
    request = {
        "id": draft["id"],
        "revision": draft["revision"],
        "hash": preview.json()["hash"],
    }

    first = web_harness.client.post(f"{API}/apply", json=request, headers=_headers(web_harness, csrf))
    assert first.status_code == 200, first.text
    first_payload = first.json()
    assert isinstance(first_payload.get("version"), int)

    repeated = web_harness.client.post(f"{API}/apply", json=request, headers=_headers(web_harness, csrf))
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first_payload
    with web_harness.store.box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM market_versions").fetchone()[0] == 2
        assert db.execute("SELECT version FROM market_active WHERE id=1").fetchone()[0] == first_payload["version"]
