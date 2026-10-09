"""Edition labels after prices must not be mistaken for quantity units."""

import pytest

from pylon.shared.sale_price import analyse_price
from pylon.tasks.marketplace import classifier, enrich


@pytest.mark.parametrize("edition", ["台版", "台湾版", "日版", "日本版", "国行", "港版"])
def test_phone_sale_price_accepts_region_edition_after_amount(edition):
    text = f"出 iPhone 15 Pro 256G，售价6500 {edition}，可邮寄。"

    assert classifier.asking_price(text) == 6500
    result = classifier.price_analysis(text)
    assert result.status == "identified"
    assert result.amount == 6500


@pytest.mark.parametrize("unit", ["台", "天", "日"])
def test_quantity_or_date_units_after_numbers_are_not_prices(unit):
    text = f"出 iPhone 15 Pro 256G，使用3{unit}，无售价。"

    result = analyse_price(
        text,
        allow_bare=True,
        subject=lambda line: classifier.category_for(line)[0] != "unknown",
        capacity_context=True,
    )
    assert result.amount is None
    assert classifier.asking_price(text) is None


@pytest.mark.parametrize("edition", ["台版", "台湾版", "日版", "日本版", "国行", "港版"])
def test_price_evidence_accepts_phone_price_followed_by_edition(edition):
    lines = ["出售 iPhone 15 Pro 256G", f"售价6500 {edition}，可邮寄。"]

    result = enrich.price_evidence(lines, 1, "phone.smartphone")

    assert result.status == "identified"
    assert result.amount == 6500

