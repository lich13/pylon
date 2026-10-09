"""Synthetic regressions for the product being sold and its owned evidence."""

import pytest

from pylon.tasks.marketplace import classifier, enrich, policy


PHONE_POLICY = {"enabled": True, "groups": ["phone"], "categories": []}
ACCESSORY_POLICY = {"enabled": True, "categories": ["accessory.phone_case", "accessory.film"]}


def classify(title, body, *, source="v2ex", platform_category=""):
    return classifier.classify_listing(
        title, body, source, True, platform_category=platform_category,
    )


def post(title, body):
    return {
        "source": "v2ex", "source_id": "subject-fixture", "title": title,
        "body": body, "body_complete": True, "history": 0,
        "url": "https://example.invalid/subject-fixture",
    }


def assert_subject(item, expected, title, body):
    subject = item["attributes"]["subject"]
    assert isinstance(subject, str) and subject
    assert expected in subject
    assert subject in title or subject in body


def assert_attribute_evidence(item, field, expected, title, body):
    evidence = item["attributes"]["attribute_evidence"][field]
    assert isinstance(evidence, list) and evidence
    assert any(expected in fragment for fragment in evidence)
    assert all(fragment in title or fragment in body for fragment in evidence)


@pytest.mark.parametrize("model", ["澜星 Q8 Ultra", "Aster P6 Max"])
def test_unfamiliar_model_without_phone_evidence_stays_unknown_and_needs_analysis(model):
    title = f"出 {model} 12+512G"
    body = "到手就贴镜头膜，使用情况见图，售价4498元，可邮寄。"
    items = classify(title, body)

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "unknown"
    assert item["price"] == 4498
    assert title in item["text"]
    assert_subject(item, model, title, body)
    assert policy.evaluate(items, PHONE_POLICY) == []
    assert policy.evaluate(items, ACCESSORY_POLICY) == []
    assert enrich.needs_analysis(post(title, body), items)


@pytest.mark.parametrize("source", ["v2ex", "chiphell", "nga"])
@pytest.mark.parametrize("model", ["澜星 Q8 Ultra", "Aster P6 Max"])
def test_phone_platform_and_owned_device_facts_support_unfamiliar_model(source, model):
    title = f"出 {model} 12+512G"
    body = "自用整机，镜头膜一直贴着，机身无磕碰，售价4498元，可邮寄。"
    items = classify(title, body, source=source, platform_category="手机")

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "phone.smartphone"
    assert item["status"] == "available"
    assert item["price"] == 4498
    assert title in item["text"]
    assert "12+512G" in item["attributes"]["specs"]
    assert item["attributes"]["facts"]["ram_gb"] == 12
    assert item["attributes"]["facts"]["storage_gb"] == 512
    assert_attribute_evidence(item, "ram_gb", "12+512G", title, body)
    assert_attribute_evidence(item, "storage_gb", "12+512G", title, body)
    assert_subject(item, model, title, body)
    assert policy.evaluate(items, PHONE_POLICY) == items
    assert policy.evaluate(items, ACCESSORY_POLICY) == []


def test_explicit_phone_noun_supports_unfamiliar_model_without_platform_hint():
    title = "出 澜星 Q8 Ultra 手机 12+512G"
    body = "自用整机，全程带壳，已贴镜头膜，售价4498元，可邮寄。"
    items = classify(title, body)

    assert len(items) == 1
    assert items[0]["category"] == "phone.smartphone"
    assert items[0]["price"] == 4498
    assert_subject(items[0], "澜星 Q8 Ultra", title, body)
    assert policy.evaluate(items, PHONE_POLICY) == items


@pytest.mark.parametrize("body", [
    "镜头膜一直贴着，带壳使用，售价4498元，可邮寄。",
    "全程带壳，已贴镜头膜，售价4498元，可邮寄。",
    "因为换成 Galaxy S24，所以出售原机。镜头膜一直贴着，售价4498元，可邮寄。",
    "换用 iPhone 16 Pro 后闲置，原机带壳使用，镜头膜已贴，售价4498元，可邮寄。",
])
def test_known_phone_survives_protection_and_replacement_background(body):
    title = "出 iPhone 15 Pro 512G"
    items = classify(title, body)

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "phone.smartphone"
    assert item["status"] == "available"
    assert item["price"] == 4498
    assert title in item["text"]
    assert_subject(item, "iPhone 15 Pro", title, body)
    assert "Galaxy S24" not in item["attributes"]["subject"]
    assert "iPhone 16 Pro" not in item["attributes"]["subject"]
    assert policy.evaluate(items, PHONE_POLICY) == items
    assert policy.evaluate(items, ACCESSORY_POLICY) == []


