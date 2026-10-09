from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

import pytest

from pylon.outbox import Outbox
from pylon.tasks.marketplace import classifier, policy, poll, sources, taxonomy
from pylon.tasks.marketplace import store as marketplace_store
from pylon.tasks.marketplace.pipeline import process
from pylon.tasks.marketplace.pipeline import format_message
from pylon.tasks.marketplace.store import Store


EXPECTED_CATEGORY_IDS = {
    "phone.smartphone",
    "phone.smartphone",
    "computer.desktop",
    "computer.mini",
    "computer.laptop",
    "component.gpu",
    "component.cpu",
    "component.memory",
    "component.mainboard",
    "storage.ssd_nvme",
    "storage.ssd_sata",
    "storage.hdd",
    "network.router_soft",
    "network.router",
    "accessory.phone_case",
}


def classify(body: str, title: str = "数码闲置") -> list[dict]:
    return classifier.classify_listing(
        title,
        body,
        source="v2ex",
        body_complete=True,
    )


def evaluate(items: list[dict], *categories: str) -> list[dict]:
    return policy.evaluate(
        items,
        {
            "version": 1,
            "enabled": True,
            "categories": list(categories),
            "allow_beijing_pickup": True,
        },
    )


def make_store(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    return box, Store(box)


def listing(*, body_complete: bool = True, history_body: str | None = None) -> dict:
    body = history_body or "iPhone 15 Pro 256G 4800元出售"
    return {
        "id": "42",
        "title": "出 iPhone 15 Pro",
        "url": "https://www.v2ex.com/t/42",
        "body_full": body if body_complete else "",
        "body_excerpt": body if not body_complete else "",
        "body_complete": body_complete,
    }


def selected_policy(*categories: str) -> dict:
    return {
        "version": 1,
        "enabled": True,
        "categories": list(categories),
        "allow_beijing_pickup": True,
    }


def test_taxonomy_uses_the_stable_marketplace_category_ids():
    assert isinstance(taxonomy.CATEGORIES, dict)
    assert EXPECTED_CATEGORY_IDS <= set(taxonomy.CATEGORIES)
    assert all(isinstance(path, str) and path.strip() for path in taxonomy.CATEGORIES.values())


def test_complete_first_floor_body_classifies_the_second_product():
    items = classify(
        "iPhone 15 Pro 256G 4800元出售\n"
        "RTX 4070 SUPER 独立显卡 4200元出售",
        title="两件数码产品一起出",
    )

    assert any(item["category"] == "phone.smartphone" for item in items)
    gpu = next(item for item in items if item["category"] == "component.gpu")
    assert "4070" in gpu["text"]
    assert {"category", "text", "status", "price", "evidence", "attributes"} <= gpu.keys()


def test_sold_first_item_does_not_suppress_an_available_second_item():
    items = classify(
        "iPhone 15 Pro 256G 已售\n"
        "RTX 4070 SUPER 独立显卡 4200元出",
        title="两件闲置分别处理",
    )
    phone = next(item for item in items if item["category"] == "phone.smartphone")
    assert phone["status"] == "sold"

    hits = evaluate(items, "phone.smartphone", "component.gpu")

    assert [item["category"] for item in hits] == ["component.gpu"]


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("求购 iPhone 15 Pro", "预算 4500 元，成色好就收"),
        ("收 RTX 4070 SUPER", "收一张独立显卡，价格私聊"),
        ("iPhone 15 Pro 询价", "只了解行情，不出售"),
        ("RTX 4070 SUPER 价格咨询", "请问这款显卡行情多少，不是出货"),
    ],
)
def test_buying_and_price_inquiries_are_not_policy_hits(title: str, body: str):
    items = classify(body, title=title)

    assert evaluate(items, "phone.smartphone", "component.gpu") == []


@pytest.mark.parametrize(
    ("title", "expected_status"),
    [
        ("[硬件交易][收] RTX 3070", "buy"),
        ("[硬件交易]收3070", "buy"),
        ("[硬件交易][出] RTX 3070 2000元出售", "available"),
        ("出 RTX 3070 2000元 [硬件交易]", "available"),
    ],
)
def test_hardware_trade_buy_markers_match_only_buy_posts(title: str, expected_status: str):
    items = classify("", title=title)

    gpu = next(item for item in items if item["category"] == "component.gpu")
    assert gpu["status"] == expected_status
    if expected_status == "buy":
        assert evaluate(items, "component.gpu") == []
    else:
        assert evaluate(items, "component.gpu") == [gpu]


def test_gpu_inside_a_whole_desktop_is_not_a_separate_gpu_listing():
    items = classify(
        "R7 7800X3D + RTX 4090 高配主机 15000元出，整机交易",
        title="整机出 RTX 4090 主机",
    )

    assert not any(item["category"] == "component.gpu" for item in items)
    assert evaluate(items, "component.gpu") == []


def test_phone_case_is_not_classified_or_matched_as_a_phone():
    items = classify(
        "全新 iPhone 15 Pro 手机壳，50元出，不含手机",
        title="iPhone 15 Pro 手机壳",
    )

    assert not any(item["category"] == "phone.smartphone" for item in items)
    assert not evaluate(items, "phone.smartphone")


def test_gifted_phone_case_is_not_treated_as_an_independent_sale():
    items = classify(
        "iPhone 15 Pro 256G 4800元出，手机壳随手机赠送，不单卖",
        title="iPhone 15 Pro 和手机壳",
    )

    assert not evaluate(items, "accessory.phone_case")


def test_mixed_post_can_match_its_known_product_among_other_items():
    items = classify(
        "iPhone 15 Pro 256G 4800元出；自制木质摆件 80元出",
        title="处理两件闲置物品",
    )

    hits = evaluate(items, "phone.smartphone")
    assert any(item["category"] == "phone.smartphone" for item in hits)


@pytest.mark.parametrize(
    ("body", "title", "expected_hit"),
    [
        ("RTX 4070 SUPER 独立显卡 4200元出，仅上海本地自提", "[上海] 显卡出", False),
        ("RTX 4070 SUPER 独立显卡 4200元出，北京本地自提", "[北京] 显卡出", True),
        ("RTX 4070 SUPER 独立显卡 4200元出，全国可邮寄", "显卡出", True),
    ],
)
def test_pickup_policy_excludes_other_cities_but_allows_beijing_or_shipping(
    body: str,
    title: str,
    expected_hit: bool,
):
    items = classify(body, title=title)

    assert bool(evaluate(items, "component.gpu")) is expected_hit


def test_price_is_none_when_listing_does_not_name_a_price():
    items = classify("iPhone 15 Pro 256G 可议价，感兴趣私聊，不标价")

    phone = next(item for item in items if item["category"] == "phone.smartphone")
    assert phone["price"] is None


def test_unknown_category_cannot_be_whitelisted_into_policy_hits():
    items = classify("新奇设备 Alpha 100元出售", title="出一台新奇设备")

    assert any(item["category"] == "unknown" for item in items)
    assert evaluate(items, "unknown") == []


def test_duplicate_ingest_and_restart_enqueue_only_one_notification(tmp_path):
    box, store = make_store(tmp_path)
    post = listing()
    assert store.ingest("v2ex", [post]) == 1
    assert store.ingest("v2ex", [post]) == 0

    restarted = Store(Outbox(box.path))
    with patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}):
        assert process(restarted, current_policy=selected_policy("phone.smartphone")) == 1
        assert restarted.ingest("v2ex", [post]) == 0
        assert process(Store(Outbox(box.path)), current_policy=selected_policy("phone.smartphone")) == 0

    with box.connect() as db:
        events = db.execute("SELECT id,kind FROM events WHERE job_id='marketplace-poll'").fetchall()
        row = db.execute("SELECT status FROM marketplace_posts WHERE source='v2ex' AND source_id='42'").fetchone()
    assert len(events) == 1
    assert events[0]["kind"] == "text"
    assert row["status"] == "queued"


