"""Semantic regressions for the v12 marketplace classifier and source boundary."""

from pathlib import Path

import pytest

from pylon.outbox import Outbox
from pylon.tasks.forums import v2ex
from pylon.tasks.marketplace import classifier, enrich, policy, sources
from pylon.tasks.marketplace.store import Store


FIXTURES = Path(__file__).parent / "fixtures" / "marketplace_v12"
SALE_POLICY = {"enabled": True, "version": 12, "groups": ["phone", "computer", "wearable"], "categories": []}
CAMERA_POLICY = {"enabled": True, "version": 12, "groups": ["camera"], "categories": []}


def _post(title: str, body: str, source_id: str = "marketplace-v12") -> dict:
    return {
        "source": "v2ex",
        "source_id": source_id,
        "title": title,
        "body": body,
        "body_complete": True,
        "history": 0,
        "url": f"https://example.invalid/t/{source_id}",
    }


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_iqoo_z9_turbo_wanted_is_a_phone_and_never_a_camera():
    title = "求购 iQOO Z9 Turbo 手机"
    items = classifier.classify_listing(title, "预算 2200 元，成色不限。", "v2ex", True)

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "phone.smartphone"
    assert item["status"] == "buy"
    assert item["attributes"]["intent"] == "wanted"
    assert policy.evaluate(items, CAMERA_POLICY) == []


def test_hp_705_g5_dm_r5_desktop_sale_is_not_a_camera():
    title = "出 惠普 705 G5 DM 含 R5 PRO 3400G 整机"
    items = classifier.classify_listing(title, "尾价 600 元，可邮寄。", "v2ex", True)

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "computer.desktop"
    assert item["price"] == 600
    assert policy.evaluate(items, CAMERA_POLICY) == []


def test_ddr5_frequency_is_not_the_tail_price():
    title = "出 DDR5 5600 16Gx2 内存条"
    items = classifier.classify_listing(title, "尾价：2700 元，可邮寄。", "v2ex", True)

    assert len(items) == 1
    item = items[0]
    assert item["category"] == "component.memory"
    assert item["price"] == 2700
    assert "2700" in item["attributes"]["price_evidence"]
    assert "5600" not in item["attributes"]["price_evidence"]


@pytest.mark.parametrize(
    ("title", "category"),
    [
        ("出 电话手表 Z8", "wearable.child_watch"),
        ("出 儿童智能手表 小天才 Z8", "wearable.child_watch"),
        ("出 智能手表 Galaxy Watch 6", "wearable.watch"),
        ("出 Apple Watch SE", "wearable.watch"),
    ],
)
def test_child_and_regular_smart_watches_keep_separate_categories(title, category):
    items = classifier.classify_listing(title, "售价 300 元，可邮寄。", "v2ex", True)

    assert len(items) == 1
    assert items[0]["category"] == category
    assert items[0]["category"] in policy.effective({**SALE_POLICY, "groups": ["wearable"]})


@pytest.mark.parametrize(
    ("title", "body", "category", "status"),
    [
        ("澜星 Q8 Ultra 已售", "该物件已售，勿拍。", "unknown", "sold"),
        ("请教 澜星 Q8 Ultra 怎么购买", "只了解行情，暂无交易计划。", "unknown", "uncertain"),
        ("求助 iPhone 15 Pro 估价", "只想了解回收行情，不出售也不求购。", "phone.smartphone", "uncertain"),
        ("估价 iPhone 15 Pro", "请教目前行情，暂无交易计划。", "phone.smartphone", "uncertain"),
    ],
)
def test_closed_or_inquiry_item_does_not_enter_review(title, body, category, status):
    post = _post(title, body, source_id="review-boundary")
    items = classifier.classify_listing(title, body, "v2ex", True)

    assert len(items) == 1
    assert items[0]["category"] == category
    assert items[0]["status"] == status
    decision, reasons = policy.decision(items[0], SALE_POLICY)
    assert decision == "exclude"
    assert "不是在售商品" in reasons
    assert policy.evaluate(items, SALE_POLICY) == []
    assert not enrich.needs_analysis(post, items)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("出 A7CR", "unknown"),
        ("出 A7M4", "unknown"),
        ("出 Sony A7CR 相机", "camera.body"),
        ("出 佳能 A7M4 相机", "camera.body"),
    ],
)
def test_bare_camera_model_needs_camera_or_brand_evidence(title, expected):
    items = classifier.classify_listing(title, "售价 5000 元，可邮寄。", "v2ex", True)

    assert len(items) == 1
    assert items[0]["category"] == expected


