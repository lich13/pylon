"""Synthetic regressions for attribute filters and separate sale/wanted policies."""

import pytest

from pylon.tasks.marketplace import classifier, policy, wanted


CATEGORY = "phone.smartphone"


def sale_policy(rules=None, **overrides):
    value = {
        "version": 1,
        "enabled": True,
        "categories": [CATEGORY],
        "filters": {CATEGORY: rules or {}},
    }
    value.update(overrides)
    return value


def item(facts=None, *, price=None, status="available"):
    return {
        "category": CATEGORY,
        "status": status,
        "price": price,
        "text": "求购 iPhone 15 Pro" if status == "buy" else "出售 iPhone 15 Pro",
        "attributes": {
            "facts": facts or {},
            "intent": "wanted" if status == "buy" else "sale",
            "body_complete": True,
        },
    }


def wanted_policy(**overrides):
    value = {
        "version": 1,
        "enabled": True,
        "targets": [{"category": CATEGORY, "model": "iPhone 15 Pro"}],
    }
    value.update(overrides)
    return wanted.validate(value)


@pytest.mark.parametrize("actual", ["Aster", "Helio", ["Helio", "Other"]])
def test_values_in_one_include_field_are_alternatives(actual):
    selected = sale_policy({"brand": {"include": ["Aster", "Helio"]}})

    assert policy.attribute_decision(item({"brand": actual}), selected) == ("match", [])
    assert policy.decision(item({"brand": actual}), selected) == ("match", [])


@pytest.mark.parametrize("facts,expected", [
    ({"brand": "Aster", "storage_gb": 512}, "match"),
    ({"brand": "Aster", "storage_gb": 128}, "exclude"),
    ({"brand": "Other", "storage_gb": 512}, "exclude"),
    ({"brand": "Aster"}, "review"),
])
def test_constraints_on_distinct_fields_all_have_to_match(facts, expected):
    selected = sale_policy({
        "brand": {"include": ["Aster", "Helio"]},
        "storage_gb": {"include": [256, 512]},
    })

    assert policy.attribute_decision(item(facts), selected)[0] == expected
    assert policy.decision(item(facts), selected)[0] == expected


@pytest.mark.parametrize("actual", ["Aster", ["Helio", "Aster"]])
def test_exclude_wins_when_the_same_value_is_included(actual):
    selected = sale_policy({
        "brand": {"include": ["Aster", "Helio"], "exclude": ["Aster"]},
    })

    assert policy.attribute_decision(item({"brand": actual}), selected) == (
        "exclude", ["brand:排除条件"],
    )


@pytest.mark.parametrize("missing_first", [True, False])
def test_explicit_exclude_wins_over_another_missing_field(missing_first):
    rules = [
        ("condition", {"include": ["全新"]}),
        ("brand", {"exclude": ["Aster"]}),
    ]
    if not missing_first:
        rules.reverse()

    assert policy.attribute_decision(
        item({"brand": "Aster"}), sale_policy(dict(rules)),
    ) == ("exclude", ["brand:排除条件"])


@pytest.mark.parametrize("rule", [{"include": ["Aster"]}, {"exclude": ["Aster"]}])
@pytest.mark.parametrize("actual", [None, "", []])
def test_missing_constrained_attribute_needs_review(rule, actual):
    assert policy.attribute_decision(
        item({"brand": actual}), sale_policy({"brand": rule}),
    ) == ("review", ["brand"])


@pytest.mark.parametrize("filters", [
    {},
    {CATEGORY: {}},
    {CATEGORY: {"condition": {}}},
    {CATEGORY: {"condition": {"include": []}}},
    {CATEGORY: {"condition": {"include": [], "exclude": []}}},
])
def test_missing_attribute_without_a_constraint_does_not_block_a_sale(filters):
    selected = sale_policy(filters=filters)

    assert policy.attribute_decision(item(), selected) == ("match", [])
    assert policy.decision(item(), selected) == ("match", [])


@pytest.mark.parametrize("price,expected", [
    (999, "exclude"),
    (1000, "match"),
    (2500, "match"),
    (4000, "match"),
    (4001, "exclude"),
    (None, "review"),
])
def test_price_range_is_inclusive_and_requires_a_known_price(price, expected):
    selected = sale_policy({"price": {"min": 1000, "max": 4000}})

    outcome, reasons = policy.attribute_decision(item(price=price), selected)
    assert outcome == expected
    if price is None:
        assert reasons == ["price"]


@pytest.mark.parametrize("rule,price,expected", [
    ({"min": 1000}, 1000, "match"),
    ({"min": 1000}, 999, "exclude"),
    ({"max": 4000}, 4000, "match"),
    ({"max": 4000}, 4001, "exclude"),
])
def test_one_sided_price_ranges(rule, price, expected):
    assert policy.attribute_decision(
        item(price=price), sale_policy({"price": rule}),
    )[0] == expected


def test_unknown_condition_is_not_assumed_to_be_used():
    items = classifier.classify_listing(
        "出售 iPhone 15 Pro", "售价4500元，可邮寄。", "v2ex", True,
    )
    assert len(items) == 1
    phone = items[0]
    assert phone["category"] == CATEGORY
    assert phone["attributes"]["facts"].get("condition") is None
    assert phone["attributes"]["condition"] != "二手"
    assert policy.decision(phone, sale_policy({"condition": {"include": ["二手"]}})) == (
        "review", ["condition"],
    )
    assert policy.decision(phone, sale_policy()) == ("match", [])


def test_explicit_used_condition_can_satisfy_the_used_filter():
    items = classifier.classify_listing(
        "出售 iPhone 15 Pro", "自用整机，售价4500元，可邮寄。", "v2ex", True,
    )
    assert len(items) == 1
    assert items[0]["attributes"]["facts"]["condition"] == "二手"
    assert policy.decision(items[0], sale_policy({"condition": {"include": ["二手"]}})) == (
        "match", [],
    )


@pytest.mark.parametrize("selection", [
    {"categories": [CATEGORY]},
    {"categories": [], "types": ["phone.smartphone"]},
    {"categories": [], "groups": ["phone"]},
    {"categories": [CATEGORY], "types": ["phone.smartphone"], "groups": ["phone"]},
])
def test_blocked_parent_overrides_all_child_subscription_forms(selection):
    selected = sale_policy(blocked_groups=["phone"], **selection)

    assert policy.decision(item(), selected) == ("exclude", ["大类已屏蔽"])
    assert not policy.selected(CATEGORY, selected)


def test_wanted_item_never_enters_sales_even_when_its_attributes_match():
    request = item({"brand": "Aster"}, price=3000, status="buy")
    selected = sale_policy({"brand": {"include": ["Aster"]}})

    assert policy.attribute_decision(request, selected) == ("match", [])
    assert policy.decision(request, selected) == ("exclude", ["不是在售商品"])
    assert policy.evaluate([request], selected) == []
    assert wanted.evaluate([request], wanted_policy()) == [request]


def test_sales_parent_block_does_not_change_independent_wanted_selection():
    sale = item()
    request = item(status="buy")
    selected = sale_policy(blocked_groups=["phone"])

    assert policy.evaluate([sale, request], selected) == []
    assert wanted.evaluate([sale, request], wanted_policy()) == [request]


def test_wanted_parent_block_does_not_change_independent_sales_selection():
    sale = item()
    request = item(status="buy")

    assert wanted.evaluate([sale, request], wanted_policy(blocked_groups=["phone"])) == []
    assert policy.evaluate([sale, request], sale_policy()) == [sale]