def test_history_posts_are_never_queued_by_live_pipeline(tmp_path):
    box, store = make_store(tmp_path)
    assert store.ingest("v2ex", [listing()], history=True) == 1

    with patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}):
        assert process(store, current_policy=selected_policy("phone.smartphone")) == 0

    with box.connect() as db:
        row = db.execute("SELECT status,history FROM marketplace_posts").fetchone()
        events = db.execute("SELECT id FROM events WHERE job_id='marketplace-poll'").fetchall()
    assert row["status"] == "historical"
    assert row["history"] == 1
    assert events == []


def test_incomplete_body_stays_pending_and_body_error_can_be_retried(tmp_path):
    box, store = make_store(tmp_path)
    assert store.ingest("v2ex", [listing(body_complete=False)]) == 1
    pending = store.pending()
    assert len(pending) == 1
    assert pending[0]["status"] == "pending_body"
    assert pending[0]["body_complete"] == 0

    with patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}):
        assert process(store, current_policy=selected_policy("phone.smartphone")) == 0
    with box.connect() as db:
        assert db.execute("SELECT status FROM marketplace_posts").fetchone()["status"] == "pending_body"
        assert db.execute("SELECT id FROM events WHERE job_id='marketplace-poll'").fetchall() == []

    with patch.object(marketplace_store.time, "time", return_value=1000):
        store.body_error(pending[0], "TimeoutError")
    with patch.object(marketplace_store.time, "time", return_value=1059.9):
        assert store.pending() == []
    with patch.object(marketplace_store.time, "time", return_value=1060):
        retry = store.pending()
    assert len(retry) == 1
    assert retry[0]["attempts"] == 1
    assert retry[0]["error"] == "TimeoutError"
    assert retry[0]["status"] == "pending_body"


def test_only_selected_categories_are_passed_to_quote_matching(tmp_path):
    _, store = make_store(tmp_path)
    store.ingest("v2ex", [listing()])
    with (
        patch("pylon.tasks.quotes.health.inspect", return_value={"available": True}),
        patch("pylon.tasks.quotes.arbitrage.connect", return_value=nullcontext(None)),
        patch("pylon.tasks.quotes.arbitrage.load_quotes", return_value=[{"sku_key": "fixture"}]),
        patch("pylon.tasks.marketplace.pipeline._quote_matches", return_value=[]) as match_quotes,
    ):
        assert process(store, current_policy=selected_policy()) == 0

    match_quotes.assert_called_once()
    assert match_quotes.call_args.args[0] == []


def test_empty_formatted_message_does_not_enqueue_notification(tmp_path):
    box, store = make_store(tmp_path)
    store.ingest("v2ex", [listing()])
    with (
        patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}),
        patch("pylon.tasks.marketplace.pipeline.format_message", return_value=""),
    ):
        assert process(store, current_policy=selected_policy("phone.smartphone")) == 0

    with box.connect() as db:
        row = db.execute("SELECT status,event_id FROM marketplace_posts").fetchone()
        events = db.execute("SELECT id FROM events WHERE job_id='marketplace-poll'").fetchall()
    assert row["status"] == "skipped"
    assert row["event_id"] is None
    assert events == []


def test_title_only_notice_keeps_uncertainty_and_empty_items_are_silent():
    title = "出售 iPhone 15 Pro 256G"
    items = classifier.classify_listing(title, "", source="v2ex", body_complete=False)
    item = next(item for item in items if item["category"] == "phone.smartphone")
    post = {
        "source": "v2ex",
        "title": title,
        "url": "https://www.v2ex.com/t/42",
        "body": "",
        "body_complete": False,
    }

    message = format_message(post, [item], [])

    assert title in message
    assert "正文暂不可读，仅依据标题" in message
    assert "价格与交易条件请查看原帖" in message
    assert format_message(post, [], []) == ""


def test_coldboot_records_initial_page_as_history_without_broadcast(tmp_path):
    box, store = make_store(tmp_path)

    @contextmanager
    def fake_browser(_source):
        yield object()

    post = {
        "id": "100",
        "title": "出 iPhone 15 Pro",
        "url": "https://bbs.nga.cn/read.php?tid=100",
        "body_full": "iPhone 15 Pro 256G 4800元出售",
        "body_complete": True,
    }
    with (
        patch("pylon.tasks.marketplace.poll.pipeline.enabled", return_value=True),
        patch("pylon.tasks.marketplace.poll.Store", return_value=store),
        patch("pylon.tasks.marketplace.poll.sources.browser", side_effect=fake_browser),
        patch("pylon.tasks.marketplace.poll.sources.list_page", return_value=[post]),
        patch("pylon.tasks.marketplace.poll.sources.refill", return_value=[]),
        patch("pylon.tasks.marketplace.pipeline.policy.load", return_value=selected_policy("phone.smartphone")),
        patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}),
        patch("pylon.tasks.marketplace.poll.events.result"),
    ):
        assert poll.main() == 0

    with box.connect() as db:
        row = db.execute("SELECT status,history FROM marketplace_posts WHERE source='nga'").fetchone()
        events = db.execute("SELECT id FROM events WHERE job_id='marketplace-poll'").fetchall()
    assert row["status"] == "historical"
    assert row["history"] == 1
    assert events == []


def test_nga_poll_rate_limit_persists_cooldown_without_retrying_early(tmp_path, monkeypatch):
    _, store = make_store(tmp_path)
    now = {"value": 1000.0}
    monkeypatch.setattr(poll.time, "time", lambda: now["value"])

    @contextmanager
    def fake_browser(_source):
        yield object()

    with (
        patch("pylon.tasks.marketplace.poll.pipeline.enabled", return_value=True),
        patch("pylon.tasks.marketplace.poll.Store", return_value=store),
        patch("pylon.tasks.marketplace.poll.sources.browser", side_effect=fake_browser) as browser,
        patch("pylon.tasks.marketplace.poll.sources.list_page",
              side_effect=sources.SourceRateLimited("nga_rate_limited")) as list_page,
        patch("pylon.tasks.marketplace.poll.pipeline.process", return_value=0),
        patch("pylon.tasks.marketplace.poll.events.result"),
    ):
        assert poll.main() == 0
        original_cooldown = store.checkpoint("nga:cooldown")
        now["value"] += 10
        assert poll.main() == 0

    assert original_cooldown == {"until": 1060.0}
    assert store.checkpoint("nga:cooldown") == original_cooldown
    assert browser.call_count == 1
    assert list_page.call_count == 1

