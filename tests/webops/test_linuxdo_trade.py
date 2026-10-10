from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import stat
from unittest.mock import Mock

import pytest

from pylon.outbox import Outbox
from pylon.tasks.linuxdo import __main__ as linuxdo_main
from pylon.tasks.linuxdo import helper_account, public_review, trade, trade_store
from pylon.tasks.linuxdo.helper import SOURCE
from pylon.tasks.linuxdo.helper_store import HelperStore
from pylon.tasks.marketplace import linuxdo_bridge, pipeline as marketplace_pipeline
from pylon.tasks.marketplace.store import Store as MarketplaceStore
from pylon.tasks.linuxdo.store import Store


REF = "fixture:topic:1"
NOW = 1_800_000_000


def proof(value: str = "", quote: str = "", ref: str = REF) -> dict:
    return {"value": value, "ref": ref if value or quote else "", "quote": quote}


def offer(*, price="90", term="1", term_unit="month", delivery="codex", access="codex",
          participants="", personal_quota="", car_prices=None, product="gpt", tier="Pro",
          provider="", model="", quota="", quota_unit="", quota_period="", multiplier="",
          restrictions="", expires_at="", usable_until="", allocation="unknown", allocation_basis="") -> dict:
    term_label = "个月" if term_unit == "month" else "天"
    value = {
        "product": product,
        "utility": "useful",
        "delivery": delivery,
        "access": access,
        "condition": "normal",
        "currency": "CNY",
        "term_unit": term_unit,
        "allocation": allocation,
        "description": proof("Codex Pro", "Codex Pro"),
        "price": proof(str(price), f"售价{price}元"),
        "term": proof(str(term), f"期限{term}{term_label}"),
        "region": proof(),
        "provider": proof(provider, f"服务商{provider}" if provider else ""),
        "model": proof(model, f"模型{model}" if model else ""),
        "quota": proof(quota, f"额度{quota}{quota_unit}" if quota else ""),
        "quota_unit": proof(quota_unit, f"额度单位{quota_unit}" if quota_unit else ""),
        "quota_period": proof(quota_period, f"额度周期{quota_period}" if quota_period else ""),
        "multiplier": proof(multiplier, f"倍率{multiplier}" if multiplier else ""),
        "participants": proof(participants, f"总人数{participants}人含车主" if participants else ""),
        "personal_quota": proof(personal_quota, f"个人额度{personal_quota}" if personal_quota else ""),
        "restrictions": proof(restrictions, f"限制{restrictions}" if restrictions else ""),
        "warranty": proof(),
        "expires_at": proof(expires_at, f"截止{expires_at}" if expires_at else ""),
        "usable_until": proof(usable_until, f"使用到期{usable_until}" if usable_until else ""),
        "allocation_basis": proof(allocation_basis, allocation_basis),
        "minimum_quantity": proof(),
        "event": proof(),
        "tier": proof(tier, tier),
        "fees": [],
        "availability": "available",
        "change": "initial",
        "car_prices": car_prices or [],
    }
    return value


def snapshot_for(value: dict, *, topic_id="9201", title="Codex Pro 套餐说明", actor="fixture_seller") -> dict:
    quotes = []
    for item in [*trade.ATTRIBUTES, "tier"]:
        if value[item]["quote"]:
            quotes.append(value[item]["quote"])
    for fee in value["fees"]:
        quotes.append(fee["quote"])
    for part in value["car_prices"]:
        quotes.extend((part["count"]["quote"], part["price"]["quote"]))
    body = "；".join(dict.fromkeys(quotes))
    return {
        "topic_id": topic_id,
        "version": 1,
        "fingerprint": f"fixture-{topic_id}",
        "title": title,
        "historical": False,
        "actors": {REF: actor},
        "evidence": [{"ref": REF, "title": title, "body": body,
                      "source_published_at": "", "source_edited_at": ""}],
    }


def price_config(value: dict, *, monthly="200", total="300", unit="", car_reference="", reference_until="", shared_price_mode=None) -> dict:
    match_fields = {"product", "tier", "delivery", "access", "condition", "currency"}
    if value["delivery"] == "api":
        match_fields |= {"provider", "model", "quota_unit", "quota_period"}
    match = {key: value[key]["value"] if isinstance(value[key], dict) else value[key]
             for key in match_fields}
    config = {
        "version": 1,
        "revision": "fixture-confirmed-1",
        "confirmed_at": "2026-01-01T00:00:00Z",
        "rules": [{
            "id": "fixture-rule",
            "match": match,
            "monthly_max": monthly,
            "total_max": total,
            "minimum_days": "",
            "unit_max": unit,
            "car_monthly_reference": car_reference,
            "reference_until": reference_until,
        }],
    }
    if shared_price_mode is not None:
        config["rules"][0]["shared_price_mode"] = shared_price_mode
    return trade.validate_config(config)


def shared_offer(*, participants="3", personal_quota="100 GB/30天", car_counts=("1", "2"), car_prices=("200", "300")) -> dict:
    rows = [{"count": proof(count, f"{count}人档"), "price": proof(price, f"车价{price}元")}
            for count, price in zip(car_counts, car_prices)]
    return offer(price=car_prices[-1], delivery="shared", access="full", participants=participants,
                 personal_quota=personal_quota, car_prices=rows)


def api_offer(*, quota="100", multiplier="2", restrictions="仅官方API") -> dict:
    return offer(price="10", delivery="api", access="api", provider="fixture_provider",
                 model="fixture_model", quota=quota, quota_unit="tokens", quota_period="monthly",
                 multiplier=multiplier, restrictions=restrictions)


def test_trade_proofs_must_reference_exact_source_text_and_numeric_role():
    value = offer()
    snapshot = snapshot_for(value)

    validated = trade.validate({"intent": "offer", "offers": [value]}, snapshot)["offers"][0]
    assert validated["price"] == proof("90", "售价90元")
    assert validated["term"] == proof("1", "期限1个月")
    assert validated["description"]["value"] == "Codex Pro"

    unsupported_quote = deepcopy(value)
    unsupported_quote["description"]["quote"] = "未出现在来源中的套餐"
    with pytest.raises(trade.TradeError, match="offer quote absent"):
        trade.validate({"intent": "offer", "offers": [unsupported_quote]}, snapshot)

    unsupported_number = deepcopy(value)
    unsupported_number["price"]["value"] = "999"
    with pytest.raises(trade.TradeError, match="offer number absent"):
        trade.validate({"intent": "offer", "offers": [unsupported_number]}, snapshot)