def test_product_keywords_and_virtual_origin_are_source_bound_fields():
    physical = classifier.classify_listing(
        "出 iPhone 15 Pro 256G",
        "售价 4500 元，可邮寄。",
        "v2ex",
        True,
    )[0]
    assert physical["attributes"]["facts"]["product"] == "出 iPhone 15 Pro 256G"
    assert "iPhone 15 Pro 256G" in physical["attributes"]["facts"]["keywords"]
    assert "origin" not in physical["attributes"]["facts"]

    entitlement = classifier.classify_listing(
        "出 88VIP 赠送 Apple Music 会员",
        "售价 20 元，可转让。",
        "v2ex",
        True,
    )[0]
    assert entitlement["category"] == "virtual.music_membership"
    assert entitlement["attributes"]["facts"]["product"] == "出 88VIP 赠送 Apple Music 会员"
    assert "Apple Music" in entitlement["attributes"]["facts"]["keywords"]
    assert entitlement["attributes"]["facts"]["origin"] == ["88VIP"]


def test_split_88vip_entitlement_is_excluded_but_independent_spotify_matches():
    items = classifier.classify_listing(
        "出 88VIP 赠送 Apple Music 会员",
        "售价 20 元，可转让。\n另外出售 Spotify会员20元",
        "v2ex",
        True,
    )

    apple_music = next(item for item in items if "Apple Music" in item["text"])
    spotify = next(item for item in items if "Spotify" in item["text"])
    assert apple_music["attributes"]["facts"]["origin"] == ["88VIP"]
    assert spotify["attributes"]["facts"].get("origin") in (None, [])

    policy_with_origin_exclusion = {
        **SALE_POLICY,
        "groups": [],
        "categories": ["virtual.music_membership"],
        "filters": {"virtual": {"origin": {"exclude": ["88VIP"]}}},
    }
    assert policy.evaluate(items, policy_with_origin_exclusion) == [spotify]


def test_gift_clause_does_not_replace_the_phone_subject_or_make_it_uncertain():
    item = classifier.classify_listing(
        "iPhone 15 PM 512g 4600元，送壳膜", "", "v2ex", True,
    )[0]

    assert item["category"] == "phone.smartphone"
    assert item["price"] == 4600
    assert item["status"] == "available"


def test_seller_price_wins_over_jd_reference_price_for_a_mouse():
    item = classifier.classify_listing(
        "出 ATK 鼠标，JD售价199元，卖价150元", "", "v2ex", True,
    )[0]

    assert item["category"] == "input.mouse"
    assert item["price"] == 150


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DDR5 32GB 6000", None),
        ("DDR5 32GB 6000 300", 300),
        ("DDR5 5600 16Gx2 2700", 2700),
        ("DDR5 32GB 2700", 2700),
        ("出 ATK 鼠标，JD售价199元，卖价150元", 150),
    ],
)
def test_v12_price_roles_keep_specs_and_reference_prices_out_of_seller_price(text, expected):
    assert classifier.asking_price(text) == expected


def test_conditional_upgrade_enters_analysis_without_a_current_future_price():
    title = "出 iCloud 当前方案"
    body = "如果都同意可订阅 Apple Music，价格会涨15元/月"
    items = classifier.classify_listing(title, body, "v2ex", True)
    post = _post(title, body, source_id="conditional-upgrade")

    assert items
    assert items[0]["attributes"]["conditional_terms"] is True
    assert items[0]["price"] is None
    assert enrich.needs_analysis(post, items)


def test_forum_r_price_suffix_is_yuan():
    assert classifier.asking_price("出 Spotify会员 5r") == 5


def test_virtual_membership_keeps_monthly_billing_unit():
    item = classifier.classify_listing(
        "转让Spotify会员，每月5元", "", "v2ex", True,
    )[0]

    assert item["category"] == "virtual.music_membership"
    assert item["price"] == 5
    assert item["attributes"]["billing_unit"] == "月"


def test_v2ex_sidebar_markers_downgrade_old_body_to_untrusted_content():
    body = "exe-hub skill.md ed25519 Solana $V2EX；售价 5 元。"
    item = classifier.classify_listing("ChatGPT 拼车", body, "v2ex", True)[0]

    assert item["attributes"]["body_complete"] is False
    assert item["price"] is None