def test_v2ex_trade_routing_uses_the_explicit_trade_tag_only():
    from pylon.tasks.forums import v2ex as v2ex_notice

    tagged = v2ex_notice.Post(
        message_id=101,
        telegram_url="https://t.me/v2ex_notice/101",
        v2ex_url="https://www.v2ex.com/t/101",
        posted_at="2026-10-08T00:00:00+00:00",
        title="普通标题",
        tags=["二手交易"],
        core_title="普通标题",
        rough_type="other",
        body_excerpt="fixture",
    )
    rough_type_only = v2ex_notice.Post(
        message_id=102,
        telegram_url="https://t.me/v2ex_notice/102",
        v2ex_url="https://www.v2ex.com/t/102",
        posted_at="2026-10-08T00:01:00+00:00",
        title="推广活动",
        tags=["推广"],
        core_title="推广活动",
        rough_type="secondary_market",
        body_excerpt="fixture",
    )
    seen_by_original_flow = []

    with (
        patch.object(v2ex_notice, "load_state", return_value={"last_seen_id": 0}),
        patch.object(v2ex_notice, "save_state"),
        patch.object(v2ex_notice, "pop_pending_instant_alert", return_value=None),
        patch.object(v2ex_notice, "normalize_agent_delivery_batch", return_value={}),
        patch.object(
            v2ex_notice,
            "collect_recent_pages",
            return_value=([tagged, rough_type_only], {"pages_fetched": 1}),
        ),
        patch.object(v2ex_notice, "enrich_posts_with_topic_excerpt") as enrich,
        patch("pylon.tasks.marketplace.pipeline.enabled", return_value=True),
        patch("pylon.tasks.marketplace.pipeline.ingest") as ingest,
        patch.object(
            v2ex_notice,
            "route_decisions_by_message_id",
            side_effect=lambda posts: (
                seen_by_original_flow.extend(posts)
                or {post.message_id: v2ex_notice.RouteDecision("exclude") for post in posts}
            ),
        ),
        patch.object(v2ex_notice, "arbitrage_scan_posts", return_value=[]),
    ):
        assert v2ex_notice.run_once() == 0

    ingest.assert_called_once()
    assert [post["id"] for post in ingest.call_args.args[1]] == ["101"]
    assert [post.message_id for post in seen_by_original_flow] == [102]
    assert [post.message_id for post in enrich.call_args.args[0]] == [102]


def test_incomplete_listing_cannot_produce_a_quote_match_or_priority_label():
    item = {
        "category": "phone.smartphone",
        "text": "iPhone 15 Pro 4800元",
        "status": "available",
        "price": 4800,
        "evidence": "iPhone 15 Pro 4800元",
        "attributes": {
            "bundle": False,
            "shipping": False,
            "pickup_only": False,
            "beijing": False,
            "condition": "二手/未注明",
            "excluded_device": False,
        },
    }
    plausible_match = {
        "product_name": "iPhone 15 Pro",
        "sale_price": 4800,
        "quote_price": 5200,
        "profit": 400,
        "condition_evidence": "fixture",
        "price_evidence": "fixture",
    }

    with patch("pylon.tasks.quotes.matching.select_match", return_value=plausible_match) as select_match:
        from pylon.tasks.marketplace.pipeline import _quote_matches

        matches = _quote_matches([item], False, [{"sku_key": "fixture"}])

    select_match.assert_not_called()
    assert matches == []
    message = format_message(
        {
            "source": "v2ex",
            "title": "出 iPhone 15 Pro",
            "url": "https://www.v2ex.com/t/42",
            "body": "",
            "body_complete": False,
        },
        [item],
        matches,
    )
    assert "重点比价" not in message


def _marketplace_match(sale_price: int) -> dict:
    quote_price = 5200
    return {
        "product_name": "iPhone 15 Pro 256G",
        "sale_price": sale_price,
        "quote_price": quote_price,
        "profit": quote_price - sale_price,
        "condition_evidence": "fixture",
        "price_evidence": "fixture",
    }


def _marketplace_post(body: str) -> dict:
    return {
        "source": "v2ex",
        "title": "出 iPhone 15 Pro 256G",
        "url": "https://example.invalid/t/42",
        "body": body,
        "body_complete": True,
    }


def test_explicit_item_price_allows_matching_sale_price_and_priority_label():
    from pylon.tasks.marketplace.pipeline import _quote_matches

    item = classify("iPhone 15 Pro 256G 4800元出售")[0]
    match = _marketplace_match(4800)
    with patch("pylon.tasks.quotes.matching.select_match", return_value=match) as select_match:
        matches = _quote_matches([item], True, [{"sku_key": "fixture"}])

    select_match.assert_called_once()
    assert matches == [match]
    message = format_message(
        _marketplace_post(item["text"]),
        [item],
        matches,
    )
    assert "重点比价" in message
    assert "售价 4800" in message


def test_unknown_item_price_keeps_ordinary_notification_without_quote_match():
    from pylon.tasks.marketplace.pipeline import _quote_matches

    item = classify("iPhone 15 Pro 256G 可议价，感兴趣私聊，不标价")[0]
    assert item["price"] is None
    with patch(
        "pylon.tasks.quotes.matching.select_match",
        return_value=_marketplace_match(4800),
    ) as select_match:
        matches = _quote_matches([item], True, [{"sku_key": "fixture"}])

    select_match.assert_not_called()
    assert matches == []
    message = format_message(_marketplace_post(item["text"]), [item], matches)
    assert "重点比价" not in message
    assert "价格未标" in message


def test_match_with_another_items_price_is_rejected():
    from pylon.tasks.marketplace.pipeline import _quote_matches

    item = classify("iPhone 15 Pro 256G 4800元出售")[0]
    assert item["price"] == 4800
    # A valid-looking quote match for a different listing must not attach here.
    cross_price_match = _marketplace_match(4200)
    with patch(
        "pylon.tasks.quotes.matching.select_match",
        return_value=cross_price_match,
    ) as select_match:
        matches = _quote_matches([item], True, [{"sku_key": "fixture"}])

    select_match.assert_called_once()
    assert matches == []
    message = format_message(_marketplace_post(item["text"]), [item], matches)
    assert "重点比价" not in message
    assert "4800 元" in message


@pytest.mark.parametrize("limit", [12, 5])
def test_pending_backlog_is_fair_fifo_due_and_partitioned_by_history(tmp_path, limit):
    box, store = make_store(tmp_path)
    urls = {
        "chiphell": "https://www.chiphell.com/thread-{id}-1-1.html",
        "nga": "https://bbs.nga.cn/read.php?tid={id}",
        "v2ex": "https://www.v2ex.com/t/{id}",
    }

    # A large, older Chiphell backlog must not fill either bounded batch.
    for source_id in range(1, 25):
        store.ingest("chiphell", [{
            "id": str(source_id),
            "title": "出 iPhone 15 Pro",
            "url": urls["chiphell"].format(id=source_id),
            "posted_at": f"2018-01-{(source_id - 1) % 28 + 1:02d}T00:00:00+00:00",
            "body_complete": False,
        }], history=True)
    for source in ("v2ex", "nga"):
        for offset in range(2):
            source_id = str(offset + 1)
            store.ingest(source, [{
                "id": source_id,
                "title": "出 iPhone 15 Pro",
                "url": urls[source].format(id=source_id),
                "posted_at": f"2026-10-08T00:0{offset}:00+00:00",
                "body_complete": False,
            }], history=True)

    # Add a separate live queue and make one row in each partition not due yet.
    for source in ("chiphell", "nga", "v2ex"):
        for source_id in (101, 102):
            store.ingest(source, [{
                "id": str(source_id),
                "title": "出 iPhone 15 Pro",
                "url": urls[source].format(id=source_id),
                "posted_at": "2026-10-08T00:00:00+00:00",
                "body_complete": False,
            }])
    with box.connect() as db:
        db.execute(
            "UPDATE marketplace_posts SET next_attempt=200 WHERE source='chiphell' AND source_id='1' AND history=1"
        )
        db.execute(
            "UPDATE marketplace_posts SET next_attempt=200 WHERE source='v2ex' AND source_id='101' AND history=0"
        )

    with patch.object(marketplace_store.time, "time", return_value=100):
        historical = store.pending(history=True, limit=limit)
        live = store.pending(history=False, limit=limit)

    assert len(historical) <= limit
    assert len(live) <= limit
    assert historical and {row["history"] for row in historical} == {1}
    assert live and {row["history"] for row in live} == {0}
    assert {row["source"] for row in historical} == {"chiphell", "nga", "v2ex"}
    assert {row["source"] for row in live} == {"chiphell", "nga", "v2ex"}
    assert not any(row["source"] == "chiphell" and row["source_id"] == "1" for row in historical)
    assert not any(row["source"] == "v2ex" and row["source_id"] == "101" for row in live)

    for rows in (historical, live):
        for source in ("chiphell", "nga", "v2ex"):
            ids = [int(row["source_id"]) for row in rows if row["source"] == source]
            assert ids == sorted(ids)