def test_trade_cache_key_changes_with_focus_prices_model_and_prompt_revision(monkeypatch):
    value = offer()
    snapshot = snapshot_for(value)
    config = price_config(value)
    focus = {"categories": ["B1"]}

    baseline = trade.cache_key(snapshot, config, "fixture-model", focus=focus)

    assert baseline != trade.cache_key(snapshot, price_config(value, monthly="190"), "fixture-model", focus=focus)
    assert baseline != trade.cache_key(snapshot, config, "fixture-model-v2", focus=focus)
    assert baseline != trade.cache_key(snapshot, config, "fixture-model", focus={"categories": ["B1", "B2"]})
    monkeypatch.setattr(trade, "VERSION", "linuxdo-trade-fixture-prompt-v2")
    assert baseline != trade.cache_key(snapshot, config, "fixture-model", focus=focus)


@pytest.mark.parametrize("monthly_quote", ["500元/月", "每月500元"])
def test_explicit_chinese_participant_count_and_monthly_price_supply_numeric_evidence(monthly_quote):
    value = offer(price="500", participants="4")
    value["participants"] = proof("4", "含车主四人")
    value["term"] = proof("1", monthly_quote)
    value["price"] = proof("500", monthly_quote)

    validated = trade.validate({"intent": "offer", "offers": [value]}, snapshot_for(value))["offers"][0]

    assert validated["participants"]["value"] == "4"
    assert validated["price"]["value"] == "500"
    assert validated["term"]["value"] == "1"
    assert validated["term_unit"] == "month"


@pytest.mark.parametrize(("field", "amount", "quote"), [
    ("participants", "3", "车主加两位乘客"),
    ("participants", "4", "两人加两人"),
    ("term", "30", "一个月"),
    ("price", "250", "每月500元两人"),
])
def test_numbers_cannot_be_fabricated_by_arithmetic_or_unproven_unit_conversion(field, amount, quote):
    value = offer()
    value[field] = proof(amount, quote)
    if field == "term":
        value["term_unit"] = "day"

    with pytest.raises(trade.TradeError, match="offer number absent"):
        trade.validate({"intent": "offer", "offers": [value]}, snapshot_for(value))


@pytest.mark.parametrize("field", [
    "description", "personal_quota", "restrictions", "warranty", "allocation_basis", "event",
])
def test_display_claims_are_copied_from_evidence_without_mutating_model_output(field):
    value = offer()
    original_quote = "原文事实：每人每日限 100 次。"
    value[field] = proof("模型编造的无限额度", original_quote)
    extraction = {"intent": "offer", "offers": [value]}
    original = deepcopy(extraction)

    validated = trade.validate(extraction, snapshot_for(value))

    assert validated["offers"][0][field]["value"] == original_quote
    assert validated["offers"][0][field]["quote"] == original_quote
    assert extraction == original
    assert validated["offers"][0] is not value
    assert validated["offers"][0][field] is not value[field]


def test_trade_schema_rejects_nontrade_offers_and_extra_proof_fields():
    value = offer()
    snapshot = snapshot_for(value)
    with pytest.raises(trade.TradeError, match="nontrade cannot include offers"):
        trade.validate({"intent": "non_trade", "offers": [value]}, snapshot)

    malformed = deepcopy(value)
    malformed["price"]["source"] = "fixture"
    with pytest.raises(trade.TradeError, match="invalid offer evidence"):
        trade.validate({"intent": "offer", "offers": [malformed]}, snapshot)


def test_web_only_and_unconfirmed_price_specifications_do_not_push():
    web = offer(access="web", delivery="account")
    config = price_config(web)
    assert trade.evaluate(web, config, snapshot_for(web), now=NOW)["decision"] == "skip"

    codex = offer()
    result = trade.evaluate(codex, trade.default_config(), snapshot_for(codex), now=NOW)
    assert (result["decision"], result["reason"]) == ("hold", "该规格价格上限尚未确认")


def test_seat_price_is_checked_as_the_member_price_without_splitting_it():
    value = offer(price="90", delivery="seat", access="full")

    result = trade.evaluate(value, price_config(value, monthly="80", total="100"),
                            snapshot_for(value), now=NOW)

    assert result["decision"] == "skip"
    assert result["monthly"] == "90"


