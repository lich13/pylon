"""Synthetic regressions for marketplace facts, field choices, and reply ownership."""

import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylon import commands, config, inline
from pylon.tasks.gmail import review as gmail_review
from pylon.tasks.marketplace import classifier, control, enrich, interface, subjects, taxonomy
from pylon.tasks.marketplace.store import Store


PHONE = "phone.smartphone"
OWNER = 123
COMMON_FIELDS = {"brand", "series", "model", "price", "product", "keywords"}


@pytest.fixture
def market(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_HOME", str(tmp_path))
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "data/outbox.sqlite3"))
    monkeypatch.setenv("PYLON_COMMAND_PROFILE", "fixture")
    monkeypatch.delenv("PYLON_REPLY_MESSAGE_ID", raising=False)

    def environment(profile):
        return {
            "TELEGRAM_HOME_CHANNEL": str(OWNER),
            "TELEGRAM_BOT_TOKEN": "fixture-" + profile,
        }

    monkeypatch.setattr(config, "profile_env", environment)
    monkeypatch.setattr(inline, "profile_env", environment)
    settings = tmp_path / "config"
    settings.mkdir()
    (settings / "marketplace.json").write_text(json.dumps({
        "version": 3, "enabled": True, "categories": [], "groups": [],
        "types": [], "blocked_groups": [], "blocked_categories": [], "filters": {},
    }))
    (settings / "marketplace-wanted.json").write_text(json.dumps({
        "version": 2, "enabled": False, "targets": [], "blocked_groups": [],
    }))
    prior_extensions = copy.deepcopy(taxonomy.EXTENSIONS)
    taxonomy.refresh_extensions({})
    store = Store()
    with store.box.connect() as db:
        control.baseline(db)
    yield store
    taxonomy.refresh_extensions(prior_extensions)


def classified(title, body):
    items = classifier.classify_listing(title, body, "v2ex", True)
    assert len(items) == 1
    return items[0]


def delivered_button(store, card, label):
    matches = [button for row in card["reply_markup"]["inline_keyboard"]
               for button in row if button["text"] == label]
    assert len(matches) == 1
    with store.box.connect() as db:
        message_id = 100 + db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    event_id = store.box.enqueue(
        profile="webops", job_id="test-attribute-guards", kind="text", payload=card,
        key=f"attribute-card:{message_id}",
    )
    store.box.finish(event_id, "delivered", receipt=[{
        "message_id": message_id, "chat_id": OWNER,
    }])
    return {
        "data": matches[0]["callback_data"], "from": {"id": OWNER},
        "message": {"message_id": message_id,
                    "chat": {"id": OWNER, "type": "private"}},
    }


def begin_price_input(store):
    with store.box.connect() as db:
        draft = interface.draft(db)
        prompt = interface.route(
            db,
            "input",
            {"draft": draft["id"], "category": PHONE, "field": "price"},
            draft["base"],
        )
    reply_to = delivered_button(store, prompt, "取消输入")
    return draft["id"], reply_to["message"]["message_id"]


def saved_draft(store, draft_id):
    with store.box.connect() as db:
        return interface.draft(db, draft_id)


def input_count(store):
    with store.box.connect() as db:
        return db.execute("SELECT COUNT(*) FROM market_inputs").fetchone()[0]


@pytest.mark.parametrize("condition", [
    "全新未拆封，无划痕无磕碰",
    "无划痕、无磕碰，全新未拆封",
    "全新未拆封，没有划痕没有磕碰",
])
def test_negated_wear_does_not_turn_a_sealed_phone_into_used(condition):
    phone = classified("出售 iPhone 15 Pro 256G", condition + "，售价4500元。")

    assert phone["category"] == PHONE
    assert phone["attributes"]["facts"]["condition"] == "未拆封"
    assert phone["attributes"]["condition"] == "未拆封"
    evidence = phone["attributes"]["attribute_evidence"]["condition"]
    assert evidence and all(fragment in condition for fragment in evidence)


@pytest.mark.parametrize("field,negative,positive", [
    ("fault", "无故障", "屏幕花屏"),
    ("fault", "功能正常", "不能开机"),
    ("repair", "无拆无修", "换过屏"),
    ("repair", "未维修", "维修过"),
])
@pytest.mark.parametrize("positive_first", [False, True])
def test_conflicting_fault_or_repair_claims_never_certify_absence(
    field, negative, positive, positive_first,
):
    claims = [negative, positive]
    if positive_first:
        claims.reverse()
    values, _ = subjects.facts("出售手机，" + "，".join(claims), {}, category=PHONE)

    assert values.get(field) != "无"


@pytest.mark.parametrize("field,claim", [
    ("fault", "无故障"),
    ("fault", "功能正常"),
    ("repair", "无拆无修"),
    ("repair", "未维修"),
])
def test_uncontradicted_absence_claims_remain_available(field, claim):
    values, evidence = subjects.facts("出售手机，" + claim, {}, category=PHONE)

    assert values[field] == "无"
    assert evidence[field] == [claim]