def test_multiple_explicit_items_keep_their_own_prices_and_sold_status():
    items = classify(
        "iPhone 15 Pro 256G 4800元已售\n"
        "RTX 4070 SUPER 独立显卡 4200元出售\n"
        "Keychron Q1 机械键盘 900元出",
        title="处理几件数码闲置",
    )

    by_category = {item["category"]: item for item in items}
    assert by_category["phone.smartphone"]["status"] == "sold"
    assert by_category["phone.smartphone"]["price"] == 4800
    assert by_category["component.gpu"]["status"] == "available"
    assert by_category["component.gpu"]["price"] == 4200
    assert by_category["input.keyboard"]["status"] == "available"
    assert by_category["input.keyboard"]["price"] == 900


def test_same_category_sold_and_available_items_keep_separate_status_and_price():
    items = classify("iPhone 15 Pro 256G 已出，iPhone 14 128G 2500元仍在售")

    assert len(items) == 2
    assert "iPhone 15 Pro" in items[0]["text"]
    assert items[0]["status"] == "sold"
    assert items[0]["price"] is None
    assert "iPhone 14" in items[1]["text"]
    assert items[1]["status"] == "available"
    assert items[1]["price"] == 2500


def test_sold_phone_and_shippable_gpu_split_when_separated_by_commas():
    items = classify("iPhone 15 128G 已售，RTX 4070 独立显卡 3000元出，可邮寄")

    assert len(items) == 2
    phone, gpu = items
    assert phone["category"] == "phone.smartphone"
    assert phone["status"] == "sold"
    assert gpu["category"] == "component.gpu"
    assert gpu["price"] == 3000
    assert gpu["status"] == "available"
    assert evaluate(items, "component.gpu") == [gpu]


def test_item_prices_stay_with_products_around_battery_and_shipping_clauses():
    items = classify(
        "iPhone 15 3000元出，电池90%，RTX 4070 独立显卡 2000元出，包邮"
    )

    assert len(items) == 2
    phone, gpu = items
    assert [phone["category"], gpu["category"]] == ["phone.smartphone", "component.gpu"]
    assert [phone["price"], gpu["price"]] == [3000, 2000]
    assert not any(item["category"] in {"accessory.battery", "accessory.charger"} for item in items)


def test_shared_shanghai_pickup_clause_excludes_both_separate_items():
    items = classify(
        "iPhone 15 3000元出，RTX 4070 独立显卡 2000元出，上海仅自提"
    )

    assert [item["category"] for item in items] == ["phone.smartphone", "component.gpu"]
    assert all(item["attributes"]["transaction_scope"] == "non_beijing_local_only" for item in items)
    assert evaluate(items, "phone.smartphone", "component.gpu") == []


def test_phone_condition_and_included_accessory_do_not_create_extra_sales():
    items = classify(
        "iPhone 15 Pro 256G 3000元出，电池健康度90%，边框轻微划痕，附赠原装充电器"
    )

    assert len(items) == 1
    assert items[0]["category"] == "phone.smartphone"
    assert items[0]["price"] == 3000
    assert evaluate(items, "accessory.battery", "accessory.charger") == []


@pytest.mark.parametrize("protection", [
    "一直贴膜使用", "全程带壳贴膜", "屏幕贴了钢化膜", "贴膜有轻微划痕", "已撕掉钢化膜",
])
def test_screen_protection_describes_the_phone_not_an_accessory_sale(protection):
    items = classify(f"iPhone 15 Pro 256G\n{protection}\n售价 3000元，包邮", title="出 iPhone 15 Pro")

    assert [(item["category"], item["price"]) for item in items] == [("phone.smartphone", 3000)]
    assert protection in items[0]["text"]
    assert evaluate(items, "accessory.film") == []


def test_priced_phone_with_film_condition_does_not_create_another_item():
    items = classify("iPhone 15 Pro 3000元出售，一直贴膜使用，屏幕完好", title="出 iPhone 15 Pro")
    assert [(item["category"], item["price"]) for item in items] == [("phone.smartphone", 3000)]


def test_film_sold_separately_keeps_its_category_and_price():
    items = classify(
        "iPhone 15 Pro 3000元出售，一直贴膜使用\n全新 iPhone 15 Pro 钢化膜20元单出",
        title="两件闲置出售",
    )
    assert [(item["category"], item["price"]) for item in items] == [
        ("phone.smartphone", 3000), ("accessory.film", 20),
    ]


def test_trailing_national_shipping_keeps_same_category_sales_separate():
    items = classify(
        "iPhone 15 Pro 256G 已售，iPhone 14 128G 2500元出，全国包邮"
    )

    assert len(items) == 2
    assert [item["category"] for item in items] == ["phone.smartphone", "phone.smartphone"]
    assert [item["status"] for item in items] == ["sold", "available"]
    assert [item["price"] for item in items] == [None, 2500]
    assert evaluate(items, "phone.smartphone") == [items[1]]


def test_desktop_configuration_does_not_create_separate_component_listings():
    items = classify(
        "R7 7800X3D + RTX 4090 + 64GB DDR5 + 2TB M.2 SSD 高配台式整机 15999元出售",
        title="高配台式整机出售",
    )

    assert [item["category"] for item in items] == ["computer.desktop"]
    assert evaluate(items, "component.cpu", "component.gpu", "component.memory", "storage.ssd_nvme") == []


def test_ipad_pro_m2_is_not_classified_as_nvme_storage():
    items = classify(
        "iPad Pro M2 11英寸 256GB 4800元出售",
        title="出 iPad Pro M2",
    )

    assert any(item["category"] == "tablet.tablet" for item in items)
    assert not any(item["category"] == "storage.ssd_nvme" for item in items)


def test_unused_charger_does_not_make_the_phone_listing_new():
    items = classify(
        "iPhone 15 Pro 256G 4800元出售\n附原装充电器全新未使用",
        title="出 iPhone 15 Pro",
    )

    phone = next(item for item in items if item["category"] == "phone.smartphone")
    assert phone["attributes"]["condition"] != "全新"


def test_ps5_console_is_not_a_desktop_computer():
    items = classify("PS5 游戏主机 2500元出售", title="出 PS5 游戏主机")

    assert any(item["category"] == "game.console" for item in items)
    assert not any(item["category"] == "computer.desktop" for item in items)


def test_nas_mechanical_drive_is_not_classified_as_a_nas_system():
    items = classify(
        "群晖 NAS 专用 8TB 机械硬盘 900元出售",
        title="出 NAS 机械硬盘",
    )

    assert any(item["category"] == "storage.hdd" for item in items)
    assert not any(item["category"] == "storage.nas" for item in items)


def test_server_ecc_memory_is_not_classified_as_a_server_system():
    items = classify(
        "服务器拆机 ECC RDIMM 内存条 64GB 800元出售",
        title="出服务器 ECC 内存",
    )

    assert any(item["category"] == "component.memory_ecc" for item in items)
    assert not any(item["category"] == "computer.server" for item in items)


