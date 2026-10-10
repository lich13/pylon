"""Synthetic HTTP-boundary regressions for the shared intelligence workspace."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass

import pytest
from starlette.testclient import TestClient

from pylon.outbox import Outbox
from pylon.tasks.intelligence import policy
from pylon.tasks.intelligence.store import Store as IntelligenceStore
from pylon.tasks.marketplace import control, web, web_auth
from pylon.tasks.marketplace.store import Store as MarketplaceStore


BASE_URL = "https://example.invalid"
MARKET_API = "/pylon/market/api"
INTEL_API = "/pylon/intel/api"


@dataclass
class WebHarness:
    store: MarketplaceStore
    client: TestClient

    @property
    def origin(self) -> str:
        return BASE_URL


@pytest.fixture
def web_harness(tmp_path, monkeypatch) -> WebHarness:
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
                "blocked_types": [],
                "partial_types": [],
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

    store = MarketplaceStore(Outbox(outbox_path))
    with store.box.connect() as db:
        control.baseline(db)
    app = web.create_app(store=store, secure=False)
    return WebHarness(store=store, client=TestClient(app, base_url=BASE_URL))


def issue_token(harness: WebHarness) -> str:
    with harness.store.box.connect() as db:
        return web_auth.issue_login(db, profile="webops")


def login(harness: WebHarness, path: str = MARKET_API) -> str:
    response = harness.client.post(f"{path}/login", json={"token": issue_token(harness)})
    assert response.status_code == 200, response.text
    csrf = response.json().get("csrf")
    assert isinstance(csrf, str) and csrf
    return csrf


def headers(harness: WebHarness, csrf: str) -> dict[str, str]:
    return {"Origin": harness.origin, "X-CSRF-Token": csrf}


def intel_state(harness: WebHarness) -> dict:
    response = harness.client.get(f"{INTEL_API}/state")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert {"active", "draft", "catalog", "groups", "sources", "counts", "csrf"} <= set(payload)
    assert {"version", "content"} <= set(payload["active"])
    assert {"id", "base", "revision", "content", "preview", "status"} <= set(payload["draft"])
    return payload


def intel_content(content: dict) -> dict:
    value = copy.deepcopy(content)
    value["enabled"] = True
    value["categories"] = ["B1"]
    value["blocked_groups"] = []
    value["category_sources"] = {
        key: (["linuxdo"] if key == "B1" else []) for key in policy.CATALOG
    }
    value["exclude_terms"] = {}
    value["include_terms"] = {}
    return value


def test_market_login_session_is_shared_with_intelligence_workspace(web_harness: WebHarness):
    csrf = login(web_harness, MARKET_API)

    state = intel_state(web_harness)

    assert state["csrf"] == csrf
    assert web_harness.client.get(f"{MARKET_API}/state").status_code == 200


def test_intelligence_mutation_requires_the_shared_session_csrf_and_origin(
    web_harness: WebHarness,
):
    csrf = login(web_harness)
    state = intel_state(web_harness)
    payload = {
        "id": state["draft"]["id"],
        "revision": state["draft"]["revision"],
        "content": intel_content(state["draft"]["content"]),
    }

    missing = web_harness.client.post(f"{INTEL_API}/draft", json=payload)
    assert missing.status_code == 403
    wrong_origin = web_harness.client.post(
        f"{INTEL_API}/draft",
        json=payload,
        headers={"Origin": "https://attacker.example", "X-CSRF-Token": csrf},
    )
    assert wrong_origin.status_code == 403
    assert intel_state(web_harness)["draft"]["revision"] == state["draft"]["revision"]


def test_intelligence_draft_preview_and_apply_are_explicit_and_idempotent(
    web_harness: WebHarness,
):
    csrf = login(web_harness)
    state = intel_state(web_harness)
    draft = state["draft"]
    content = intel_content(draft["content"])

    edited = web_harness.client.post(
        f"{INTEL_API}/draft",
        json={"id": draft["id"], "revision": draft["revision"], "content": content},
        headers=headers(web_harness, csrf),
    )
    assert edited.status_code == 200, edited.text
    current = edited.json()["draft"]
    assert current["revision"] == draft["revision"] + 1

    preview = web_harness.client.post(
        f"{INTEL_API}/preview",
        json={"id": current["id"], "revision": current["revision"]},
        headers=headers(web_harness, csrf),
    )
    assert preview.status_code == 200, preview.text
    preview_payload = preview.json()
    assert {"push", "skip", "hold"} <= set(preview_payload["counts"])
    assert isinstance(preview_payload["hash"], str) and preview_payload["hash"]

    request = {
        "id": current["id"],
        "revision": current["revision"],
        "hash": preview_payload["hash"],
    }
    applied = web_harness.client.post(
        f"{INTEL_API}/apply", json=request, headers=headers(web_harness, csrf)
    )
    assert applied.status_code == 200, applied.text
    version = applied.json()["version"]
    assert version == state["active"]["version"] + 1

    repeated = web_harness.client.post(
        f"{INTEL_API}/apply", json=request, headers=headers(web_harness, csrf)
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"version": version}
    assert intel_state(web_harness)["active"]["version"] == version


def test_intelligence_items_preserve_helper_scope_and_do_not_fetch_original_or_external_links(
    web_harness: WebHarness,
):
    login(web_harness)
    intelligence = IntelligenceStore(web_harness.store.box)
    assert intelligence.ingest(
        "linuxdo",
        "fixture-intel-1",
        title="合成 Helper 摘要",
        body="摘要中写明一个合成方法，外链仅供查看。",
        url="https://example.invalid/intel/fixture-intel-1",
        evidence_scope="excerpt",
        context={"evidence_scope": "helper_excerpt", "links": ["https://example.invalid/resource"]},
    )

    response = web_harness.client.get(f"{INTEL_API}/items?status=pending")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "linuxdo"
    assert items[0]["evidence_scope"] == "excerpt"
    assert items[0]["url"] == "https://example.invalid/intel/fixture-intel-1"
