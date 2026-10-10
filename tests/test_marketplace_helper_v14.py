"""Synthetic v14 regressions for finite Linux.do Helper evidence."""

from copy import deepcopy
import json

import pytest

from pylon.outbox import Outbox
from pylon.tasks.marketplace import ai_prices
from pylon.tasks.marketplace import classifier, enrich, policy
from pylon.tasks.marketplace.pipeline import _quote_matches, format_message
from pylon.tasks.marketplace.store import Store


def helper_post(title: str, body: str) -> dict:
    return {
        "source": "linuxdo",
        "source_id": "helper-fixture-1",
        "title": title,
        "body": body,
        "body_complete": False,
        "source_context": {"evidence_scope": "helper_excerpt"},
        "url": "https://example.invalid/linuxdo/helper-fixture-1",
    }


def helper_items(title: str, body: str) -> list[dict]:
    return classifier.classify_listing(
        title,
        body,
        "linuxdo",
        False,
        evidence_scope="helper_excerpt",
    )


def revision_post(tmp_path, *, revision: str = "helper-rev-a"):
    store = Store(Outbox(tmp_path / "helper-revision.sqlite3"))
    assert store.ingest("linuxdo", [{
        "id": "1",
        "url": "https://linux.do/t/topic/1",
        "title": "出 iPhone 15 Pro",
        "body_full": "iPhone 15 Pro 256G，售价2999元，可邮寄。",
        "body_complete": False,
        "source_context": {
            "evidence_scope": "helper_excerpt",
            "snapshot": {"fingerprint": revision},
        },
        "body_provenance": {"source": "helper-fixture"},
    }]) == 1
    return store


def test_helper_excerpt_keeps_body_evidence_when_body_is_not_complete():
    title = "出 iPhone 15 Pro"
    body = "iPhone 15 Pro 256G，售价2999元，可邮寄"

    items = helper_items(title, body)

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "phone.smartphone"
    assert item["price"] == 2999
    assert item["attributes"]["body_complete"] is False
    assert item["attributes"]["evidence_scope"] == "helper_excerpt"
    assert "2999" in item["text"]
    assert "2999" in item["attributes"]["price_evidence"]


def test_incomplete_forum_body_keeps_the_title_only_default_scope():
    title = "出 iPhone 15 Pro"
    body = "售价2999元，可邮寄"

    item = classifier.classify_listing(title, body, "v2ex", False)[0]

    assert item["category"] == "phone.smartphone"
    assert item["price"] is None
    assert item["attributes"]["body_complete"] is False
    assert item["attributes"]["evidence_scope"] == "title"
    assert "2999" not in item["text"]


def test_unknown_helper_price_is_missing_and_never_zero():
    items = helper_items(
        "出 iPhone 15 Pro",
        "iPhone 15 Pro 256G，价格面议，可邮寄",
    )

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "phone.smartphone"
    assert item["price"] is None
    assert item["price"] != 0
    assert item["attributes"]["price_status"] in {"missing", "ambiguous"}


def test_only_a_configured_missing_attribute_requests_review():
    item = helper_items(
        "出 iPhone 15 Pro",
        "iPhone 15 Pro 256G，售价2999元，可邮寄",
    )[0]
    no_filter = {"filters": {}}
    configured_filter = {
        "filters": {
            "phone.smartphone": {"network": {"include": ["5G"]}},
        }
    }

    assert policy.attribute_decision(item, no_filter) == ("match", [])
    assert policy.attribute_decision(item, configured_filter) == ("review", ["network"])


def test_helper_excerpt_does_not_trigger_entity_price_matching():
    items = helper_items(
        "出 iPhone 15 Pro",
        "iPhone 15 Pro 256G，售价2999元，可邮寄",
    )
    quote_fixture = [{"product_name": "iPhone 15 Pro", "sale_price": 2800}]

    assert _quote_matches(items, False, quote_fixture) == []


def test_helper_summary_labels_unknown_conditions_without_a_price_comparison():
    title = "出 iPhone 15 Pro"
    body = "iPhone 15 Pro 256G，价格面议，可邮寄"
    post = helper_post(title, body)
    items = helper_items(title, body)

    message = format_message(post, items, [])

    assert "依据 Helper 摘要" in message
    assert "价格未标" in message
    assert "重点比价" not in message
    assert "0 元" not in message