@pytest.mark.parametrize("field,claim", [("fault", "花屏"), ("repair", "换过屏")])
def test_explicit_fault_or_repair_is_still_recorded(field, claim):
    values, evidence = subjects.facts("出售手机，" + claim, {}, category=PHONE)

    assert values[field] == "有"
    assert evidence[field] == [claim]


@pytest.mark.parametrize("purchase", ["9月购买", "今年9月购入", "2026年9月购买"])
def test_phone_purchase_month_is_not_an_entitlement_duration(purchase):
    phone = classified("出售 iPhone 15 Pro 256G", purchase + "，售价4500元。")

    assert phone["category"] == PHONE
    assert "duration" not in phone["attributes"]["facts"]
    assert "duration" not in phone["attributes"]["attribute_evidence"]


def test_service_entitlement_keeps_its_two_hour_duration():
    title = "出售京东家政2小时保洁权益"
    item = classified(title, "仅限北京，可转让，售价80元。")

    assert item["category"] == "virtual.service"
    assert item["attributes"]["facts"]["duration"] == ["2小时"]
    assert item["attributes"]["attribute_evidence"]["duration"] == ["2小时"]


@pytest.mark.parametrize("category,applicable", [
    (PHONE, {"condition", "ram_gb", "storage_gb", "size", "network", "fault", "repair"}),
    ("life.clothing", {"condition", "size"}),
    ("virtual.service", {"platform", "plan", "duration", "region", "transfer", "origin"}),
])
@pytest.mark.parametrize("scope", ["sale", "wanted"])
def test_attribute_editor_exposes_only_relevant_fields_and_keeps_shared_fields(
    market, category, applicable, scope,
):
    with market.box.connect() as db:
        draft = interface.draft(db)
        card = interface.fields_card(db, draft, category, scope)
        fields = []
        for row in card["reply_markup"]["inline_keyboard"]:
            for button in row:
                token = button["callback_data"].split("|")[1]
                action = db.execute(
                    "SELECT action,payload FROM market_buttons WHERE id=?", (token,),
                ).fetchone()
                if action["action"] != "field":
                    continue
                payload = json.loads(action["payload"])
                fields.append(payload["field"])
                assert payload["category"] == category
                assert payload["scope"] == scope

    assert len(fields) == len(set(fields))
    assert set(fields) == COMMON_FIELDS | applicable


@pytest.mark.parametrize("next_command", ["/gmail", "/help", "/start"])
def test_non_market_command_clears_market_input_before_plain_gmail_reply(
    market, monkeypatch, next_command,
):
    draft_id, _ = begin_price_input(market)
    before = saved_draft(market, draft_id)
    assert input_count(market) == 1
    monkeypatch.setattr(commands, "gmail_text", Mock(return_value="Gmail 向导"))
    bind_reason = Mock(return_value="原因已记录")
    monkeypatch.setattr(gmail_review, "bind_reason", bind_reason)

    commands.handle("webops", next_command)

    assert input_count(market) == 0
    reason = "构建成功通知没有价值"
    assert commands.handle("webops", reason) == "原因已记录"
    bind_reason.assert_called_once_with("webops", 0, reason)
    assert saved_draft(market, draft_id) == before


@pytest.mark.parametrize("explicit_reply", [False, True])
def test_market_input_still_accepts_a_reply_just_before_one_hour(
    market, monkeypatch, explicit_reply,
):
    now = [1_700_000_000.0]
    monkeypatch.setattr(interface, "time", SimpleNamespace(time=lambda: now[0]))
    draft_id, message_id = begin_price_input(market)
    now[0] += 3599

    before = saved_draft(market, draft_id)
    result = interface.reply(
        "webops", message_id if explicit_reply else 0, "最低 10；最高 300", store=market,
    )

    assert result is None
    assert saved_draft(market, draft_id) == before
    assert input_count(market) == 1


@pytest.mark.parametrize("explicit_reply", [False, True])
def test_market_input_expires_from_original_button_even_after_a_draft_update(
    market, monkeypatch, explicit_reply,
):
    now = [1_700_000_000.0]
    monkeypatch.setattr(interface, "time", SimpleNamespace(time=lambda: now[0]))
    draft_id, message_id = begin_price_input(market)
    now[0] += 3500
    with market.box.connect() as db:
        draft = interface.draft(db, draft_id)
        interface.save(db, draft)
    before = saved_draft(market, draft_id)
    assert before["updated_at"] == now[0]
    now[0] += 101

    result = interface.reply(
        "webops", message_id if explicit_reply else 0, "最低 10；最高 300", store=market,
    )

    assert result is None
    assert input_count(market) == 1
    assert saved_draft(market, draft_id) == before


