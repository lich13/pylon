from __future__ import annotations

import pytest

from pylon.tasks.quotes.validation import valid_match, valid_quote


def quote_row(**changes):
    return {
        "sku_key": "example-sku",
        "brand": "测试品牌",
        "model_spec": "测试型号",
        "color": "黑",
        "product_name": "测试品牌 测试型号 黑",
        "quote_price": 1888,
        "normalized": "测试品牌测试型号黑",
        "source_url": "https://example.invalid/quotes",
        **changes,
    }


@pytest.mark.parametrize(
    "row",
    [
        {},
        quote_row(quote_price=None),
        quote_row(quote_price=""),
        quote_row(quote_price=0),
        quote_row(quote_price="00000"),
        quote_row(quote_price="缺货"),
        quote_row(status="缺货"),
        quote_row(availability="缺货"),
    ],
)
def test_empty_zero_and_out_of_stock_quotes_are_rejected(row: dict):
    assert valid_quote(row) is False


@pytest.mark.parametrize("field", ["sku_key", "brand", "model_spec", "product_name", "normalized"])
def test_required_quote_identity_fields_cannot_be_empty(field: str):
    assert valid_quote(quote_row(**{field: ""})) is False


@pytest.mark.parametrize("price", [50, 999, 1888, 50000])
def test_positive_in_range_quote_is_valid(price: int):
    assert valid_quote(quote_row(quote_price=price)) is True


@pytest.mark.parametrize("product_name", ["", "00000", "缺货"])
def test_marketplace_match_rejects_empty_or_placeholder_product_names(product_name: str):
    match = {
        "product_name": product_name,
        "sale_price": 1800,
        "quote_price": 2200,
        "profit": 400,
    }

    assert valid_match(match) is False