def test_one_item_with_conflicting_sale_prices_enters_analysis_queue():
    title = "出 iPhone 15 Pro"
    body = "售价4500元，卖价4300元，可邮寄。"
    post = _post(title, body, source_id="1207")
    item = classifier.classify_listing(title, body, "v2ex", True)[0]

    assert item["attributes"]["price_status"] == "ambiguous"
    assert enrich.needs_analysis(post, [item])


def test_v2ex_topic_document_ignores_home_sidebar_and_preserves_main_provenance():
    html = _read_fixture("v2ex_topic_with_sidebar.html")
    document = v2ex.extract_topic_document(
        html,
        expected_url="https://www.v2ex.com/t/1201",
        final_url="https://www.v2ex.com/t/1201#Main",
    )

    assert document["title"] == "合成交易帖"
    assert "主楼商品：iPhone 15 Pro 256G，4500元出售，可邮寄。" in document["body_full"]
    assert "主楼补充：北京自提优先。" in document["body_full"]
    assert "侧栏假商品" not in document["body_full"]
    assert document["body_provenance"] == {
        "version": 1,
        "topic_id": "1201",
        "container": "Main",
        "identity_verified": True,
    }


def test_v2ex_topic_document_requires_the_main_topic_container():
    html = _read_fixture("v2ex_topic_with_sidebar.html").replace('id="Main"', 'class="not-main"')

    with pytest.raises(ValueError, match="topic_identity_missing"):
        v2ex.extract_topic_document(
            html,
            expected_url="https://www.v2ex.com/t/1201",
            final_url="https://www.v2ex.com/t/1201#Main",
        )


def test_v2ex_detail_rejects_a_redirected_home_page_as_content_unavailable():
    html = _read_fixture("v2ex_home_sidebar.html")

    class Response:
        status_code = 200
        url = "https://www.v2ex.com/"
        text = html

        def raise_for_status(self):
            return None

    class Context:
        def get(self, _url):
            return Response()

    post = {
        "id": "1202",
        "title": "合成交易帖",
        "url": "https://www.v2ex.com/t/1202",
        "body_full": "",
        "body_complete": False,
    }

    with pytest.raises(sources.ContentUnavailable, match="unexpected_topic_redirect"):
        sources.detail(Context(), "v2ex", post)


def test_v2ex_topic_document_requires_main_heading_identity():
    html = _read_fixture("v2ex_topic_with_sidebar.html").replace(
        "      <h1>合成交易帖</h1>\n", ""
    )

    class Response:
        status_code = 200
        url = "https://www.v2ex.com/t/1201"
        text = html

        def raise_for_status(self):
            return None

    class Context:
        def get(self, _url):
            return Response()

    with pytest.raises(sources.ContentUnavailable, match="topic_identity_missing"):
        sources.detail(
            Context(),
            "v2ex",
            {
                "id": "1201",
                "title": "合成交易帖",
                "url": "https://www.v2ex.com/t/1201",
                "body_full": "",
                "body_complete": False,
            },
        )


@pytest.mark.parametrize(
    ("tags", "title", "expected"),
    [
        (["二手交易"], "iPhone 15 Pro", "secondary_market"),
        ([], "出售 iPhone 15 Pro", "secondary_market"),
        (["拼车"], "ChatGPT 拼车", "secondary_market"),
    ],
)
def test_v2ex_explicit_trade_and_shared_ride_use_secondary_market_route(tags, title, expected):
    assert v2ex.classify(tags, title, title) == expected