def grok_phone(body, *, name="iPhone 15 Pro", attributes=None, price=4500):
    post = {
        "source": "v2ex", "source_id": "42", "title": "出售 " + name,
        "body": body, "body_complete": True, "history": 0,
        "url": "https://example.invalid/t/42",
    }
    answer = {
        "items": [{
            "name": name, "category": PHONE, "start_line": 0, "end_line": 1,
            "price": price, "price_line": 1, "status": "available", "link_ids": [],
            "category_evidence": [name], "category_reason": "原文明示出售手机",
            "attributes": attributes or [], "novelty": None,
        }],
        "common_lines": [], "shared_link_ids": [], "ignored_lines": [],
    }
    return post, answer


def test_grok_request_uses_english_attribute_field_enum_and_separate_price(market, monkeypatch):
    post, answer = grok_phone("iPhone 15 Pro 256G，售价4500元，可邮寄。")
    calls = []

    def request(prompt, *, schema, name, **_kwargs):
        calls.append((prompt, schema, name))
        return ({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(answer, ensure_ascii=False)},
        ]}]}, "fixture-field-schema")

    monkeypatch.setattr("pylon.tasks.software.monitor._request_grok_structured", request)

    items, request_id = enrich.request(post)

    assert len(calls) == 1 and calls[0][2] == "marketplace_items"
    fields = calls[0][1]["properties"]["items"]["items"]["properties"]
    field_schema = fields["attributes"]["items"]["properties"]["field"]
    assert field_schema["type"] == "string"
    assert set(field_schema["enum"]) == set(subjects.FIELDS) - {"price"}
    assert all(key.isascii() and key.replace("_", "").isalpha() for key in field_schema["enum"])
    assert fields["price"]["type"] == ["number", "null"]
    prompt_data = json.loads(calls[0][0].split("\n", 1)[1])
    assert set(prompt_data["attribute_fields"]) == set(field_schema["enum"])
    assert "价格只放 price，不能放 attributes" in calls[0][0]
    assert items[0]["price"] == 4500 and request_id == "fixture-field-schema"


@pytest.mark.parametrize("field", ["内存", "memory_gb", "price"])
def test_grok_rejects_localized_invented_and_price_attribute_keys(field):
    body = "iPhone 15 Pro 256G，售价4500元，可邮寄。"
    post, answer = grok_phone(body, attributes=[{
        "field": field, "value": "4500", "evidence": body,
    }])

    with pytest.raises(enrich.AnalysisError, match="invalid_attribute_field:use_attribute_fields_keys_only"):
        enrich.validate(answer, post)


@pytest.mark.parametrize("quote", [
    "12+512G，因为换手机所以出售",
    "因为换手机所以出售，12+512G",
])
def test_grok_accepts_owned_ram_value_in_a_quote_that_also_contains_background(quote):
    post, answer = grok_phone(
        quote + "，售价1500元，可邮寄。", name="Redmi K70 手机", price=1500,
        attributes=[{"field": "ram_gb", "value": "12", "evidence": quote}],
    )

    items = enrich.validate(answer, post)

    assert len(items) == 1
    assert items[0]["attributes"]["facts"]["ram_gb"] == 12
    assert items[0]["attributes"]["facts"]["storage_gb"] == 512


@pytest.mark.parametrize("whole_quote", [False, True])
def test_grok_cannot_borrow_the_replacement_devices_model_from_a_mixed_quote(whole_quote):
    background = "因为换成 Galaxy S24 所以出售"
    body = "iPhone 15 Pro 256G，" + background + "，售价4500元，可邮寄。"
    post, answer = grok_phone(body, attributes=[{
        "field": "model", "value": "Galaxy S24", "evidence": body if whole_quote else background,
    }])

    with pytest.raises(enrich.AnalysisError, match="attribute_from_background:model"):
        enrich.validate(answer, post)


@pytest.mark.parametrize("prefix", ["继上一个帖子 ", "继上一个帖子，"])
def test_reference_to_a_previous_post_does_not_remove_the_current_sale_subject(prefix):
    phone = classified(prefix + "出 iPhone 15 Pro 512G", "自用，售价4498元，可邮寄。")

    assert phone["category"] == PHONE
    assert phone["status"] == "available" and phone["price"] == 4498
    assert "iPhone 15 Pro" in phone["attributes"]["subject"]


@pytest.mark.parametrize("closure", ["已售", "已出"])
def test_repeated_phone_in_title_and_body_does_not_leave_an_available_duplicate(closure):
    phone = classified(
        "出 iPhone 15 Pro 512G",
        "出 iPhone 15 Pro 512G，售价4498元，可邮寄，" + closure + "。",
    )

    assert phone["category"] == PHONE
    assert phone["status"] == "sold"


def test_grok_cannot_certify_no_repair_when_the_source_also_reports_a_repair():
    body = "iPhone 15 Pro 256G，无拆无修，但维修过，售价4500元，可邮寄。"
    phone = classified("出售 iPhone 15 Pro", body)
    assert "repair" in phone["attributes"]["attribute_conflicts"]
    assert "repair" not in phone["attributes"]["facts"]
    post, answer = grok_phone(body, attributes=[{
        "field": "repair", "value": "无", "evidence": "无拆无修",
    }])

    with pytest.raises(enrich.AnalysisError, match="attribute_conflict:repair"):
        enrich.validate(answer, post)
