"""Synthetic v14 regressions for service subjects and item-local delivery."""

import pytest

from pylon.tasks.marketplace import classifier, policy


SERVICE_POLICY = {
    "version": 1,
    "enabled": True,
    "categories": ["virtual.service"],
    "allow_beijing_pickup": True,
}


def classify(title: str, body: str):
    return classifier.classify_listing(title, body, "v2ex", True)


def only_item(title: str, body: str) -> dict:
    items = classify(title, body)
    assert len(items) == 1
    return items[0]


@pytest.mark.parametrize(
    ("title", "body", "category"),
    [
        ("出运动鞋", "运动鞋，20元，可邮寄", "life.shoes"),
        ("出洗衣机", "洗衣机，800元，可邮寄", "home.laundry"),
    ],
)
def test_physical_shoes_and_washing_machines_are_not_service_subjects(
    title, body, category
):
    item = only_item(title, body)

    assert item["category"] == category
    assert item["attributes"]["subject_kind"] == "goods"
    assert item["attributes"]["regional_service"] is False


def test_washing_shoes_service_is_one_virtual_item_and_guangzhou_is_excluded():
    item = only_item("出洗鞋服务", "广州洗鞋服务，20元，广州本地可用")

    assert item["category"] == "virtual.service"
    assert item["price"] == 20
    assert item["attributes"]["subject_kind"] == "service"
    assert item["attributes"]["regional_service"] is True
    assert item["attributes"]["usage_regions"] == ["广州"]
    assert item["attributes"]["transaction_scope"] == "non_beijing_local_only"
    assert policy.evaluate([item], SERVICE_POLICY) == []


@pytest.mark.parametrize(
    ("body", "scope", "allowed"),
    [
        ("洗鞋服务，20元，全国寄洗", "not_local", True),
        ("洗鞋服务，20元，全国服务可用", "not_local", True),
        ("北京洗鞋服务，20元", "beijing_local_ok", True),
        ("广州洗鞋服务，20元，仅限广州", "non_beijing_local_only", False),
        ("广州洗鞋服务，20元，卡券可邮寄", "non_beijing_local_only", False),
    ],
)
def test_service_scope_distinguishes_nationwide_use_from_a_mailed_local_coupon(
    body, scope, allowed
):
    item = only_item("出洗鞋服务", body)

    assert item["category"] == "virtual.service"
    assert item["attributes"]["transaction_scope"] == scope
    assert bool(policy.evaluate([item], SERVICE_POLICY)) is allowed


def test_remote_delivery_overrides_local_pickup_preference_for_a_physical_item():
    item = only_item(
        "出运动鞋",
        "广州运动鞋，50元，本地面交优先，也可邮寄",
    )

    assert item["category"] == "life.shoes"
    assert item["attributes"]["transaction_scope"] == "optional_pickup_or_remote"
    assert item["attributes"]["shipping"] is True
    assert item["attributes"]["pickup_only"] is False


def test_mixed_sales_keep_each_service_or_good_region_on_its_own_item():
    items = classify(
        "混售单件地区独立",
        "1. 广州洗鞋服务，20元\n"
        "2. 北京洗鞋服务，30元\n"
        "3. 广州运动鞋，50元，本地面交",
    )

    assert [(item["category"], item["price"]) for item in items] == [
        ("virtual.service", 20),
        ("virtual.service", 30),
        ("life.shoes", 50),
    ]
    assert [item["attributes"]["transaction_scope"] for item in items] == [
        "non_beijing_local_only",
        "beijing_local_ok",
        "non_beijing_local_only",
    ]
    assert policy.evaluate(items, SERVICE_POLICY) == [items[1]]