def test_explicit_product_title_takes_precedence_over_body_accessory():
    items = classify(
        "附原装充电器全新未使用",
        title="出 iPhone 15 Pro 256G 4800元",
    )

    assert [item["category"] for item in items] == ["phone.smartphone"]
    assert items[0]["price"] == 4800
    assert items[0]["attributes"]["condition"] != "全新"


def test_v2ex_trade_body_includes_subtle_supplement_only_when_requested():
    from pylon.tasks.forums.v2ex import extract_topic_body

    html = (
        '<div class="topic_content">iPhone 15 Pro 256G 4800元出售</div>'
        '<div class="subtle">附言：原装充电器全新未使用</div>'
    )

    original_scope = extract_topic_body(html, include_supplements=False)
    trade_scope = extract_topic_body(html, include_supplements=True)

    assert "iPhone 15 Pro" in original_scope
    assert "附言" not in original_scope
    assert "附言" in trade_scope
    assert "充电器全新未使用" in trade_scope


def test_bracketed_v2ex_trade_and_beijing_tags_allow_local_pickup():
    items = classify(
        "RTX 4070 SUPER 独立显卡 4200元出售\n交易方式：支持自提",
        title="[二手交易][北京] 出 RTX 4070 SUPER",
    )

    gpu = next(item for item in items if item["category"] == "component.gpu")
    assert gpu["attributes"]["transaction_scope"] == "beijing_local_ok"
    assert evaluate(items, "component.gpu") == [gpu]


def test_shanghai_pickup_remains_excluded_even_when_escrow_uses_xianyu():
    items = classify(
        "RTX 4070 SUPER 独立显卡 4200元出售\n仅限上海自提，可走闲鱼",
        title="[二手交易][上海] 出 RTX 4070 SUPER",
    )

    gpu = next(item for item in items if item["category"] == "component.gpu")
    assert gpu["attributes"]["transaction_scope"] == "non_beijing_local_only"
    assert evaluate(items, "component.gpu") == []


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("iPhone 15 Pro 求助", "请帮忙估价一下"),
        ("求推荐 RTX 4070 SUPER", "预算 4000 元，求推荐型号"),
        ("帮忙估价 iPhone 15 Pro", "看看大概多少钱"),
    ],
)
def test_help_estimate_and_recommendation_posts_are_not_sales(title: str, body: str):
    items = classify(body, title=title)

    assert items
    assert all(item["status"] == "buy" for item in items)
    assert evaluate(items, "phone.smartphone", "component.gpu") == []


def test_iphone_price_3090_yuan_is_not_misread_as_a_gpu_model():
    items = classify("iPhone 13 128G 3090元出售", title="出 iPhone 13")

    assert [item["category"] for item in items] == ["phone.smartphone"]
    assert items[0]["price"] == 3090


def test_13700kf_alias_is_classified_as_a_cpu():
    items = classify("i7-13700KF 处理器 1800元出售", title="出 i7-13700KF")

    cpu = next(item for item in items if item["category"] == "component.cpu")
    assert "13700KF" in cpu["text"]
    assert cpu["price"] == 1800


def test_m2_shape_does_not_claim_nvme_without_an_interface_signal():
    sata = classify("M.2 SATA SSD 1TB 300元出售", title="出 M.2 SATA SSD")
    unspecified = classify("M.2 SSD 1TB 280元出售", title="出 M.2 SSD")

    assert any(item["category"] == "storage.ssd_sata" for item in sata)
    assert not any(item["category"] == "storage.ssd_nvme" for item in sata)
    assert not any(item["category"] == "storage.ssd_nvme" for item in unspecified)


def test_quote_database_read_race_does_not_block_regular_marketplace_notice(tmp_path):
    box, store = make_store(tmp_path)
    store.ingest("v2ex", [listing()])
    with (
        patch("pylon.tasks.quotes.health.inspect", return_value={"available": True}),
        patch("pylon.tasks.quotes.arbitrage.connect", side_effect=sqlite3.OperationalError("fixture read race")),
    ):
        assert process(store, current_policy=selected_policy("phone.smartphone")) == 1

    with box.connect() as db:
        row = db.execute("SELECT status,event_id FROM marketplace_posts").fetchone()
        events = db.execute("SELECT id,kind FROM events WHERE job_id='marketplace-poll'").fetchall()
    assert row["status"] == "queued"
    assert row["event_id"] == events[0]["id"]
    assert [(event["kind"]) for event in events] == ["text"]


def test_enqueue_failure_rolls_back_post_state_and_retry_creates_one_event(tmp_path):
    box, store = make_store(tmp_path)
    store.ingest("v2ex", [listing()])
    with (
        patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}),
        patch.object(box, "enqueue", side_effect=sqlite3.OperationalError("fixture enqueue failure")),
    ):
        with pytest.raises(sqlite3.OperationalError, match="fixture enqueue failure"):
            process(store, current_policy=selected_policy("phone.smartphone"))

    with box.connect() as db:
        assert db.execute("SELECT status FROM marketplace_posts").fetchone()["status"] == "ready"
        assert db.execute("SELECT id FROM events WHERE job_id='marketplace-poll'").fetchall() == []

    with patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}):
        assert process(store, current_policy=selected_policy("phone.smartphone")) == 1
        assert process(store, current_policy=selected_policy("phone.smartphone")) == 0
    with box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events WHERE job_id='marketplace-poll'").fetchone()[0] == 1
        assert db.execute("SELECT status FROM marketplace_posts").fetchone()["status"] == "queued"


@pytest.mark.parametrize(
    ("closure", "expected_status"),
    [
        ("全部已出", "sold"),
        ("全部售罄", "sold"),
        ("全部出售，可单买", "available"),
    ],
)
def test_explicit_global_sold_closure_updates_all_items_without_treating_sale_word_as_sold(
    closure: str,
    expected_status: str,
):
    items = classify(
        "iPhone 15 Pro 256G 4800元出售\n"
        "RTX 4070 SUPER 独立显卡 4200元出售\n"
        + closure,
        title="处理两件数码闲置",
    )

    assert {item["category"] for item in items} == {"phone.smartphone", "component.gpu"}
    assert {item["status"] for item in items} == {expected_status}


@pytest.mark.parametrize("closure", ["两条内存全部已售", "DDR5 内存全部售罄"])
def test_named_group_sold_does_not_close_another_available_item(closure):
    items = classify(
        closure + "\niPhone 15 Pro 256G 4800元出售，支持邮寄",
        title="处理数码闲置",
    )
    assert next(i for i in items if i["category"].startswith("component.memory"))["status"] == "sold"
    phone = evaluate(items, "phone.smartphone")
    assert len(phone) == 1
    assert phone[0]["price"] == 4800
    assert phone[0]["status"] == "available"


def test_named_group_sold_title_with_another_sale_keeps_that_sale_available():
    items = classify(
        "DDR5 内存全部已售\niPhone 15 Pro 256G 4800元出售，支持邮寄",
        title="内存全部已售，另出 iPhone 15 Pro",
    )
    assert len(evaluate(items, "phone.smartphone")) == 1


