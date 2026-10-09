"""Shared AI price gates and source-boundary regressions for marketplace v12."""

from __future__ import annotations

import copy
import json

import pytest

from pylon.outbox import Outbox
from pylon.tasks.linuxdo import trade, trade_store
from pylon.tasks.marketplace import ai_prices, classifier, enrich, pipeline, policy
from pylon.tasks.marketplace.store import Store
from tests.webops.test_linuxdo_trade import offer, price_config, proof, shared_offer, snapshot_for


AI_POLICY = {
    "enabled": True,
    "version": 12,
    "groups": ["virtual"],
    "categories": [],
    "blocked_groups": [],
    "blocked_categories": [],
    "filters": {},
}


def _post(value: dict, *, source: str = "v2ex", source_id: str = "1204") -> dict:
    description = value["description"]["value"]
    snapshot = snapshot_for(value, topic_id=source_id, title=description)
    return {
        "source": source,
        "source_id": source_id,
        "title": description,
        "body": "；".join(
            quote
            for key in ("description", "price", "term")
            if (quote := value[key]["quote"])
        ),
        "body_complete": True,
        "updated_at": 1_800_000_000,
        "posted_at": "2026-10-10T00:00:00Z",
        "url": f"https://example.invalid/t/{source_id}",
        "source_context": json.dumps({"snapshot": snapshot}, ensure_ascii=False),
    }


def _ai_offer(product: str, *, price: str = "90", term: str = "1") -> dict:
    names = {
        "gpt": "ChatGPT Pro 会员",
        "claude": "Claude Pro 会员",
        "grok": "Grok Pro 会员",
        "gemini": "Gemini Pro 会员",
    }
    value = offer(product=product, price=price, term=term, delivery="account", access="full")
    value["description"] = proof(names[product], names[product])
    return value


def _price_config(values: list[dict], *, monthly: str = "200", total: str = "300") -> dict:
    first = price_config(values[0], monthly=monthly, total=total)
    rules = []
    for index, value in enumerate(values):
        rule = copy.deepcopy(price_config(value, monthly=monthly, total=total)["rules"][0])
        rule["id"] = f"fixture-ai-{index}"
        rules.append(rule)
    first["rules"] = rules
    return trade.validate_config(first)


@pytest.mark.parametrize("source", ["v2ex", "chiphell", "nga", "linuxdo"])
def test_four_ai_platforms_share_one_offers_to_items_and_policy_gate(source):
    values = [_ai_offer(product) for product in ("gpt", "claude", "grok", "gemini")]
    items = ai_prices.offers_to_items(
        _post(values[0], source=source, source_id="1204"),
        {"intent": "offer", "offers": values},
        _price_config(values),
    )

    assert len(items) == 4
    assert {item["category"] for item in items} == {"virtual.ai_membership"}
    assert {item["attributes"]["ai_price_result"]["decision"] for item in items} == {"push"}
    assert policy.evaluate(items, AI_POLICY) == items


@pytest.mark.parametrize("intent", ["unknown", "wanted", "non_trade"])
def test_non_offer_intent_never_turns_ai_offers_into_sale_items(intent):
    value = _ai_offer("gpt")
    assert ai_prices.offers_to_items(
        _post(value, source_id="1206"),
        {"intent": intent, "offers": [value]},
        _price_config([value]),
    ) == []


@pytest.mark.parametrize(
    ("case", "expected_decision"),
    [
        ("missing_config", "hold"),
        ("over_budget", "skip"),
        ("term_too_long", "skip"),
        ("shared_participants_missing", "hold"),
    ],
)
def test_ai_price_gate_cannot_be_bypassed_by_missing_config_budget_term_or_people(
    case, expected_decision,
):
    if case == "shared_participants_missing":
        value = shared_offer()
        value["participants"] = proof()
        prices = price_config(
            value,
            monthly="400",
            total="400",
            car_reference="1000",
            reference_until="2099-01-01T00:00:00Z",
        )
    elif case == "term_too_long":
        value = _ai_offer("gpt", term="4")
        prices = _price_config([value], monthly="1000", total="5000")
    else:
        value = _ai_offer("gpt")
        prices = trade.default_config() if case == "missing_config" else _price_config([value], monthly="50", total="50")

    items = ai_prices.offers_to_items(
        _post(value, source_id="1205"),
        {"intent": "offer", "offers": [value]},
        prices,
    )

    result = items[0]["attributes"]["ai_price_result"]
    assert result["decision"] == expected_decision
    assert policy.evaluate(items, AI_POLICY) == []


def _delivery_store(tmp_path):
    store = Store(Outbox(tmp_path / "delivery.sqlite3"))
    with store.box.connect() as db:
        db.executescript(ai_prices.SCHEMA)
        trade_store.initialize(db)
    return store