@pytest.mark.parametrize(("utility", "decision"), [("uncertain", "hold"), ("none", "skip")])
def test_acceptable_price_cannot_override_missing_or_absent_utility(utility, decision):
    value = offer(price="10") | {"utility": utility}

    result = trade.evaluate(value, price_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == decision


def test_codex_access_with_proven_shared_allocation_uses_shared_price_limits():
    value = offer(price="300", delivery="codex", access="codex", participants="4",
                  personal_quota="100 GB/30天", allocation="equal",
                  allocation_basis="全员同价含车主共享拼车")
    value["participants"] = proof("4", "含车主四人共享拼车")
    value["allocation_basis"] = proof("全员同价含车主", "全员同价含车主共享拼车")
    value["car_prices"] = [{"count": proof("4", "四人车档"),
                            "price": proof("300", "每席车价300元")}]
    snapshot = snapshot_for(value)

    validated = trade.validate({"intent": "offer", "offers": [value]}, snapshot)["offers"][0]
    config = trade.validate_config({
        "version": 1, "revision": "fixture-shared-vs-exclusive", "confirmed_at": "2026-01-01T00:00:00Z",
        "rules": [
            {"id": "codex-exclusive", "match": {"product": "gpt", "tier": "Pro", "delivery": "codex",
                "access": "codex", "condition": "normal", "currency": "CNY"},
             "monthly_max": "60", "total_max": "120", "minimum_days": "", "unit_max": "",
             "car_monthly_reference": "", "reference_until": ""},
            {"id": "shared", "match": {"product": "gpt", "tier": "Pro", "delivery": "shared",
                "access": "codex", "condition": "normal", "currency": "CNY"},
             "monthly_max": "318", "total_max": "300", "minimum_days": "", "unit_max": "",
             "car_monthly_reference": "1060", "reference_until": "2099-01-01T00:00:00Z",
             "shared_price_mode": "equal_share"},
        ],
    })
    result = trade.evaluate(validated, config, snapshot, now=NOW)

    assert validated["delivery"] == "shared"
    assert result["decision"] == "push"
    assert result["rule_id"] == "shared"
    assert result["monthly"] == "300"
    assert result["car_monthly"] == "1200"
    assert "每席上限 318.00" in result["comparison"]


@pytest.mark.parametrize(("kind", "expected_reason"), [
    ("wrong_price_role", "实际付款数字"),
    ("wrong_currency", "实际付款数字"),
    ("fee_without_price_role", "必要费用的金额依据"),
    ("car_price_without_price_role", "整车各档金额依据"),
])
def test_price_facts_need_a_price_role_and_currency_match(kind, expected_reason):
    value = offer(price="90")
    if kind == "wrong_price_role":
        value["price"] = proof("1", "售价1060元，服务期1个月")
    elif kind == "wrong_currency":
        value["price"] = proof("100", "额度100 USD，服务期1个月")
    elif kind == "fee_without_price_role":
        value["fees"] = [proof("1", "服务期1个月")]
    else:
        value = shared_offer()
        value["car_prices"][0]["price"] = proof("1", "两人档服务期1个月")
    config = (price_config(value, monthly="500", total="500", car_reference="1000",
                           reference_until="2099-01-01T00:00:00Z")
              if value["delivery"] == "shared" else price_config(value, monthly="500", total="500"))

    result = trade.evaluate(value, config, snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"
    assert expected_reason in result["reason"]


@pytest.mark.parametrize(("currency", "quote"), [
    ("CNY", "售价90R"),
    ("CNY", "售价90r"),
    ("CNY", "售价90元"),
    ("USD", "售价90USD"),
    ("USDT", "售价90USDT"),
    ("USDT", "售价90u"),
])
def test_price_amount_accepts_only_an_explicit_same_currency_mark(currency, quote):
    value = offer(price="90")
    value["currency"] = currency
    value["price"] = proof("90", quote)

    result = trade.evaluate(value, price_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "push"


@pytest.mark.parametrize(("currency", "quote"), [
    ("CNY", "售价90U"),
    ("CNY", "售价90u"),
    ("CNY", "售价90USD"),
    ("CNY", "售价90USDT"),
    ("USD", "售价90元"),
    ("USD", "售价90USDT"),
    ("USDT", "售价90元"),
    ("USDT", "售价90USD"),
])
def test_price_amount_cannot_be_inferred_from_another_currency_mark(currency, quote):
    value = offer(price="90")
    value["currency"] = currency
    value["price"] = proof("90", quote)

    result = trade.evaluate(value, price_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"
    assert result["reason"] == "实际付款数字与币种缺少明确对应依据"


def test_price_with_other_currency_quota_still_uses_the_selected_payment_mark():
    value = offer(price="90")
    value["price"] = proof("90", "售价90R或90元，另含100USD/u额度")

    result = trade.evaluate(value, price_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "push"


@pytest.mark.parametrize(("amount", "quote", "decision"), [
    ("6500", "售价6,500元", "skip"),
    ("6500", "售价6，500元", "skip"),
    ("90", "售价90元，期限1个月", "push"),
])
def test_numeric_evidence_preserves_thousands_and_sentence_commas(amount, quote, decision):
    value = offer(price=amount)
    value["price"] = proof(amount, quote)
    snapshot = snapshot_for(value)

    validated = trade.validate({"intent": "offer", "offers": [value]}, snapshot)["offers"][0]
    result = trade.evaluate(validated, price_config(validated, monthly="6000", total="6000"),
                            snapshot, now=NOW)

    assert validated["price"]["value"] == amount
    assert result["decision"] == decision
    assert result["monthly"] == amount


@pytest.mark.parametrize("amount", ["6", "500"])
def test_numeric_evidence_cannot_split_a_thousands_amount(amount):
    value = offer(price=amount)
    value["price"] = proof(amount, "售价6,500元")

    with pytest.raises(trade.TradeError, match="offer number absent"):
        trade.validate({"intent": "offer", "offers": [value]}, snapshot_for(value))


@pytest.mark.parametrize(("currency", "price_quote", "fee_quote"), [
    ("CNY", "售价90元", "必须手续费11USD"),
    ("CNY", "售价90元", "必须手续费11USDT"),
    ("USD", "售价90USD", "必须手续费11USDT"),
    ("USDT", "售价90USDT", "必须手续费11USD"),
])
def test_required_fee_amount_must_match_the_offer_currency(currency, price_quote, fee_quote):
    value = offer(price="90")
    value["currency"] = currency
    value["price"] = proof("90", price_quote)
    value["fees"] = [proof("11", fee_quote)]

    result = trade.evaluate(value, price_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"
    assert result["reason"] == "必要费用的金额依据不完整"


@pytest.mark.parametrize(("currency", "price_quote", "wrong_quote", "right_quote"), [
    ("CNY", "售价300元", "车价200USD", "车价300元"),
    ("CNY", "售价300元", "车价200USDT", "车价300元"),
    ("USD", "售价300USD", "车价200USDT", "车价300USD"),
    ("USDT", "售价300USDT", "车价200USD", "车价300USDT"),
])
def test_shared_car_tier_amount_must_match_the_offer_currency(
        currency, price_quote, wrong_quote, right_quote):
    value = shared_offer()
    value["currency"] = currency
    value["price"] = proof("300", price_quote)
    value["car_prices"][0]["price"] = proof("200", wrong_quote)
    value["car_prices"][1]["price"] = proof("300", right_quote)
    config = price_config(value, monthly="600", total="600", car_reference="1000",
                          reference_until="2099-01-01T00:00:00Z")

    result = trade.evaluate(value, config, snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"
    assert result["reason"] == "整车各档金额依据不完整"


def test_context_without_value_normalizes_to_missing_and_value_without_quote_is_invalid():
    value = offer()
    value["price"] = proof("", "售价90元")
    validated = trade.validate({"intent": "offer", "offers": [value]}, snapshot_for(value))["offers"][0]

    assert validated["price"] == proof()
    result = trade.evaluate(validated, price_config(validated), snapshot_for(value), now=NOW)
    assert result["decision"] == "hold"

    malformed = offer()
    malformed["price"] = proof("90")
    with pytest.raises(trade.TradeError):
        trade.validate({"intent": "offer", "offers": [malformed]}, snapshot_for(malformed))


@pytest.mark.parametrize(("price", "term", "unit", "monthly", "total", "decision"), [
    ("90", "90", "day", "100", "100", "push"),
    ("90", "91", "day", "100", "100", "skip"),
    ("101", "1", "month", "100", "200", "skip"),
    ("201", "3", "month", "100", "200", "skip"),
])
def test_term_is_limited_to_90_days_and_both_price_caps_apply(price, term, unit, monthly, total, decision):
    value = offer(price=price, term=term, term_unit=unit)
    config = price_config(value, monthly=monthly, total=total)

    result = trade.evaluate(value, config, snapshot_for(value), now=NOW)

    assert result["decision"] == decision


def test_required_fees_count_toward_both_caps_and_another_tier_cannot_inherit_the_budget():
    value = offer(price="90")
    value["fees"] = [proof("11", "必须手续费11元")]
    config = price_config(value, monthly="100", total="100")

    result = trade.evaluate(value, config, snapshot_for(value), now=NOW)

    assert result["decision"] == "skip"
    assert result["monthly"] == result["total"] == "101"
    other_tier = offer(price="1", tier="Ultra")
    assert trade.evaluate(other_tier, config, snapshot_for(other_tier), now=NOW)["decision"] == "hold"


def test_unresolvable_expiration_is_held_instead_of_skipped():
    value = offer(expires_at="今天")
    config = price_config(value)

    result = trade.evaluate(value, config, snapshot_for(value), now=NOW)

    assert (result["decision"], result["reason"]) == ("hold", "截止时间缺少可核验日期")


def test_offer_deadline_does_not_shorten_monthly_service_but_actual_usage_expiry_does():
    value = offer(price="90", expires_at="今天")
    snapshot = snapshot_for(value)
    snapshot["evidence"][0]["source_published_at"] = datetime.fromtimestamp(NOW, timezone.utc).isoformat()

    result = trade.evaluate(value, price_config(value, monthly="100"), snapshot, now=NOW)

    assert result["decision"] == "push"
    assert result["monthly"] == "90"
    assert result["expires_at"] > NOW

    expiry = (datetime.fromtimestamp(NOW, timezone.utc) + timedelta(days=15)).isoformat()
    shorter = offer(price="90", usable_until=expiry)
    result = trade.evaluate(shorter, price_config(shorter, monthly="100"), snapshot_for(shorter), now=NOW)
    assert result["decision"] == "skip"
    assert Decimal(result["monthly"]) == Decimal("180")


def test_shared_price_uses_every_car_tier_and_holds_if_personal_quota_or_headcount_is_missing():
    value = shared_offer()
    config = price_config(value, monthly="400", total="400", car_reference="1000",
                          reference_until="2099-01-01T00:00:00Z")
    result = trade.evaluate(value, config, snapshot_for(value), now=NOW)
    assert result["decision"] == "push"
    assert result["car_monthly"] == "800"

    for missing in ("participants", "personal_quota"):
        incomplete = shared_offer(**{missing: ""})
        held = trade.evaluate(incomplete, price_config(
            incomplete, monthly="400", total="400", car_reference="1000",
            reference_until="2099-01-01T00:00:00Z"), snapshot_for(incomplete), now=NOW)
        assert held["decision"] == "hold"


def test_shared_headcount_must_include_owner_and_total_car_price_has_120_percent_cap():
    missing_owner = shared_offer(car_counts=("1", "1"))
    config = price_config(missing_owner, monthly="400", total="400", car_reference="1000",
                          reference_until="2099-01-01T00:00:00Z")
    assert trade.evaluate(missing_owner, config, snapshot_for(missing_owner), now=NOW)["decision"] == "hold"

    at_cap = shared_offer(car_prices=("200", "500"))
    config = price_config(at_cap, monthly="600", total="600", car_reference="1000",
                          reference_until="2099-01-01T00:00:00Z")
    assert trade.evaluate(at_cap, config, snapshot_for(at_cap), now=NOW)["decision"] == "push"

    over_cap = shared_offer(car_prices=("200", "501"))
    config = price_config(over_cap, monthly="600", total="600", car_reference="1000",
                          reference_until="2099-01-01T00:00:00Z")
    result = trade.evaluate(over_cap, config, snapshot_for(over_cap), now=NOW)
    assert result["decision"] == "skip"
    assert result["car_monthly"] == "1202"


def equal_shared_offer(*, participants="3", price="400", allocation="equal", basis="全员含车主同价同额度") -> dict:
    return offer(price=price, delivery="shared", access="full", participants=participants,
                 personal_quota="100 GB/30天", allocation=allocation, allocation_basis=basis,
                 car_prices=[{"count": proof(participants, f"含车主共{participants}人"),
                              "price": proof(price, f"每人{price}元")}])


def equal_shared_config(value: dict) -> dict:
    return price_config(value, monthly="1000", total="1000", car_reference="1000",
                        reference_until="2099-01-01T00:00:00Z", shared_price_mode="equal_share")


@pytest.mark.parametrize(("participants", "price", "decision"), [
    ("3", "400", "push"), ("3", "400.01", "skip"),
    ("4", "300", "push"), ("4", "300.01", "skip"),
])
def test_equal_shared_seat_limit_divides_120_percent_reference_by_every_participant(participants, price, decision):
    value = equal_shared_offer(participants=participants, price=price)

    result = trade.evaluate(value, equal_shared_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == decision


@pytest.mark.parametrize(("allocation", "basis"), [
    ("unknown", ""), ("equal", ""), ("tiered", "全员按不同档位分配额度"),
])
def test_equal_shared_rule_holds_missing_allocation_evidence_and_tiered_offers(allocation, basis):
    value = equal_shared_offer(allocation=allocation, basis=basis)

    result = trade.evaluate(value, equal_shared_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"


@pytest.mark.parametrize(("participants", "participants_quote"), [
    ("4", "2-4人可选，四人拼车"),
    ("3", "至少3人参与拼车"),
    ("2", "2等1拼车，人数按车主口径"),
])
def test_ambiguous_shared_participant_count_cannot_be_reduced_to_a_single_number(participants, participants_quote):
    value = equal_shared_offer(participants=participants)
    value["participants"] = proof(participants, participants_quote)
    value["car_prices"] = [{
        "count": proof(participants, f"整车共{participants}人"),
        "price": proof("400", "每席400元"),
    }]

    result = trade.evaluate(value, equal_shared_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"


def test_different_tier_prices_cannot_be_disguised_as_equal_shared_allocation():
    value = equal_shared_offer(price="300")
    value["car_prices"] = [
        {"count": proof("1", "1人档"), "price": proof("100", "车价100元")},
        {"count": proof("2", "2人档"), "price": proof("300", "车价300元")},
    ]

    result = trade.evaluate(value, equal_shared_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"


@pytest.mark.parametrize("missing_component", ["fee", "car_count", "car_price"])
def test_incomplete_required_fee_or_car_component_holds_without_breaking_the_batch(missing_component):
    value = equal_shared_offer()
    if missing_component == "fee":
        value["fees"] = [proof()]
    elif missing_component == "car_count":
        value["car_prices"][0]["count"] = proof()
    else:
        value["car_prices"][0]["price"] = proof()

    result = trade.evaluate(value, equal_shared_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "hold"


def test_api_unit_price_applies_total_times_multiplier_over_quota_and_requires_complete_terms():
    value = api_offer()
    result = trade.evaluate(value, price_config(value, total="20", monthly="", unit="0.19"),
                            snapshot_for(value), now=NOW)
    assert result["decision"] == "skip"
    assert result["unit_price"] == "0.2"

    incomplete = api_offer(restrictions="")
    result = trade.evaluate(incomplete, price_config(incomplete, total="20", monthly="", unit="1"),
                            snapshot_for(incomplete), now=NOW)
    assert result["decision"] == "hold"


def test_api_message_discloses_multiplier_term_quota_and_limits():
    value = api_offer()
    result = trade.evaluate(value, price_config(value, total="20", monthly="", unit="1"),
                            snapshot_for(value), now=NOW)

    message = trade.format_offer(snapshot_for(value), result)

    for required in ("期限：1个月", "2", "100", "tokens", "monthly", "仅官方API"):
        assert required in message


def test_bulk_minimum_quantity_is_not_treated_as_a_single_item_offer():
    value = offer(price="100")
    value["minimum_quantity"] = proof("2", "最低起购2份")

    result = trade.evaluate(value, price_config(value), snapshot_for(value), now=NOW)

    assert result["decision"] == "skip"


def save_topic(store: Store, topic_id: str) -> None:
    with store.box.connect() as db:
        db.execute("""INSERT INTO linuxdo_topics
            (topic_id,title,status,version,first_seen,updated_at,historical) VALUES(?,?,'pending',1,0,0,0)""",
                   (topic_id, "Codex Pro 套餐说明"))


def notify(store: Store, topic_id: str, price: str, *, actor="fixture_seller") -> dict:
    value = offer(price=price)
    config = price_config(value, monthly="200", total="300")
    return trade_store.finish(store, snapshot_for(value, topic_id=topic_id, actor=actor),
                              {"intent": "offer", "offers": [value]}, config)


def test_same_seller_edits_and_reposts_only_notify_after_cumulative_ten_percent_drop(tmp_path):
    store = Store(Outbox(tmp_path / "trade.sqlite3"))
    for topic_id in ("9201", "9202", "9203"):
        save_topic(store, topic_id)

    assert notify(store, "9201", "100")["decision"] == "push"
    assert notify(store, "9201", "100")["decision"] == "skip"
    restarted = Store(Outbox(store.box.path))
    assert notify(restarted, "9202", "95")["decision"] == "skip"
    assert notify(restarted, "9203", "90")["decision"] == "push"

    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM linuxdo_trade_notifications").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_equal_price_from_different_sellers_is_not_collapsed(tmp_path):
    store = Store(Outbox(tmp_path / "sellers.sqlite3"))
    save_topic(store, "9201")
    save_topic(store, "9202")

    assert notify(store, "9201", "100", actor="fixture_first_seller")["decision"] == "push"
    assert notify(store, "9202", "100", actor="fixture_second_seller")["decision"] == "push"

    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 2


def test_legacy_notification_is_not_repeated_without_a_proven_new_benefit(tmp_path):
    store = Store(Outbox(tmp_path / "legacy-trade.sqlite3"))
    save_topic(store, "9201")
    with store.box.connect() as db:
        db.execute("INSERT INTO linuxdo_legacy_topics VALUES('9201','fixture-delivered-at')")
    value = offer(price="100")
    snapshot = snapshot_for(value, topic_id="9201")
    config = price_config(value)

    first = trade_store.finish(store, snapshot, {"intent": "offer", "offers": [value]}, config)

    assert first["decision"] == "skip"
    assert first["event_id"] is None
    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0

    benefit = deepcopy(value)
    benefit["change"] = "new_benefit"
    benefit["event"] = proof("新增席位权益", "新增席位权益")
    updated = snapshot_for(benefit, topic_id="9201")
    second = trade_store.finish(store, updated, {"intent": "offer", "offers": [benefit]}, config)

    assert second["decision"] == "push"
    assert second["event_id"] is not None


def test_seller_rewording_same_spec_and_price_does_not_create_another_notification(tmp_path):
    store = Store(Outbox(tmp_path / "rewording.sqlite3"))
    save_topic(store, "9201")
    save_topic(store, "9202")
    assert notify(store, "9201", "100")["decision"] == "push"
    edited = offer(price="100")
    edited["description"] = proof("Codex Pro 新描述", "Codex Pro 新描述")
    result = trade_store.finish(store, snapshot_for(edited, topic_id="9202", title="改写标题 Codex Pro"),
                               {"intent": "offer", "offers": [edited]}, price_config(edited))

    assert result["decision"] == "skip"
    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["equivalent_duration", "provider_and_model"])
def test_same_seller_equivalent_term_and_nonapi_description_changes_do_not_bypass_deduplication(tmp_path, change):
    store = Store(Outbox(tmp_path / "equivalent-offer.sqlite3"))
    save_topic(store, "9201")
    save_topic(store, "9202")
    assert notify(store, "9201", "100")["decision"] == "push"
    if change == "equivalent_duration":
        repost = offer(price="100", term="30", term_unit="day")
    else:
        repost = offer(price="100", provider="fixture_provider", model="fixture_model")

    result = trade_store.finish(store, snapshot_for(repost, topic_id="9202"),
                               {"intent": "offer", "offers": [repost]}, price_config(repost))

    assert result["decision"] == "skip"
    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM linuxdo_trade_notifications").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_mixed_push_and_hold_keeps_missing_evidence_eligible_for_later_review(tmp_path):
    store = Store(Outbox(tmp_path / "mixed.sqlite3"))
    save_topic(store, "9201")
    ready = offer(price="100")
    unconfirmed = offer(price="50", tier="Ultra")
    snapshot = snapshot_for(ready)
    snapshot["evidence"][0]["body"] += "；" + snapshot_for(unconfirmed)["evidence"][0]["body"]
    result = trade_store.finish(store, snapshot,
                               {"intent": "offer", "offers": [ready, unconfirmed]}, price_config(ready))

    assert result["decision"] == "push"
    assert result["event_id"] is not None
    with store.box.connect() as db:
        topic = dict(db.execute("SELECT status,next_attempt FROM linuxdo_topics WHERE topic_id='9201'").fetchone())
        assert topic["status"] == "hold"
        assert topic["next_attempt"] > 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert store.claim(now=topic["next_attempt"]) == ["9201"]


def test_wanted_topics_never_create_trade_notifications(tmp_path):
    store = Store(Outbox(tmp_path / "wanted.sqlite3"))
    save_topic(store, "9301")
    snapshot = snapshot_for(offer(), topic_id="9301")

    result = trade_store.finish(store, snapshot, {"intent": "wanted", "offers": []}, trade.default_config())

    assert result["decision"] == "skip"
    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM linuxdo_trade_notifications").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize(("intent", "decision"), [("unknown", "hold"), ("wanted", "skip")])
def test_unknown_or_wanted_intent_discards_complete_cheap_offers_and_never_enqueues(tmp_path, intent, decision):
    store = Store(Outbox(tmp_path / "uncertain-intent.sqlite3"))
    save_topic(store, "9301")
    cheap = offer(price="1")
    snapshot = snapshot_for(cheap, topic_id="9301")
    extraction = {"intent": intent, "offers": [cheap]}

    assert trade.validate(extraction, snapshot) == {"intent": intent, "offers": []}
    assert extraction["offers"] == [cheap]
    result = trade_store.finish(store, snapshot, extraction, price_config(cheap))

    assert result["decision"] == decision
    assert result["event_id"] is None
    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM linuxdo_trade_notifications").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM linuxdo_trade_quotes").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_policy_refresh_requeues_only_recent_skip_or_hold_topics_and_warns_once_for_expired_reference(tmp_path, monkeypatch):
    store = Store(Outbox(tmp_path / "refresh.sqlite3"))
    monkeypatch.setattr(trade_store.time, "time", lambda: NOW)
    trade_store.refresh_policy(store, trade.default_config(), {"categories": ["B1"]}, "fixture-model")
    for topic_id in ("9401", "9402", "9403", "9404"):
        save_topic(store, topic_id)
    with store.box.connect() as db:
        db.execute("UPDATE linuxdo_topics SET status='hold',updated_at=? WHERE topic_id='9401'", (NOW - 3600,))
        db.execute("UPDATE linuxdo_topics SET status='skip',updated_at=? WHERE topic_id='9402'", (NOW - 8 * 86400,))
        db.execute("UPDATE linuxdo_topics SET status='queued',updated_at=? WHERE topic_id='9403'", (NOW - 3600,))
        db.execute("UPDATE linuxdo_topics SET status='hold',updated_at=? WHERE topic_id='9404'", (NOW - 3600,))
        for topic_id in ("9401", "9402", "9403"):
            db.execute("INSERT INTO linuxdo_trade_quotes VALUES(?,?,?,?,?)",
                       (topic_id, f"fixture-{topic_id}", "old", "{}", NOW - 60))
    config = trade.validate_config({
        "version": 1, "revision": "fixture-expired-reference", "confirmed_at": "2026-01-01T00:00:00Z",
        "rules": [{"id": "fixture-shared", "match": {"product": "gpt", "tier": "Pro",
            "delivery": "shared", "access": "full", "condition": "normal", "currency": "CNY"},
            "monthly_max": "100", "total_max": "200", "minimum_days": "", "unit_max": "",
            "car_monthly_reference": "1000", "reference_until": "2020-01-01T00:00:00Z"}],
    })

    trade_store.refresh_policy(store, config, {"categories": ["B1"]}, "fixture-model")
    trade_store.refresh_policy(store, config, {"categories": ["B1"]}, "fixture-model")

    with store.box.connect() as db:
        topics = {row["topic_id"]: row["status"] for row in db.execute("SELECT topic_id,status FROM linuxdo_topics")}
        assert topics == {"9401": "pending", "9402": "skip", "9403": "queued", "9404": "hold"}
        warnings = db.execute("SELECT count(*) FROM events").fetchone()[0]
        assert warnings == 1


def test_codex_topics_are_claimed_before_other_recent_topics(tmp_path):
    store = Store(Outbox(tmp_path / "priority.sqlite3"))
    save_topic(store, "9201")
    save_topic(store, "9202")
    with store.box.connect() as db:
        db.execute("UPDATE linuxdo_topics SET title='普通套餐报价',first_seen=? WHERE topic_id='9201'", (NOW - 300,))
        db.execute("UPDATE linuxdo_topics SET title='CODEX套餐报价',first_seen=? WHERE topic_id='9202'", (NOW - 30,))

    assert store.claim(now=NOW, limit=2) == ["9202", "9201"]


def test_old_other_topics_are_not_starved_by_new_codex_posts(tmp_path):
    store = Store(Outbox(tmp_path / "fairness.sqlite3"))
    save_topic(store, "9201")
    save_topic(store, "9202")
    with store.box.connect() as db:
        db.execute("UPDATE linuxdo_topics SET title='普通套餐报价',first_seen=? WHERE topic_id='9201'", (NOW - 3601,))
        db.execute("UPDATE linuxdo_topics SET title='CODEX套餐报价',first_seen=? WHERE topic_id='9202'", (NOW - 30,))

    assert store.claim(now=NOW, limit=1) == ["9201"]


def test_sold_edit_suppresses_unsent_trade_and_withdraws_old_public_evidence_before_new_review(tmp_path, monkeypatch):
    monkeypatch.setattr(public_review.time, "time", lambda: datetime.fromisoformat("2026-01-02T05:00:00+00:00").timestamp())
    store = Store(Outbox(tmp_path / "sold-edit.sqlite3"))
    value = offer()
    for item in [*trade.ATTRIBUTES, "tier"]:
        if value[item]["value"]:
            value[item]["ref"] = f"tg:{SOURCE}:101"
    initial = snapshot_for(value)
    raw = {
        "message_id": 101, "incoming": True,
        "text": "【following_posted】 @fixture_seller Codex Pro\n" + initial["evidence"][0]["body"],
        "published_at": "2026-01-02T03:04:05Z", "edited_at": "",
        "links": [{"label": "Codex Pro", "url": "https://linux.do/t/topic/9201", "header": True}],
        "buttons": [{"label": "查看原帖", "url": "https://linux.do/t/topic/9201"}],
    }
    checkpoint = {"identity": "fixture-owner", "baseline": True, "baseline_id": 100, "cursor": 101}
    helper = HelperStore(store)
    assert helper.save_page([raw], checkpoint)["following"] == 1
    assert public_review.review(store, request=lambda snapshot: {"intent": "offer", "offers": [value]},
                                config=price_config(value), model="fixture-model")["processed"] == 1
    queued = trade_store.finish(store, store.snapshot("9201"), {"intent": "offer", "offers": [value]}, price_config(value))
    assert queued["decision"] == "push"
    assert store.snapshot("9201")["evidence"]

    sold = raw | {"text": "【following_posted】 @fixture_seller Codex Pro\n已售出，停止出售。",
                  "edited_at": "2026-01-02T04:00:00Z"}
    assert helper.save_page([sold], checkpoint)["queued"] == 0

    assert store.snapshot("9201")["evidence"] == []
    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM linuxdo_observations").fetchone()[0] == 0
        assert db.execute("SELECT status FROM events WHERE id=?", (queued["event_id"],)).fetchone()[0] == "suppressed"
        assert db.execute("SELECT count(*) FROM linuxdo_helper_events").fetchone()[0] == 0

    inspect_edited = Mock(side_effect=AssertionError("public review only captures Helper evidence"))
    reviewed = public_review.review(store, request=inspect_edited, config=price_config(value), model="fixture-model")
    assert reviewed == {"processed": 1, "errors": 0, "queued": 0}
    inspect_edited.assert_not_called()
    sold_snapshot = store.snapshot("9201")
    assert len(sold_snapshot["evidence"]) == 1
    assert "售价90元" not in sold_snapshot["evidence"][0]["body"]
    assert "已售出" in sold_snapshot["evidence"][0]["body"]

    corrected = raw | {"text": "【following_posted】 @fixture_seller Codex Pro\n" +
                       initial["evidence"][0]["body"] + "；已恢复出售。",
                       "edited_at": "2026-01-02T04:05:00Z"}
    assert helper.save_page([corrected], checkpoint)["queued"] == 0
    assert public_review.review(store, request=lambda snapshot: {"intent": "offer", "offers": [value]},
                                config=price_config(value), model="fixture-model")["processed"] == 1
    restored_snapshot = store.snapshot("9201")
    restored = trade_store.finish(store, restored_snapshot, {"intent": "offer", "offers": [value]}, price_config(value))
    replay = trade_store.finish(store, restored_snapshot, {"intent": "offer", "offers": [value]}, price_config(value))
    assert restored["decision"] == "push"
    assert replay["decision"] == "skip"
    with store.box.connect() as db:
        assert db.execute("SELECT status FROM events WHERE id=?", (queued["event_id"],)).fetchone()[0] == "suppressed"
        assert db.execute("SELECT count(*) FROM linuxdo_trade_notifications").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM events WHERE status='pending'").fetchone()[0] == 1


@pytest.mark.parametrize(
    ('delivery_status', 'expected_personal_queue'),
    [('pending', 1), ('sending', 0), ('uncertain', 0), ('delivered', 0)],
)
def test_marketplace_helper_edit_withdraws_pending_event_and_dedupes_personal_notice(
        tmp_path, monkeypatch, delivery_status, expected_personal_queue):
    """Helper revisions own the lifecycle of Linux.do marketplace evidence."""
    monkeypatch.setattr(
        marketplace_pipeline.policy,
        'load',
        lambda: {
            'version': 1, 'enabled': True, 'categories': ['phone.smartphone'],
            'groups': [], 'blocked_groups': [], 'blocked_categories': [],
            'blocked_types': [], 'types': [], 'partial_types': [], 'filters': {},
            'allow_beijing_pickup': True,
        },
    )
    monkeypatch.setattr(
        marketplace_pipeline.wanted,
        'load',
        lambda: {'version': 1, 'enabled': False, 'targets': [], 'blocked_groups': [], 'filters': {}},
    )
    monkeypatch.setattr('pylon.tasks.quotes.health.inspect', lambda *_: {'available': False})

    store = Store(Outbox(tmp_path / f'market-helper-{delivery_status}.sqlite3'))
    helper = HelperStore(store)
    body = 'iPhone 15 Pro 256G，售价2999元，可邮寄。'
    raw = {
        'message_id': 101, 'incoming': True,
        'text': '【following_posted】 @fixture_seller 出 iPhone 15 Pro\n' + body,
        'published_at': '2026-01-02T03:04:05Z', 'edited_at': '',
        'links': [{'label': '出 iPhone 15 Pro', 'url': 'https://linux.do/t/topic/9201/1', 'header': True}],
        'buttons': [{'label': '查看原帖', 'url': 'https://linux.do/t/topic/9201/1'}],
    }
    checkpoint = {'identity': 'fixture-owner', 'baseline': True, 'baseline_id': 100, 'cursor': 101}
    assert helper.save_page([raw], checkpoint)['following'] == 1
    assert public_review.review(store) == {'processed': 1, 'errors': 0, 'queued': 0}
    snapshot = store.snapshot('9201')
    market = MarketplaceStore(store.box)
    assert market.ingest('linuxdo', [{
        'id': '9201', 'url': 'https://linux.do/t/topic/9201', 'title': '出 iPhone 15 Pro',
        'body_full': body, 'body_complete': True, 'posted_at': raw['published_at'],
        'source_context': {'snapshot': snapshot, 'evidence_scope': 'helper_excerpt'},
        'body_provenance': {'source': 'helper-fixture'},
    }]) == 1
    assert marketplace_pipeline.process(
        market,
        current_policy=marketplace_pipeline.policy.load(),
        wanted_policy=marketplace_pipeline.wanted.load(),
        source='linuxdo',
    ) == 1
    with store.box.connect() as db:
        old_row = db.execute(
            'SELECT event_id FROM marketplace_posts WHERE source=? AND source_id=?', ('linuxdo', '9201')
        ).fetchone()
        old_event = old_row['event_id']
        db.execute('UPDATE events SET status=? WHERE id=?', (delivery_status, old_event))

    sold = raw | {
        'text': '【following_posted】 @fixture_seller 出 iPhone 15 Pro\n已售出，停止出售。',
        'edited_at': '2026-01-02T04:00:00Z',
    }
    assert helper.save_page([sold], checkpoint)['queued'] == 0
    with store.box.connect() as db:
        sold_row = db.execute(
            'SELECT status,event_id FROM marketplace_posts WHERE source=? AND source_id=?', ('linuxdo', '9201')
        ).fetchone()
        assert db.execute('SELECT status FROM events WHERE id=?', (old_event,)).fetchone()[0] == (
            'suppressed' if delivery_status == 'pending' else delivery_status
        )
    if delivery_status != 'pending':
        assert sold_row['event_id'] == old_event
        assert sold_row['status'] == 'awaiting_helper'
        personal = sold | {
            'message_id': 102,
            'text': '【replied】 @fixture_seller 出 iPhone 15 Pro\n私人提醒',
            'edited_at': '',
        }
        with store.box.connect() as db:
            # The sold edit can clear live Helper evidence before the private
            # reminder arrives; dedupe must use the persisted market snapshot.
            db.execute("DELETE FROM linuxdo_evidence WHERE topic_id='9201'")
        assert helper.save_page([personal], dict(checkpoint, cursor=102))['queued'] == 0
        with store.box.connect() as db:
            final_row = db.execute(
                'SELECT status,event_id FROM marketplace_posts WHERE source=? AND source_id=?',
                ('linuxdo', '9201'),
            ).fetchone()
            assert final_row['event_id'] == old_event and final_row['status'] == 'awaiting_helper'
            assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1
        return
    assert sold_row['event_id'] is None and sold_row['status'] == 'awaiting_helper'

    corrected = raw | {
        'text': '【following_posted】 @fixture_seller 出 iPhone 15 Pro\n' + body + '；已恢复出售。',
        'edited_at': '2026-01-02T04:05:00Z',
    }
    assert helper.save_page([corrected], checkpoint)['queued'] == 0
    assert public_review.review(store) == {'processed': 1, 'errors': 0, 'queued': 0}
    transfer = linuxdo_bridge.transfer(store, store.snapshot('9201'), {'intent': 'offer', 'offers': []})
    assert transfer['decision'] == 'marketplace'
    with store.box.connect() as db:
        new_row = db.execute(
            'SELECT status,event_id FROM marketplace_posts WHERE source=? AND source_id=?', ('linuxdo', '9201')
        ).fetchone()
        new_event = new_row['event_id']
        assert new_row['status'] == 'queued' and new_event and new_event != old_event
        assert db.execute('SELECT status FROM events WHERE id=?', (new_event,)).fetchone()[0] == 'pending'

    personal = corrected | {
        'message_id': 102,
        'text': '【replied】 @fixture_seller 出 iPhone 15 Pro\n私人提醒',
        'edited_at': '',
    }
    personal_result = helper.save_page([personal], dict(checkpoint, cursor=102))
    assert personal_result['queued'] == expected_personal_queue
    with store.box.connect() as db:
        final_row = db.execute(
            'SELECT status,event_id FROM marketplace_posts WHERE source=? AND source_id=?', ('linuxdo', '9201')
        ).fetchone()
        assert db.execute('SELECT status FROM events WHERE id=?', (old_event,)).fetchone()[0] == 'suppressed'
        if expected_personal_queue:
            assert db.execute('SELECT status FROM events WHERE id=?', (new_event,)).fetchone()[0] == 'suppressed'
            assert final_row['event_id'] is None and final_row['status'] == 'skipped'
            assert db.execute('SELECT count(*) FROM events WHERE status="pending"').fetchone()[0] == 1
        else:
            assert final_row['event_id'] == new_event


def test_trade_config_cli_atomically_installs_confirmed_private_configuration(tmp_path, monkeypatch, capsys):
    target = tmp_path / "config" / "linuxdo_prices.json"
    target.parent.mkdir(mode=0o700)
    previous = trade.default_config()
    target.write_text(json.dumps(previous))
    target.chmod(0o600)
    incoming = tmp_path / "incoming-prices.json"
    confirmed = price_config(offer())
    incoming.write_text(json.dumps(confirmed))
    incoming.chmod(0o600)
    monkeypatch.setattr(trade, "CONFIG_FILE", target)
    original_replace = helper_account.os.replace
    replacements = []

    def atomic_replace(source, destination):
        source, destination = Path(source), Path(destination)
        assert destination == target
        assert json.loads(target.read_text()) == previous
        assert source.parent == target.parent
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert json.loads(source.read_text()) == confirmed
        replacements.append((source, destination))
        original_replace(source, destination)

    monkeypatch.setattr(helper_account.os, "replace", atomic_replace)

    assert linuxdo_main.main(["trade-config", "--apply", "--input", str(incoming)]) == 0

    assert json.loads(capsys.readouterr().out) == {"revision": confirmed["revision"], "rules": 1}
    assert len(replacements) == 1
    assert trade.load(target) == confirmed
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(incoming.read_text()) == confirmed
    assert not list(target.parent.glob(".helper-*"))


@pytest.mark.parametrize("failure", ["unconfirmed", "replace"])
def test_failed_trade_configuration_install_preserves_prior_file(tmp_path, monkeypatch, failure):
    target = tmp_path / "linuxdo_prices.json"
    target.write_text(json.dumps(price_config(offer())))
    target.chmod(0o600)
    previous = target.read_bytes()
    incoming = tmp_path / "incoming-prices.json"
    config = price_config(offer(price="50"))
    config["revision"] = "fixture-confirmed-2"
    if failure == "unconfirmed":
        config["confirmed_at"] = ""
    incoming.write_text(json.dumps(config))
    incoming.chmod(0o600)
    monkeypatch.setattr(trade, "CONFIG_FILE", target)
    if failure == "replace":
        monkeypatch.setattr(helper_account.os, "replace", Mock(side_effect=OSError("fixture replace failure")))

    with pytest.raises(trade.TradeError if failure == "unconfirmed" else OSError):
        trade.install(incoming)

    assert target.read_bytes() == previous
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert not list(target.parent.glob(".helper-*"))