@pytest.mark.parametrize("title,body", [
    ("出 iPhone 15 Pro 手机壳", "全新手机壳，适配 iPhone 15 Pro，售价49元，不含手机，可邮寄。"),
    ("出适配 iPhone 15 Pro 的保护壳", "保护壳未拆封，售价49元，可邮寄。"),
    ("出 澜星 Q8 Ultra 手机壳", "防摔壳，售价49元，不含手机，可邮寄。"),
])
@pytest.mark.parametrize("platform_category", ["", "手机"])
def test_phone_case_cannot_borrow_a_model_or_platform_phone_category(title, body, platform_category):
    items = classify(title, body, platform_category=platform_category)

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "accessory.phone_case"
    assert item["price"] == 49
    assert_subject(item, "壳", title, body)
    assert policy.evaluate(items, PHONE_POLICY) == []
    assert policy.evaluate(items, ACCESSORY_POLICY) == items


@pytest.mark.parametrize("model", ["iPhone 15 Pro", "澜星 Q8 Ultra"])
def test_lens_protector_itself_remains_an_accessory_under_phone_platform(model):
    title = f"出 {model} 镜头膜"
    body = "镜头膜未拆封，售价29元，不含手机，可邮寄。"
    items = classify(title, body, platform_category="手机")

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "accessory.film"
    assert item["price"] == 29
    assert_subject(item, "镜头膜", title, body)
    assert policy.evaluate(items, PHONE_POLICY) == []
    assert policy.evaluate(items, ACCESSORY_POLICY) == items


def test_known_and_unknown_sales_keep_their_own_subjects_and_prices():
    title = "两件闲置分别出售"
    body = (
        "1. 出 iPhone 15 Pro 512G，镜头膜一直贴着，售价4498元，可邮寄。\n"
        "2. 出自制旋纹摆件，售价80元，可邮寄。"
    )
    items = classify(title, body)

    assert [(item["category"], item["price"]) for item in items] == [
        ("phone.smartphone", 4498), ("unknown", 80),
    ]
    assert_subject(items[0], "iPhone 15 Pro", title, body)
    assert_subject(items[1], "旋纹摆件", title, body)
    assert "旋纹摆件" not in items[0]["text"]
    assert "iPhone" not in items[1]["text"]
    assert policy.evaluate(items, PHONE_POLICY) == [items[0]]
    assert policy.evaluate(items, ACCESSORY_POLICY) == []


@pytest.mark.parametrize("title,body,platform_category", [
    ("出 iPhone 15 Pro 512G", "镜头膜一直贴着，出售价格4498元，可邮寄。", ""),
    ("出 澜星 Q8 Ultra 12+512G", "自用整机，镜头膜一直贴着，出售价格4498元，可邮寄。", "手机"),
])
def test_priced_protection_paragraph_does_not_discard_the_complete_title(title, body, platform_category):
    items = classify(title, body, platform_category=platform_category)

    assert len(items) == 1
    item = items[0]
    assert title in item["text"]
    assert item["category"] == "phone.smartphone"
    assert item["price"] == 4498
    assert item["attributes"]["price_status"] == "identified"
    assert "4498" in item["attributes"]["price_evidence"]


def test_enrichment_accepts_phone_subject_with_used_lens_protection():
    assert enrich.supported_category(
        "phone.smartphone", "iPhone 15 Pro",
        "出 iPhone 15 Pro 512G\n镜头膜一直贴着，售价4498元，可邮寄。",
    )


@pytest.mark.parametrize("evidence", [
    "出手机壳\n适配 iPhone 15 Pro，售价49元。",
    "出镜头膜\n适配 iPhone 15 Pro，售价29元。",
    "出 澜星 Q8 Ultra 12+512G\n换用 iPhone 15 Pro 后闲置，售价4498元。",
])
def test_enrichment_rejects_a_phone_name_borrowed_from_compatibility_or_replacement(evidence):
    assert not enrich.supported_category("phone.smartphone", "iPhone 15 Pro", evidence)