def test_marketplace_stats_separate_source_sales_sold_items_and_mixed_posts(tmp_path):
    _, store = make_store(tmp_path)
    assert store.ingest("v2ex", [{
        "id": "71",
        "title": "处理两件数码闲置",
        "url": "https://www.v2ex.com/t/71",
        "body_full": (
            "iPhone 15 Pro 256G 4800元出售\n"
            "RTX 4070 SUPER 独立显卡 4200元已售"
        ),
        "body_complete": True,
    }]) == 1
    assert store.ingest("chiphell", [{
        "id": "72",
        "title": "出 iPhone 15 Pro",
        "url": "https://www.chiphell.com/thread-72-1-1.html",
        "body_full": "iPhone 15 Pro 256G 4800元已售",
        "body_complete": True,
    }]) == 1
    assert store.ingest("nga", [{
        "id": "73",
        "title": "出 RTX 4070 SUPER",
        "url": "https://bbs.nga.cn/read.php?tid=73",
        "body_full": "RTX 4070 SUPER 独立显卡 4200元出售",
        "body_complete": True,
    }]) == 1

    stats = store.stats()
    assert stats["coverage"]["v2ex"]["available_items"] == 1
    assert stats["coverage"]["v2ex"]["mixed_posts"] == 1
    assert stats["coverage"]["chiphell"]["available_items"] == 0
    assert stats["coverage"]["nga"]["available_items"] == 1
    phone = stats["categories"]["phone.smartphone"]
    assert phone["statuses"]["available"] == 1
    assert phone["statuses"]["sold"] == 1
    assert phone["sale_items"] == {"v2ex": 1}
    assert phone["sale_posts"] == {"v2ex": 1}
    gpu = stats["categories"]["component.gpu"]
    assert gpu["statuses"]["available"] == 1
    assert gpu["statuses"]["sold"] == 1
    assert gpu["sale_items"] == {"nga": 1}


def test_ingest_rejects_non_numeric_source_ids(tmp_path):
    _, store = make_store(tmp_path)

    accepted = store.ingest("nga", [{
        "id": "thread-74",
        "title": "出 iPhone 15 Pro",
        "url": "https://bbs.nga.cn/read.php?tid=thread-74",
        "body_full": "iPhone 15 Pro 256G 4800元出售",
        "body_complete": True,
    }])

    assert accepted == 0
    assert store.stats()["coverage"] == {}


def test_category_examples_use_sale_subjects_not_compatible_or_configured_devices(tmp_path):
    _, store = make_store(tmp_path)
    fixtures = [
        ("phone", "出 iPhone 15 Pro", "iPhone 15 Pro 256G 4800元出售"),
        ("case", "出手机壳", "手机壳 50元出售，兼容 iPhone 16 Pro"),
        ("computer", "出台式整机", "台式整机 6000元出售\n配置：RTX 4070 显卡"),
        ("wanted", "求购 iPhone 17 Pro", "想收 iPhone 17 Pro，预算 6000元"),
    ]
    for number, (_, title, body) in enumerate(fixtures, 81):
        store.ingest("v2ex", [{"id": str(number), "title": title,
                              "url": f"https://www.v2ex.com/t/{number}",
                              "body_full": body, "body_complete": True}], history=True)

    categories = store.stats()["categories"]
    assert categories["phone.smartphone"]["examples"] == ["iPhone 15 Pro"]
    assert categories["accessory.phone_case"]["examples"] == ["手机壳"]
    assert categories["computer.desktop"]["examples"] == ["台式"]


def test_chiphell_attachment_metadata_is_removed_only_from_classification_copy(tmp_path):
    body = (
        "iPhone 15 Pro 256G 4800元出售\n"
        "1.jpg (321.00 KB, 下载次数:\n"
        "0)\n下载附件\n2026-10-8 12:30 上传\n"
        "* 1.5件赠品说明\n"
        "1.5件库存\n"
        "2.jpg (812.50 KB, 下载次数: 2)\n下载附件\n2026-10-8 12:31 上传\n"
        "RTX 4070 SUPER 独立显卡 4200元出售\n"
        "3.jpg (1.20 MB, 下载次数:\n"
        "0)\n下载附件\n2026-10-8 12:32 上传"
    )
    cleaned = classifier._sale_body(body, "chiphell")
    assert cleaned == (
        "iPhone 15 Pro 256G 4800元出售\n"
        "* 1.5件赠品说明\n"
        "1.5件库存\n"
        "RTX 4070 SUPER 独立显卡 4200元出售"
    )

    blocks = classifier._blocks("两件数码闲置", cleaned)
    assert len(blocks) == 2
    assert "* 1.5件赠品说明" in blocks[0]
    assert "1.5件库存" in blocks[0]

    box, store = make_store(tmp_path)
    assert store.ingest("chiphell", [{
        "id": "1701",
        "title": "两件数码闲置",
        "url": "https://www.chiphell.com/thread-1701-1-1.html",
        "body_full": body,
        "body_complete": True,
    }]) == 1
    with box.connect() as db:
        row = db.execute(
            "SELECT body,items FROM marketplace_posts WHERE source='chiphell' AND source_id='1701'"
        ).fetchone()

    items = json.loads(row["items"])
    assert row["body"] == body
    assert [(item["category"], item["price"]) for item in items] == [
        ("phone.smartphone", 4800),
        ("component.gpu", 4200),
    ]


@pytest.mark.parametrize("source", ["nga", "v2ex"])
def test_attachment_looking_authored_text_is_preserved_for_other_sources(source: str):
    body = "1.jpg (321.00 KB, 下载次数:\n0)\n下载附件\n2026-10-8 12:30 上传"

    assert classifier._sale_body(body, source) == body


def test_legion_go_is_a_handheld_and_ps5_game_media_stays_a_media_listing():
    handheld = classifier.classify_listing(
        "出 Legion Go 掌机",
        "联想拯救者 Legion Go 掌机 2500元出售",
        source="v2ex",
        body_complete=True,
    )
    media = classify("PS5 游戏卡带 300元出售", title="出 PS5 卡带")

    assert [item["category"] for item in handheld] == ["game.handheld"]
    assert [item["category"] for item in media] == ["game.media"]


def test_marketplace_offer_and_incomplete_post_stats_preserve_item_counters(tmp_path):
    box, store = make_store(tmp_path)
    assert store.ingest("v2ex", [{
        "id": "1801",
        "title": "出 iPhone 15 Pro",
        "url": "https://www.v2ex.com/t/1801",
        "body_full": "iPhone 15 Pro 4800元出售",
        "body_complete": True,
    }]) == 1
    assert store.ingest("chiphell", [{
        "id": "1802",
        "title": "iPhone 15 Pro",
        "url": "https://www.chiphell.com/thread-1802-1-1.html",
        "body_full": "",
        "body_complete": False,
    }]) == 1
    assert store.ingest("nga", [{
        "id": "1803",
        "title": "iPhone 15 Pro",
        "url": "https://bbs.nga.cn/read.php?tid=1803",
        "body_full": "",
        "body_complete": False,
    }]) == 1

    def item(category: str, status: str) -> dict:
        return {
            "category": category,
            "text": "fixture",
            "status": status,
            "price": None,
            "evidence": "fixture",
            "attributes": {},
        }

    fixtures = {
        ("v2ex", "1801"): [
            item("phone.smartphone", "available"),
            item("phone.smartphone", "sold"),
        ],
        ("chiphell", "1802"): [
            item("phone.smartphone", "buy"),
            item("phone.smartphone", "uncertain"),
            item("component.gpu", "available"),
            item("component.gpu", "sold"),
        ],
        ("nga", "1803"): [
            item("phone.smartphone", "buy"),
            item("phone.smartphone", "uncertain"),
        ],
    }
    with box.connect() as db:
        for (source, source_id), items in fixtures.items():
            db.execute(
                "UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=?",
                (json.dumps(items), source, source_id),
            )

    stats = store.stats()["categories"]
    phone = stats["phone.smartphone"]
    assert phone["posts"] == {"v2ex": 1, "chiphell": 1, "nga": 1}
    assert phone["items"] == {"v2ex": 2, "chiphell": 2, "nga": 2}
    assert phone["statuses"] == {"available": 1, "sold": 1, "buy": 2, "uncertain": 2}
    assert phone["offer_posts"] == {"v2ex": 1}
    assert phone["offer_items"] == {"v2ex": 2}
    assert phone["incomplete_posts"] == {"chiphell": 1, "nga": 1}
    assert phone["sale_posts"] == {"v2ex": 1}
    assert phone["sale_items"] == {"v2ex": 1}

    gpu = stats["component.gpu"]
    assert gpu["posts"] == {"chiphell": 1}
    assert gpu["items"] == {"chiphell": 2}
    assert gpu["offer_posts"] == {"chiphell": 1}
    assert gpu["offer_items"] == {"chiphell": 2}
    assert gpu["incomplete_posts"] == {"chiphell": 1}