def test_helper_summary_preserves_evidence_for_a_known_price():
    title = "出 iPhone 15 Pro"
    body = "iPhone 15 Pro 256G，售价2999元，可邮寄"
    post = helper_post(title, body)
    item = helper_items(title, body)[0]
    result = format_message(post, [deepcopy(item)], [])

    assert "依据 Helper 摘要" in result
    assert "2999 元" in result
    assert "价格未标" not in result


def test_same_helper_body_with_a_new_revision_gets_a_new_analysis_fingerprint(tmp_path):
    first = revision_post(tmp_path, revision="helper-rev-a")
    with first.box.connect() as db:
        post_a = dict(db.execute("SELECT * FROM marketplace_posts WHERE source_id='1'").fetchone())
    second = Store(Outbox(first.box.path))
    with second.box.connect() as db:
        context = json.loads(db.execute("SELECT source_context FROM marketplace_posts WHERE source_id='1'").fetchone()[0])
        context["snapshot"]["fingerprint"] = "helper-rev-b"
        db.execute("UPDATE marketplace_posts SET source_context=? WHERE source='linuxdo' AND source_id='1'",
                   (json.dumps(context, ensure_ascii=False),))
        post_b = dict(db.execute("SELECT * FROM marketplace_posts WHERE source_id='1'").fetchone())

    assert post_a["body"] == post_b["body"]
    assert enrich.fingerprint(post_a, "fixture-model") != enrich.fingerprint(post_b, "fixture-model")
    assert ai_prices.fingerprint(post_a, {}) != ai_prices.fingerprint(post_b, {})


def test_ai_price_queue_does_not_claim_an_awaiting_helper_row(tmp_path):
    store = revision_post(tmp_path)
    with store.box.connect() as db:
        db.execute("UPDATE marketplace_posts SET status='awaiting_helper' WHERE source='linuxdo' AND source_id='1'")
        db.execute("INSERT INTO market_ai_prices(fingerprint,source,source_id,status) VALUES(?,?,?,'pending')",
                   ("fixture-ai-revision", "linuxdo", "1"))

    assert ai_prices.run_pending(store, limit=1) == 0
    with store.box.connect() as db:
        assert tuple(db.execute("SELECT status,attempts FROM market_ai_prices").fetchone()) == ("pending", 0)


@pytest.mark.parametrize("failure", [False, True])
def test_inflight_helper_edit_success_or_error_cannot_create_old_review(tmp_path, monkeypatch, failure):
    store = revision_post(tmp_path)
    with store.box.connect() as db:
        db.execute("UPDATE marketplace_posts SET status='pending_analysis' WHERE source='linuxdo' AND source_id='1'")
        post = dict(db.execute("SELECT * FROM marketplace_posts WHERE source_id='1'").fetchone())
        fingerprint = enrich.fingerprint(post, "fixture-model")
        db.execute("INSERT INTO marketplace_analyses(fingerprint,source,source_id,status,updated_at,model,requirements) "
                   "VALUES(?,?,?,?,?,?,?)", (fingerprint, "linuxdo", "1", "pending", 0, "fixture-model", "[]"))

    def revised(_post):
        with store.box.connect() as db:
            context = json.loads(db.execute("SELECT source_context FROM marketplace_posts WHERE source_id='1'").fetchone()[0])
            context["snapshot"]["fingerprint"] = "helper-rev-edited"
            context["helper_stale"] = True
            db.execute("UPDATE marketplace_posts SET source_context=?,status='awaiting_helper' WHERE source='linuxdo' AND source_id='1'",
                       (json.dumps(context, ensure_ascii=False),))
        if failure:
            raise enrich.AnalysisError("fixture validation failure")
        return [], "fixture-request"

    monkeypatch.setattr(enrich, "request", revised)

    assert enrich.run_pending(store, limit=1) == 0
    with store.box.connect() as db:
        analysis = db.execute("SELECT status,error FROM marketplace_analyses WHERE fingerprint=?", (fingerprint,)).fetchone()
        post_row = db.execute("SELECT status FROM marketplace_posts WHERE source='linuxdo' AND source_id='1'").fetchone()
        reviews = db.execute("SELECT * FROM market_reviews").fetchall()
    assert tuple(analysis) == ("error", "source_changed")
    assert post_row["status"] == "awaiting_helper"
    assert reviews == []