def _priced_item(value: dict, result: dict) -> dict:
    return {
        "index": 0,
        "category": "virtual.ai_membership",
        "status": "available",
        "name": value["description"]["value"],
        "price": float(value["price"]["value"]),
        "attributes": {"ai_price_result": result},
    }


def test_delivery_preserves_old_linux_quote_dedupe_for_linuxdo_only(tmp_path):
    value = _ai_offer("gpt")
    snapshot = snapshot_for(value, topic_id="7201", title=value["description"]["value"])
    result = trade.evaluate(value, price_config(value), snapshot, now=1_800_000_000)
    assert result["decision"] == "push"
    item = _priced_item(value, result)
    store = _delivery_store(tmp_path)
    post = _post(value, source="linuxdo", source_id="7201")

    event_id = store.box.enqueue(
        profile="webops",
        job_id="linuxdo-trade",
        kind="text",
        payload={"text": "旧报价"},
        key="fixture-old-linux-quote",
    )
    store.box.finish(event_id, "delivered", receipt={"message_id": 1})
    with store.box.connect() as db:
        offer_key = trade_store.identity(snapshot, result)
        db.execute(
            "INSERT INTO linuxdo_trade_notifications VALUES(?,?,?,?,?,?)",
            (offer_key, "old-event", event_id, result["total"], result["monthly"], 1),
        )
        db.execute(
            "INSERT INTO linuxdo_trade_quotes VALUES(?,?,?,?,?)",
            ("7201", offer_key, "old-policy", json.dumps({"offer": value}), 1),
        )
        kept, claims = ai_prices.delivery(db, post, [item])

    assert kept == []
    assert claims == []
    assert item["attributes"]["delivery_excluded"]


def test_old_linux_quote_dedupe_does_not_authorize_a_different_source(tmp_path):
    value = _ai_offer("gpt")
    snapshot = snapshot_for(value, topic_id="7202", title=value["description"]["value"])
    result = trade.evaluate(value, price_config(value), snapshot, now=1_800_000_000)
    item = _priced_item(value, result)
    store = _delivery_store(tmp_path)
    post = _post(value, source="v2ex", source_id="7202")

    with store.box.connect() as db:
        kept, claims = ai_prices.delivery(db, post, [item])

    assert kept == [item]
    assert len(claims) == 1


def test_mixed_sale_excludes_apple_music_from_88vip_and_child_watch_without_hiding_phone():
    phone = classifier.classify_listing(
        "出 iPhone 15 Pro",
        "售价 4500 元，可邮寄。",
        "v2ex",
        True,
    )[0]
    apple_music = classifier.classify_listing(
        "出 88VIP 赠送 Apple Music 会员",
        "售价 20 元，可转让。",
        "v2ex",
        True,
    )[0]
    child_watch = classifier.classify_listing(
        "出 儿童智能手表 小天才 Z8",
        "售价 300 元，可邮寄。",
        "v2ex",
        True,
    )[0]
    mixed_policy = {
        **AI_POLICY,
        "groups": [],
        "categories": ["phone.smartphone", "virtual.music_membership"],
        "filters": {"virtual": {"origin": {"exclude": ["88VIP"]}}},
    }

    selected = policy.evaluate([phone, apple_music, child_watch], mixed_policy)

    assert [item["category"] for item in selected] == ["phone.smartphone"]
    assert apple_music["attributes"]["facts"]["origin"] == ["88VIP"]
    assert child_watch["category"] == "wearable.child_watch"


def test_grok_correction_to_ai_reenters_price_gate_before_outbox(tmp_path, monkeypatch):
    """A semantic correction cannot turn an unknown row into an unpriced send."""
    store = Store(Outbox(tmp_path / "correction.sqlite3"))
    post = {
        "source": "v2ex",
        "source_id": "1210",
        "title": "待核对商品",
        "body": "待核对商品，暂无明确交易条件。",
        "body_complete": True,
        "history": 0,
        "url": "https://example.invalid/t/1210",
    }
    store.ingest(
        "v2ex",
        [{"id": post["source_id"], "title": post["title"], "url": post["url"],
          "body_full": post["body"], "body_complete": True}],
    )
    corrected = [{
        "index": 0,
        "category": "virtual.ai_membership",
        "status": "available",
        "name": "ChatGPT Pro 会员",
        "price": 90,
        "attributes": {"facts": {"product": "ChatGPT Pro 会员"}, "intent": "sale"},
    }]
    monkeypatch.setattr(enrich, "prepare", lambda _db, _post, _items: corrected)
    monkeypatch.setattr(ai_prices, "config", lambda _db=None: trade.default_config())
    monkeypatch.setattr("pylon.tasks.quotes.health.inspect", lambda *_: {"available": False})

    assert pipeline.process(
        store,
        current_policy={"enabled": True, "version": 12, "groups": ["virtual"], "categories": []},
        wanted_policy={"enabled": False, "version": 12, "targets": []},
    ) == 0

    with store.box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT status FROM market_ai_prices").fetchone()[0] == "pending"