def test_phone_max_storage_is_not_a_router_and_split_prices_stay_with_phone():
    items = classify(
        "iPhone 16 Pro Max512\n自用成色正常\n6200包邮\n"
        "iPhone 17 Pro 256GB\n电池无损耗\n8100包邮",
        title="出 iPhone 16 Pro Max512 和 iPhone 17 Pro",
    )
    assert [(i["category"], i["price"]) for i in items] == [
        ("phone.smartphone", 6200), ("phone.smartphone", 8100),
    ]
    assert classifier.category_for("AX3000 无线路由器")[0] == "network.router"


@pytest.mark.parametrize("subject", ["出13pro max国行", "闲置16PROMAX国行", "出14pm成色正常"])
def test_phone_shorthand_next_to_chinese_text_keeps_its_model_boundary(subject):
    items=classify(subject,title=subject)
    assert items[0]['category'] == 'phone.smartphone'
    assert items[0]['attributes']['brands'] == ['苹果']


@pytest.mark.parametrize("subject", ["出索尼 A7C国行", "出佳能R62国行", "索尼A7RIV出售", "A7CR出售", "A7M4出售"])
def test_camera_body_models_next_to_chinese_text_keep_their_model_boundary(subject):
    assert classifier.category_for(subject)[0] == "camera.body"


@pytest.mark.parametrize("subject", ["出X17PROMAX4", "出ZONEA7C", "出A7CUSTOM", "出R62DEVICE"])
def test_product_shorthand_does_not_match_inside_a_different_identifier(subject):
    assert classifier.category_for(subject)[0] == "unknown"


@pytest.mark.parametrize(("subject", "brands"), [
    ("出小米17PM国行", ["小米"]),
    ("出小米17 Pro Max国行", ["小米"]),
    ("出iPhone 17 Pro Max国行", ["苹果"]),
    ("闲置17PROMAX国行", []),
    ("出全新手机", []),
])
def test_phone_brand_is_an_attribute_not_a_category(subject, brands):
    items=classify(subject,title=subject)
    assert items[0]['category']=='phone.smartphone'
    assert items[0]['attributes']['brands']==brands


def test_phone_with_unresolved_brand_cannot_use_a_brand_specific_priority_quote():
    from pylon.tasks.marketplace.pipeline import _quote_matches

    items = classify("17PM 全新未激活 256GB 4800元出售", title="出17PM")
    assert items[0]['attributes']['brand_confirmed'] is False
    assert len(evaluate(items, "phone.smartphone")) == 1
    with patch("pylon.tasks.quotes.matching.select_match", side_effect=AssertionError("unresolved brand")):
        assert _quote_matches(items, True, []) == []


def test_system_and_separately_priced_watercooled_cards_keep_sale_boundaries():
    items = classify(
        "台式主机\n已预订\nR9 5900X\n价格1490\n"
        "技嘉 RTX 3080\n使用分体水冷\n冷头状态正常\n价格2450\n"
        "技嘉 RTX 3080，原装冷头状态正常\n价格2650\n"
        "另出 NVMe SSD SN730\n价格990\n可邮寄",
        title="出整机和两张显卡、硬盘",
    )
    assert [(i["category"], i["price"], i["status"]) for i in items] == [
        ("computer.desktop", 1490, "sold"),
        ("component.gpu", 2450, "available"),
        ("component.gpu", 2650, "available"),
        ("storage.ssd_nvme", 990, "available"),
    ]
    assert len(evaluate(items, "component.gpu", "storage.ssd_nvme")) == 3


def test_numbered_system_configuration_costs_are_not_individual_sales():
    items = classify(
        "台式整机出售\n配置如下：\n1. CPU R7 7800X3D 1800元\n"
        "2. RTX 4070 显卡 3200元\n3. DDR5 内存 400元\n整机打包，不拆卖",
        title="出台式整机",
    )
    assert all(i["category"] == "computer.desktop" for i in items)
    assert evaluate(items, "component.gpu", "component.cpu", "component.memory") == []


def test_laptop_specs_before_description_do_not_create_gpu_sale():
    items = classify(
        "配置记录\n处理器 Ryzen 6800H\nDDR5 内存 32GB\nRTX 3060 显卡\n"
        "笔记本正常出售\n主机电源完好\n价格4200",
        title="出 ThinkPad 笔记本",
    )
    assert items and all(i["category"] == "computer.laptop" for i in items)
    assert evaluate(items, "component.gpu", "component.memory") == []


@pytest.mark.parametrize(("title", "body", "category"), [
    ("出 DDR5 内存", "DDR5 32GB 内存 500元出售", "component.memory"),
    ("出 DDR5 内存", "笔记本 DDR5 32GB\n价格500", "component.memory_laptop"),
    ("出 SO-DIMM 内存", "SO-DIMM 32GB 内存 500元出售", "component.memory_laptop"),
    ("出 UDIMM 内存", "UDIMM 32GB 内存 500元出售", "component.memory_desktop"),
    ("出 ECC 内存", "服务器拆机 ECC RDIMM 内存 500元出售", "component.memory_ecc"),
])
def test_memory_form_factor_requires_explicit_evidence(title, body, category):
    assert [i["category"] for i in classify(body, title)] == [category]


def test_gpu_cooling_description_is_not_a_separate_waterblock_sale():
    card = classify("技嘉 RTX3080，原装冷头状态良好，价格2400", title="出 RTX3080 显卡")
    waterblock = classify("适配 RTX3080 的冷头单独出售，价格400", title="出显卡水冷头")
    assert [i["category"] for i in card] == ["component.gpu"]
    assert [i["category"] for i in waterblock] == ["component.custom_loop"]


@pytest.mark.parametrize(("body", "expected"), [
    ("台式整机3000元出售、Apple Watch 800元", [
        ("computer.desktop", 3000), ("wearable.watch", 800),
    ]),
    ("ThinkPad 笔记本3000元出售，MacBook 笔记本4000元出售", [
        ("computer.laptop", 3000), ("computer.laptop", 4000),
    ]),
    ("台式整机3000元出售、RTX 4070 显卡2500元出售", [
        ("computer.desktop", 3000), ("component.gpu", 2500),
    ]),
])
def test_separately_priced_system_and_other_sales_are_not_configuration(body, expected):
    items = classify(body, title="出售闲置设备")
    assert [(i["category"], i["price"]) for i in items] == expected
    assert len(evaluate(items, expected[-1][0])) >= 1


@pytest.mark.parametrize("body", [
    "台式整机3000元出售，配置清单：RTX 4070 显卡2500元，DDR5 内存500元",
    "台式整机3000元出售、RTX 4070 显卡2500元、DDR5 内存500元，不拆卖",
    "台式整机3000元出售、RTX 4070 显卡2500元",
])
def test_comma_separated_component_values_are_not_independent_sales(body):
    items = classify(body, title="出台式整机")
    assert items and all(i["category"] == "computer.desktop" for i in items)
    assert evaluate(items, "component.gpu", "component.memory") == []