def test_mixed_sale_keeps_verified_phone_when_unrelated_unknown_row_fails(tmp_path):
    post = _post(
        "多件闲置出售",
        "类型 | 名称 | 价格 |\n"
        "手机 | iPhone 15 Pro | 4500元 |\n"
        "其他 | 物件甲 | 80元 |\n"
        "全国可邮寄",
        source_id="1203",
    )
    answer = {
        "items": [
            {
                "name": "iPhone 15 Pro",
                "category": "phone.smartphone",
                "start_line": 2,
                "end_line": 2,
                "price": 4500,
                "price_line": 2,
                "status": "available",
                "link_ids": [],
            },
            {
                "name": "物件甲",
                "category": "unknown",
                "start_line": 3,
                "end_line": 3,
                # Deliberately disagree with the source price: partial
                # validation must isolate this row instead of borrowing its
                # value or blocking the verified phone.
                "price": 81,
                "price_line": 3,
                "status": "available",
                "link_ids": [],
            },
        ],
        "common_lines": [4],
        "shared_link_ids": [],
    }
    verified = enrich.validate(answer, post, partial=True)
    assert {item["category"] for item in verified} == {"phone.smartphone", "unknown"}
    failed = next(item for item in verified if item["category"] == "unknown")
    assert failed["price"] is None
    assert "analysis_error" in failed["attributes"]

    store = Store(Outbox(tmp_path / "marketplace-v12.sqlite3"))
    store.ingest(
        "v2ex",
        [
            {
                "id": post["source_id"],
                "title": post["title"],
                "url": post["url"],
                "body_full": post["body"],
                "body_complete": True,
            }
        ],
    )
    with store.box.connect() as db:
        stored = dict(
            db.execute(
                "SELECT * FROM marketplace_posts WHERE source=? AND source_id=?",
                ("v2ex", post["source_id"]),
            ).fetchone()
        )
        safe = enrich.prepare(db, stored, verified)

    assert safe is not None
    assert [item["category"] for item in safe] == ["phone.smartphone", "unknown"]
    assert policy.evaluate(safe, SALE_POLICY) == [safe[0]]
    assert safe[0]["price"] == 4500


def test_seller_closing_update_marks_all_bags_sold_without_grok():
    title = "出女包"
    body = "LV女包 1600元出售\nYSL女包 1000元出售\n楼主补充：没了没了"
    post = _post(title, body, source_id="closed-bags")

    items = classifier.classify_listing(title, body, "v2ex", True)

    assert [(item["category"], item["price"], item["status"]) for item in items] == [
        ("life.bag", 1600, "sold"),
        ("life.bag", 1000, "sold"),
    ]
    assert not enrich.needs_analysis(post, items)


@pytest.mark.parametrize(
    "body",
    [
        "LV女包 1600元出售\nYSL女包 1000元出售\n楼主补充：盒子没了",
        "LV女包 1600元已售\nYSL女包 1000元出售",
        "LV女包 1600元出售\nYSL女包 1000元出售\n楼主补充：没了没了\n后续重新开卖：LV女包 1800元出售",
    ],
)
def test_partial_or_reopened_bag_sales_are_not_closed_as_a_whole(body):
    items = classifier.classify_listing("出女包", body, "v2ex", True)

    assert items
    assert any(item["status"] == "available" for item in items)


def test_closed_post_with_sold_gift_does_not_reenter_analysis():
    title = "出 iPhone 15"
    body = "iPhone 15 3000元出售\n附赠原装充电器已售\n楼主补充：没了没了"
    post = _post(title, body, source_id="closed-gift")
    items = classifier.classify_listing(title, body, "v2ex", True)

    assert len(items) == 1
    assert items[0]["status"] == "sold"
    assert not enrich.needs_analysis(post, items)


def test_chiphell_attachment_controls_are_excluded_from_source_and_price_evidence():
    body = (
        "1.jpg (321.00 KB, 下载次数:\n"
        "0)\n下载附件\n2026-10-8 12:30 上传\n"
        "1) LV女包 1600元出售\n"
        "2) YSL女包 1000元出售"
    )
    post = {
        "source": "chiphell",
        "title": "出女包",
        "body": body,
        "body_complete": True,
    }

    assert enrich.source_lines(post) == [
        "出女包",
        "1) LV女包 1600元出售",
        "2) YSL女包 1000元出售",
    ]
    items = classifier.classify_listing(post["title"], body, post["source"], True)
    assert [(item["price"], item["status"]) for item in items] == [(1600, "available"), (1000, "available")]
    assert all(
        not any(marker in item["attributes"]["price_evidence"] for marker in ("下载附件", "上传", "0)"))
        for item in items
    )


@pytest.mark.parametrize("source", ["v2ex", "nga", "linuxdo"])
def test_discuz_attachment_looking_text_is_preserved_for_non_chiphell(source):
    body = "1.jpg (321.00 KB, 下载次数:\n0)\n下载附件\n2026-10-8 12:30 上传"

    assert classifier._sale_body(body, source) == body