def test_asking_price_accepts_chinese_word_boundary_but_not_model_digits():
    assert classifier.asking_price("整机3000元出售") == 3000
    assert classifier.asking_price("RTX4090元件") is None
    assert classifier.asking_price("原价3000元，售价2000元") == 2000


def test_unscoped_exclusive_bundle_cannot_create_individual_priority_matches():
    from pylon.tasks.marketplace.pipeline import _quote_matches

    body = ("iPhone 15 Pro 256GB 全新未激活 4800元出售\n"
            "iPad Air 全新未激活 2500元出售\n全部打包7000元，不拆卖")
    items = classify(body, title="两件闲置出售")
    assert len(items) == 2
    assert all(i["price"] is None and i["attributes"]["bundle"] for i in items)
    assert {i["attributes"]["bundle_price"] for i in items} == {7000}
    hits = evaluate(items, "phone.smartphone")
    assert len(hits) == 1
    with patch("pylon.tasks.quotes.matching.select_match") as matcher:
        assert _quote_matches(hits, True, []) == []
    matcher.assert_not_called()
    message = format_message(_marketplace_post(body), hits, [])
    assert "仅打包出售，合计 7000 元" in message
    assert "单件价格未拆分" in message
    assert "重点比价" not in message


def test_system_bundle_restriction_does_not_apply_to_separate_phone():
    items = classify(
        "台式整机3000元出售，不拆卖\niPhone 15 Pro 256GB 4800元出售",
        title="出整机和手机",
    )
    phone = next(i for i in items if i["category"] == "phone.smartphone")
    assert phone["price"] == 4800
    assert not phone["attributes"]["bundle"]


def test_exclusive_bundle_title_protects_every_item_without_inventing_total():
    items = classify(
        "iPhone 15 Pro 256GB 4800元出售\niPad Air 2500元出售\nApple Watch 800元出售",
        title="三件数码打包出，不拆卖",
    )
    assert len(items) == 3
    assert all(i["price"] is None and i["attributes"]["bundle_only"] for i in items)
    assert all(i["attributes"]["bundle_price"] is None for i in items)


@pytest.mark.parametrize("interrupt_at", [None, 3])
def test_nga_live_backlog_resumes_persisted_page_after_run_or_failure(tmp_path, interrupt_at):
    box, store = make_store(tmp_path)
    store.checkpoint("nga:live", {"last_id": 100})
    requested = []
    failed_once = False

    @contextmanager
    def fake_browser(_source):
        yield object()

    def page(_context, _source, number):
        nonlocal failed_once
        requested.append(number)
        if number == interrupt_at and not failed_once:
            failed_once = True
            raise RuntimeError("fixture interrupted page")
        identity = 200 - number * 10
        return [{"id": str(identity), "title": "出 iPhone 15 Pro",
                 "url": f"https://example.invalid/read.php?tid={identity}",
                 "body_complete": False}]

    with (
        patch("pylon.tasks.marketplace.poll.pipeline.enabled", return_value=True),
        patch("pylon.tasks.marketplace.poll.Store", side_effect=lambda: Store(Outbox(box.path))),
        patch("pylon.tasks.marketplace.poll.sources.browser", side_effect=fake_browser),
        patch("pylon.tasks.marketplace.poll.sources.list_page", side_effect=page),
        patch("pylon.tasks.marketplace.poll.sources.refill", return_value=[]),
        patch("pylon.tasks.marketplace.poll.pipeline.process", return_value=0),
        patch("pylon.tasks.marketplace.poll.events.result"),
    ):
        assert poll.main() == 0
        saved = Store(Outbox(box.path)).checkpoint("nga:live")
        assert saved["last_id"] == 100
        assert saved["newest_id"] == 190
        assert saved["next_page"] == (3 if interrupt_at else 9)
        boundary = len(requested)
        assert poll.main() == 0
        assert requested[boundary] == saved["next_page"]

    completed = Store(Outbox(box.path)).checkpoint("nga:live")
    assert completed["last_id"] == 190
    assert "next_page" not in completed
    with box.connect() as db:
        ids = {row[0] for row in db.execute("SELECT source_id FROM marketplace_posts")}
        assert ids == {str(number) for number in range(110, 200, 10)}
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


@pytest.mark.parametrize(("subject", "category"), [
    ("出 Claude 订阅会员 120元", "virtual.ai_membership"),
    ("出 Netflix 会员车位 120元", "virtual.video_membership"),
    ("出 Spotify 家庭会员 120元", "virtual.music_membership"),
    ("出山姆亲友卡 120元", "virtual.retail_membership"),
    ("出售云服务器 120元", "virtual.cloud_host"),
    ("出售 VPS 120元", "virtual.cloud_host"),
    ("出域名 120元", "virtual.domain"),
    ("出手机卡 120元", "virtual.sim"),
    ("出 eSIM 流量套餐 120元", "virtual.sim"),
    ("出话费权益 120元", "virtual.mobile_credit"),
    ("出机场流量套餐 120元", "virtual.proxy"),
    ("出 PT 邀请资格 120元", "virtual.pt_invite"),
])
def test_entitlement_types_can_be_selected_independently(subject, category):
    items = classify(subject, title=subject)
    assert len(items) == 1
    assert items[0]["category"] == category
    assert evaluate(items, category) == items
    assert evaluate(items, "virtual.service", "virtual.membership") == []


@pytest.mark.parametrize(("subject", "category"), [
    ("出 iPhone 17 eSIM 版 5000元", "phone.smartphone"),
    ("手机支持双SIM卡 500元出售", "phone.smartphone"),
    ("出售服务器整机，可运行VPS 3000元", "computer.server"),
    ("MacBook 笔记本 3000元出售\n可登录 Netflix 看视频", "computer.laptop"),
    ("山姆工学椅 500元出售", "home.furniture"),
    ("Netflix认证电视 1000元出售", "display.tv"),
])
def test_entitlement_mentions_do_not_replace_physical_sale_subject(subject, category):
    items = classify(subject, title=subject.splitlines()[0])
    assert len(items) == 1
    assert items[0]["category"] == category


def test_virtual_entitlement_never_uses_a_physical_device_quote():
    from pylon.tasks.marketplace.pipeline import _quote_matches

    items = classify("手机卡120元出售，兼容 iPhone 15 Pro 256GB", title="出手机卡")
    assert len(items) == 1
    assert evaluate(items, "phone.smartphone", "phone.smartphone", "phone.smartphone") == []
    item = items[0]
    assert item["category"] == "virtual.sim"
    assert item["price"] == 120
    assert evaluate([item], "virtual.sim") == [item]
    with patch("pylon.tasks.quotes.matching.select_match", return_value=_marketplace_match(120)) as matcher:
        assert _quote_matches([item], True, [{"sku_key": "fixture"}]) == []
    matcher.assert_not_called()


@pytest.mark.parametrize(("subject", "category"), [
    ("手机壳50元出售，适配 iPhone 15 Pro 256GB", "accessory.phone_case"),
    ("显卡水冷头400元出售，适配 RTX3080 显卡", "component.custom_loop"),
    ("相机5000元出售，支持 HDMI线", "camera.body"),
])
def test_compatibility_clause_cannot_create_another_sale(subject, category):
    items = classify(subject, title=subject)
    assert len(items) == 1
    assert items[0]["category"] == category


def test_compatible_accessory_explicitly_sold_separately_keeps_its_own_price():
    items = classify(
        "iPhone 15 Pro 256GB 4800元出售，兼容 iPhone 的手机壳50元另售",
        title="两件闲置出售",
    )
    assert [(i["category"], i["price"]) for i in items] == [
        ("phone.smartphone", 4800), ("accessory.phone_case", 50),
    ]
