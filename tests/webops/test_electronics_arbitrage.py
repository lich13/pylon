import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from pylon.tasks.quotes.arbitrage import (
    best_arbitrage_match,
    best_arbitrage_match_from_quotes,
    collect_arbitrage_messages,
    connect,
    discover_unknown_quote_candidates,
    format_contact_note,
    format_decoded_base64_contact_note,
    has_device_newness,
    is_excluded_device_deal,
    make_alert_message,
    make_bark_arbitrage_payload,
    money_candidates,
    normalize_match_text,
    normalize_text,
    parse_inline_quote_line,
    parse_quotes_from_copied_tsv,
    parse_quotes_from_open_et,
    parse_quotes_from_svg,
    quote_category,
    quote_matches_text,
    replace_quotes,
    sku_key,
)


CHIPHELL_TITLE = "[北京]华为 Pura 80 12+256 绿色 全新未拆封 3088元"
CHIPHELL_BODY = (
    "全新未拆封，华为Pura 80 12+256 绿色，公司以后送礼统一ipad，这最后一台 "
    "只接受北京本地面交，外地就不考虑啦，谢谢关注，qq同VX "
    "补充内容 (2026-5-6 22:30): 3楼预定，明日面交"
)
AVAILABLE_CHIPHELL_BODY = CHIPHELL_BODY.replace("3楼预定，明日面交", "可以面交")


class ElectronicsArbitrageTests(unittest.TestCase):
    def test_money_candidates_ignores_chiphell_supplement_timestamp(self):
        prices = money_candidates(f"{CHIPHELL_TITLE}\n{CHIPHELL_BODY}")
        self.assertIn(3088, prices)
        self.assertNotIn(2026, prices)
        self.assertNotIn(2025, money_candidates("苹果 iPad11 2025款 256G 蓝色 全新未拆封，价格 2600 元"))

    def test_chiphell_assume_sale_match_uses_actual_post_price(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entry = {
                "sku_key": sku_key("华为", "Pura 80 12+256", "绿"),
                "brand": "华为",
                "model_spec": "Pura 80 12+256",
                "color": "绿",
                "product_name": "华为 Pura 80 12+256 绿",
                "quote_price": 3499,
                "normalized": normalize_text("华为Pura 80 12+256绿"),
                "source_url": "test",
            }
            with connect(db_path) as conn:
                replace_quotes(conn, [entry])
                match = best_arbitrage_match(conn, CHIPHELL_TITLE, AVAILABLE_CHIPHELL_BODY, assume_sale=True)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match["sale_price"], 3088)
            self.assertEqual(match["profit"], 411)

    def test_reserved_listing_is_not_sent_as_arbitrage(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entry = {
                "sku_key": sku_key("华为", "Pura 80 12+256", "绿"), "brand": "华为",
                "model_spec": "Pura 80 12+256", "color": "绿",
                "product_name": "华为 Pura 80 12+256 绿", "quote_price": 3499,
                "normalized": normalize_text("华为Pura 80 12+256绿"), "source_url": "test",
            }
            with connect(db_path) as conn:
                replace_quotes(conn, [entry])
                self.assertIsNone(best_arbitrage_match(conn, CHIPHELL_TITLE, CHIPHELL_BODY, assume_sale=True))

    def test_collect_arbitrage_messages_matches_v2ex_new_hardware_post(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entry = {
                "sku_key": sku_key("苹果", "17PRO 256G", "蓝"),
                "brand": "苹果",
                "model_spec": "17PRO 256G",
                "color": "蓝",
                "product_name": "苹果 17PRO 256G 蓝",
                "quote_price": 7850,
                "normalized": normalize_text("苹果 17PRO 256G 蓝"),
                "source_url": "test",
            }
            with connect(db_path) as conn:
                replace_quotes(conn, [entry])

            with mock.patch("pylon.tasks.quotes.arbitrage.send_bark_arbitrage_burst_alert") as bark:
                messages = collect_arbitrage_messages(
                    "v2ex",
                    [
                        {
                            "id": 1217000,
                            "url": "https://www.v2ex.com/t/1217000",
                            "title": "【二手交易】北京出全新未拆 iPhone 17 Pro 256G 蓝 7800",
                            "body_excerpt": "全新未激活，明盘 7800，北京可面交。",
                            "body_full": "全新未激活，明盘 7800，北京可面交。",
                            "body_complete": True,
                        }
                    ],
                    db_path=db_path,
                )

        self.assertEqual(len(messages), 1)
        self.assertIn("## 重点比价预警", messages[0])
        self.assertIn("差价 ¥50", messages[0])
        bark.assert_called_once()

    def test_accessory_newness_does_not_make_used_device_arbitrage_eligible(self):
        title = "[郑州]iPhone Air 256g 黑色 4600元"
        body = "9成新,有轻微使用痕迹 自用iPhone Air黑色256g，2月激活的用了3个月，购于京东，箱说全，数据线全新未拆，4600出"
        self.assertFalse(has_device_newness(f"{title}\n{body}"))
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entry = {
                "sku_key": sku_key("苹果", "iPhone Air 256G", "黑"),
                "brand": "苹果",
                "model_spec": "iPhone Air 256G",
                "color": "黑",
                "product_name": "苹果 Air 256G 黑",
                "quote_price": 5550,
                "normalized": normalize_text("苹果 iPhone Air 256G 黑"),
                "source_url": "test",
            }
            with connect(db_path) as conn:
                replace_quotes(conn, [entry])
                match = best_arbitrage_match(conn, title, body, assume_sale=True)
            self.assertIsNone(match)

    def test_device_level_unactivated_newness_still_matches_arbitrage(self):
        title = "[北京]iPhone Air 256g 黑色 未激活 4600元"
        body = "京东购入，手机未激活未使用，包装已拆仅验机，数据线全新未拆，4600出"
        self.assertTrue(has_device_newness(f"{title}\n{body}"))
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entry = {
                "sku_key": sku_key("苹果", "iPhone Air 256G", "黑"),
                "brand": "苹果",
                "model_spec": "iPhone Air 256G",
                "color": "黑",
                "product_name": "苹果 Air 256G 黑",
                "quote_price": 5550,
                "normalized": normalize_text("苹果 iPhone Air 256G 黑"),
                "source_url": "test",
            }
            with connect(db_path) as conn:
                replace_quotes(conn, [entry])
                match = best_arbitrage_match(conn, title, body, assume_sale=True)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match["sale_price"], 4600)

    def test_excluded_device_deal_blocks_arbitrage_match(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entry = {
                "sku_key": sku_key("华为", "Pura 80 12+256", "绿"),
                "brand": "华为",
                "model_spec": "Pura 80 12+256",
                "color": "绿",
                "product_name": "华为 Pura 80 12+256 绿",
                "quote_price": 3499,
                "normalized": normalize_text("华为Pura 80 12+256绿"),
                "source_url": "test",
            }
            with connect(db_path) as conn:
                replace_quotes(conn, [entry])
                match = best_arbitrage_match(conn, "【出售】华为 Pura 80 12+256 绿色 全新官翻", "3088元", assume_sale=True)
            self.assertIsNone(match)
        self.assertTrue(is_excluded_device_deal("官翻机/监管机/有锁机都不要推"))
        self.assertTrue(is_excluded_device_deal("美版 iPhone 有锁版 低价"))
        self.assertFalse(is_excluded_device_deal("全新未拆封，无任何锁，非 MDM 设备"))

    def test_chiphell_arbitrage_push_includes_plaintext_contact(self):
        title = "【出售】华为 Pura 80 12+256 全新"
        body = "全新未拆封，微信：Alan__zhuang，3088元可出。"
        contact_note = format_contact_note(f"{title}\n{body}")
        self.assertIn("Alan__zhuang", contact_note)
        self.assertIn("（明文）", contact_note)
        match = {
            "product_name": "华为 Pura 80 12+256 绿",
            "quote_price": 3499,
            "sale_price": 3088,
            "profit": 411,
        }
        alert = make_alert_message(title, "https://www.chiphell.com/thread-123-1-1.html", match, contact_note=contact_note)
        self.assertIn(contact_note, alert)
        push_title, push_body = make_bark_arbitrage_payload(title, "https://www.chiphell.com/thread-123-1-1.html", match, contact_note=contact_note)
        self.assertIn("差价 ¥411", push_title)
        self.assertIn(contact_note, push_body)

    def test_base64_contact_decoding_still_works_for_other_marketplaces(self):
        text = "微信 QWxhbl9femh1YW5n"
        contact_note = format_decoded_base64_contact_note(text)
        self.assertIn("Alan__zhuang", contact_note)
        self.assertIn("Base64 解码", contact_note)

    def test_parse_kdocs_embedded_mate_xts_rows(self):
        svg = """
        <svg>
          <text x="10" y="10">华为Mate XTS非凡大师16+512</text>
          <text x="300" y="10">玄黑</text><text x="360" y="10">17700</text>
          <text x="430" y="10">皓白</text><text x="490" y="10">17000</text>
          <text x="560" y="10">槿紫</text><text x="620" y="10">17400</text>
          <text x="690" y="10">瑞红</text><text x="750" y="10">17450</text>
        </svg>
        """.encode()
        entries = parse_quotes_from_svg(svg)
        self.assertEqual(len(entries), 4)
        by_color = {entry["color"]: entry for entry in entries}
        self.assertEqual(by_color["玄黑"]["quote_price"], 17700)
        self.assertEqual(by_color["皓白"]["product_name"], "华为 Mate XTS非凡大师16+512 皓白")

    def test_mate_xts_quote_matches_chiphell_g_unit_config_and_requires_price(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("华为", "Mate XTS非凡大师16+512", "玄黑"),
                    "brand": "华为",
                    "model_spec": "Mate XTS非凡大师16+512",
                    "color": "玄黑",
                    "product_name": "华为 Mate XTS非凡大师16+512 玄黑",
                    "quote_price": 17700,
                    "normalized": normalize_text("华为Mate XTS非凡大师16+512玄黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("华为", "Mate XTS非凡大师16+512", "皓白"),
                    "brand": "华为",
                    "model_spec": "Mate XTS非凡大师16+512",
                    "color": "皓白",
                    "product_name": "华为 Mate XTS非凡大师16+512 皓白",
                    "quote_price": 17000,
                    "normalized": normalize_text("华为Mate XTS非凡大师16+512皓白"),
                    "source_url": "test",
                },
            ]
            title = "[合肥]华为三折手机 HUAWEI Mate Xts ULTIMATE DEDIGN 16G+512G"
            body_no_price = "华为HUAWEI Mate Xts ULTIMATE DEDIGN，非凡大师三折叠，16+512G，国行正品，原装未拆封。"
            body_with_price = body_no_price + " 明盘 16500 元。"
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                self.assertIsNone(best_arbitrage_match(conn, title, body_no_price, assume_sale=True))
                self.assertIsNone(best_arbitrage_match(conn, title, body_with_price, assume_sale=True))
                match = best_arbitrage_match(conn, title, body_with_price.replace("明盘", "皓白，明盘"), assume_sale=True)
            self.assertEqual(normalize_match_text("16G+512G"), "16+512")
            self.assertIn("非凡大师", normalize_match_text("HUAWEI Mate Xts ULTIMATE DEDIGN 16G+512G"))
            self.assertIsNotNone(match)
            assert match is not None
            self.assertEqual(match["quote_price"], 17000)
            self.assertEqual(match["sale_price"], 16500)
            self.assertEqual(match["profit"], 500)
    def test_model_aliases_cover_db_vendor_naming(self):
        self.assertIn("17PROMAX16+512", normalize_match_text("小米17PM 16GB+512GB"))
        self.assertIn("17PRO12+256", normalize_match_text("小米17P 12G+256G"))
        self.assertIn("17ULTRA16+1T", normalize_match_text("小米17U 16GB+1TB"))
        self.assertIn("17E256G", normalize_match_text("iPhone 17e 256GB"))
        self.assertIn("MATE80RS非凡大师20+512", normalize_match_text("HUAWEI Mate 80 RS ULTIMATE DESIGN 20GB+512GB"))
        self.assertIn("PURAXMAX典藏16+1T", normalize_match_text("HUAWEI Pura X Max 典藏版 16GB+1TB"))
        self.assertIn("1416+1T", normalize_match_text("小米14 16+1tTB"))
        self.assertIn("WATCHGT5PRO46MM", normalize_match_text("华为手表 GT5 Pro 46毫米"))
        self.assertIn("BAND9PRO", normalize_match_text("Xiaomi Smart Band 9 Pro"))
        self.assertIn("WATCHS1146MM", normalize_match_text("Apple Watch Series 11 46 mm"))
        self.assertIn("PADAIR11INCH256GCELLULAR", normalize_match_text("苹果平板 Air 11 256G 蜂窝版"))
        self.assertIn("BUDSPRO2USBC", normalize_match_text("苹果耳机 Pro 二代 Type-C"))
        self.assertIn("PENCILPRO", normalize_match_text("苹果触控笔 Pencil Pro"))

    def test_parse_kdocs_embedded_wearable_rows(self):
        svg = """
        <svg>
          <text x="10" y="10">华为WATCH GT5 Pro 46mm</text>
          <text x="300" y="10">黑</text><text x="360" y="10">1500</text>
          <text x="430" y="10">白</text><text x="490" y="10">1450</text>
        </svg>
        """.encode()
        entries = parse_quotes_from_svg(svg)
        self.assertEqual(len(entries), 2)
        by_color = {entry["color"]: entry for entry in entries}
        self.assertEqual(by_color["黑"]["model_spec"], "WATCH GT5 Pro 46mm")
        self.assertEqual(by_color["黑"]["quote_price"], 1500)

    def test_wearable_aliases_match_watch_and_band_quotes(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("苹果", "Watch Series 11 46mm GPS", "黑"),
                    "brand": "苹果",
                    "model_spec": "Watch Series 11 46mm GPS",
                    "color": "黑",
                    "product_name": "苹果 Watch Series 11 46mm GPS 黑",
                    "quote_price": 2800,
                    "normalized": normalize_text("苹果 Watch Series 11 46mm GPS 黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("华为", "WATCH GT5 Pro 46mm", "黑"),
                    "brand": "华为",
                    "model_spec": "WATCH GT5 Pro 46mm",
                    "color": "黑",
                    "product_name": "华为 WATCH GT5 Pro 46mm 黑",
                    "quote_price": 1500,
                    "normalized": normalize_text("华为 WATCH GT5 Pro 46mm 黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("小米", "Smart Band 9 Pro", "黑"),
                    "brand": "小米",
                    "model_spec": "Smart Band 9 Pro",
                    "color": "黑",
                    "product_name": "小米 Smart Band 9 Pro 黑",
                    "quote_price": 300,
                    "normalized": normalize_text("小米 Smart Band 9 Pro 黑"),
                    "source_url": "test",
                },
            ]
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                apple = best_arbitrage_match(conn, "苹果 Apple Watch S11 46毫米 GPS 黑色 全新未拆封", "明盘 2500 元", assume_sale=True)
                huawei = best_arbitrage_match(conn, "华为手表GT5 Pro 46毫米 黑 全新未拆封", "价格 1200 元", assume_sale=True)
                xiaomi = best_arbitrage_match(conn, "小米手环9 Pro 黑 全新未拆封", "卖 250 元", assume_sale=True)
            self.assertIsNotNone(apple)
            assert apple is not None
            self.assertIn("Watch Series 11", apple["product_name"])
            self.assertIsNotNone(huawei)
            assert huawei is not None
            self.assertIn("GT5 Pro", huawei["product_name"])
            self.assertIsNotNone(xiaomi)
            assert xiaomi is not None
            self.assertIn("Smart Band 9 Pro", xiaomi["product_name"])
            self.assertEqual(xiaomi["sale_price"], 250)

    def test_non_phone_aliases_match_tablet_and_earbud_quotes(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("苹果", "iPad Air 11 256G WiFi", "蓝"),
                    "brand": "苹果",
                    "model_spec": "iPad Air 11 256G WiFi",
                    "color": "蓝",
                    "product_name": "苹果 iPad Air 11 256G WiFi 蓝",
                    "quote_price": 4200,
                    "normalized": normalize_text("苹果 iPad Air 11 256G WiFi 蓝"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "AirPods Pro 2 USB-C", "白"),
                    "brand": "苹果",
                    "model_spec": "AirPods Pro 2 USB-C",
                    "color": "白",
                    "product_name": "苹果 AirPods Pro 2 USB-C 白",
                    "quote_price": 1100,
                    "normalized": normalize_text("苹果 AirPods Pro 2 USB-C 白"),
                    "source_url": "test",
                },
            ]
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                ipad = best_arbitrage_match(conn, "苹果平板 Air 11寸 256G WiFi 蓝色 全新未拆封", "明盘 3800 元", assume_sale=True)
                airpods = best_arbitrage_match(conn, "苹果耳机 Pro 二代 Type-C 白 全新未拆封", "价格 900 元", assume_sale=True)
            self.assertIsNotNone(ipad)
            assert ipad is not None
            self.assertIn("iPad Air", ipad["product_name"])
            self.assertIsNotNone(airpods)
            assert airpods is not None
            self.assertIn("AirPods Pro 2", airpods["product_name"])

    def test_tablet_variant_discrimination_for_matepad_and_ipad11(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("华为", "MatePad-11.5S寸2025款活力版12+256", "灰"),
                    "brand": "华为",
                    "model_spec": "MatePad-11.5S寸2025款活力版12+256",
                    "color": "灰",
                    "product_name": "华为 MatePad-11.5S寸2025款活力版12+256 灰",
                    "quote_price": 1780,
                    "normalized": normalize_text("华为MatePad-11.5S寸2025款活力版12+256灰"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("华为", "MatePad-11.5S寸2025款柔光版12+256", "灰银紫绿"),
                    "brand": "华为",
                    "model_spec": "MatePad-11.5S寸2025款柔光版12+256",
                    "color": "灰银紫绿",
                    "product_name": "华为 MatePad-11.5S寸2025款柔光版12+256 灰银紫绿",
                    "quote_price": 2300,
                    "normalized": normalize_text("华为MatePad-11.5S寸2025款柔光版12+256灰银紫绿"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "2025款iPad11 256", "蓝"),
                    "brand": "苹果",
                    "model_spec": "2025款iPad11 256",
                    "color": "蓝",
                    "product_name": "苹果 2025款iPad11 256 蓝",
                    "quote_price": 2980,
                    "normalized": normalize_text("苹果2025款iPad11 256蓝"),
                    "source_url": "test",
                },
            ]
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                matepad = best_arbitrage_match(conn, "华为 MatePad 11.5S 2025款活力版 12+256 灰 全新未拆封", "明盘 1600 元", assume_sale=True)
                ipad = best_arbitrage_match(conn, "苹果 iPad11 2025款 256G 蓝色 全新未拆封", "价格 2600 元", assume_sale=True)
            self.assertIsNotNone(matepad)
            assert matepad is not None
            self.assertIn("活力版", matepad["product_name"])
            self.assertEqual(matepad["quote_price"], 1780)
            self.assertIsNotNone(ipad)
            assert ipad is not None
            self.assertIn("iPad11", ipad["product_name"])

    def test_parse_kdocs_copied_tsv_apple_watch_earbuds_accessories(self):
        rows = [
            ["苹果配件系列(更新时间5月8号11:30)", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            ["AirPods4标准版(P63)  720", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            ["新品Max头戴耳机C口黑3150白3150蓝3150橙3150紫3150", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            ["24款二代pro手写笔(2D3）760教育700", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            ["11寸pro妙控键盘黑白-1900", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            ["苹果手表系列(更新时间5月8号10:30)", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            ["S11 42 GPS运动版官网2999元", "", "", "", "S11 46 GPS运动版官网3199元", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", ""],
            ["42-亮黑色-S/M", "U94", "2110", "", "46-亮黑色-S/M", "VG4", "2270", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "华为耳机系列(更新时间5月8号16:50）"],
            ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "华为Freebuds6无线蓝牙耳机黑500白500紫500金470"],
            ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "华为手表手环(更新时间5月7号16:00）"],
            ["", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "", "原封华为手表WATCH-FIT3灰色400黑色380粉色390绿色350白色390"],
        ]
        tsv = "\n".join("\t".join(row) for row in rows)
        entries = parse_quotes_from_copied_tsv(tsv, nonphone_only=False)
        names = [entry["product_name"] for entry in entries]
        cats = {quote_category(entry) for entry in entries}
        self.assertIn("earbuds", cats)
        self.assertIn("accessory", cats)
        self.assertIn("watch", cats)
        self.assertTrue(any("AirPods4" in name for name in names))
        self.assertTrue(any("Max头戴耳机" in name for name in names))
        self.assertTrue(any("手写笔" in name for name in names))
        self.assertTrue(any("妙控键盘" in name for name in names))
        self.assertTrue(any("S11 42 GPS运动版" in name and "42-亮黑色" in name for name in names))
        self.assertTrue(any("Freebuds6" in name for name in names))
        self.assertTrue(any("WATCH-FIT3" in name and "灰色" in name for name in names))
        self.assertFalse(any("官网" in name for name in names))
        self.assertFalse(any("Watch Ultra3 默认" in name for name in names))

    def test_nonphone_quote_matching_handles_alias_brand_and_material_words(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("苹果", "AirPods4标准版(P63)", "默认"),
                    "brand": "苹果",
                    "model_spec": "AirPods4标准版(P63)",
                    "color": "默认",
                    "product_name": "苹果 AirPods4标准版(P63) 默认",
                    "quote_price": 720,
                    "normalized": normalize_text("苹果AirPods4标准版(P63)默认"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "S11 42铁金属米兰尼斯 42-石板色", "42-石板色 A64"),
                    "brand": "苹果",
                    "model_spec": "S11 42铁金属米兰尼斯 42-石板色",
                    "color": "42-石板色 A64",
                    "product_name": "苹果 S11 42铁金属米兰尼斯 42-石板色 42-石板色 A64",
                    "quote_price": 4750,
                    "normalized": normalize_text("苹果S1142铁金属米兰尼斯42石板色42石板色A64"),
                    "source_url": "test",
                },
            ]
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                airpods = best_arbitrage_match(conn, "AirPods4标准版 P63 全新未拆封", "600元出", assume_sale=True)
                watch = best_arbitrage_match(conn, "Apple Watch S11 42mm 铁金属米兰尼斯 石板色 全新未拆封", "4500元出", assume_sale=True)
                row = conn.execute("select * from quotes where model_spec like 'S11 42铁金属%'").fetchone()
                assert row is not None
                watch_self_match = quote_matches_text(row, normalize_match_text(f"{row['brand']} {row['product_name']} {row['model_spec']} {row['color']}"))
            self.assertIsNotNone(airpods)
            assert airpods is not None
            self.assertEqual(airpods["product_name"], "苹果 AirPods4标准版(P63) 默认")
            self.assertIsNotNone(watch)
            assert watch is not None
            self.assertIn("石板色", watch["product_name"])
            self.assertTrue(watch_self_match)

    def test_insta360_short_x_models_do_not_cross_match_on_bare_digits(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("影石", "x3 标准", "标准"),
                    "brand": "影石",
                    "model_spec": "x3 标准",
                    "color": "标准",
                    "product_name": "影石 x3 标准",
                    "quote_price": 1270,
                    "normalized": normalize_text("影石x3标准"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("影石", "x5", "白"),
                    "brand": "影石",
                    "model_spec": "x5",
                    "color": "白",
                    "product_name": "影石 x5 白",
                    "quote_price": 2520,
                    "normalized": normalize_text("影石x5白"),
                    "source_url": "test",
                },
            ]
            title = "[绍兴]影石Insta360 X3 1350元"
            body = (
                "价格：1400 元；成色：全新。上个月订了名爵MG4，赠送了影石X3，"
                "昨天刚到货，全新未拆封，还赠送配件吸盘支架和64g内存卡。"
            )
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                match = best_arbitrage_match(conn, title, body, assume_sale=True)
                x5 = conn.execute("select * from quotes where product_name='影石 x5 白'").fetchone()
                assert x5 is not None
                x5_self_match = quote_matches_text(x5, normalize_match_text("影石 Insta360 X5 白 全新未拆封 1400元"))
            self.assertIsNone(match)
            self.assertTrue(x5_self_match)

    def test_model_anchors_prevent_cross_model_arbitrage_matches(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("华为", "Mate XT非凡大师16+512", "黑"),
                    "brand": "华为",
                    "model_spec": "Mate XT非凡大师16+512",
                    "color": "黑",
                    "product_name": "华为 Mate XT非凡大师16+512 黑",
                    "quote_price": 17800,
                    "normalized": normalize_text("华为Mate XT非凡大师16+512黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("华为", "Mate XTS非凡大师16+512", "黑"),
                    "brand": "华为",
                    "model_spec": "Mate XTS非凡大师16+512",
                    "color": "黑",
                    "product_name": "华为 Mate XTS非凡大师16+512 黑",
                    "quote_price": 17000,
                    "normalized": normalize_text("华为Mate XTS非凡大师16+512黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("华为", "MATEX5 12+256", "白"),
                    "brand": "华为",
                    "model_spec": "MATEX5 12+256",
                    "color": "白",
                    "product_name": "华为 MATEX5 12+256 白",
                    "quote_price": 7800,
                    "normalized": normalize_text("华为MATEX5 12+256白"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("华为", "MATEX5 12+512", "白"),
                    "brand": "华为",
                    "model_spec": "MATEX5 12+512",
                    "color": "白",
                    "product_name": "华为 MATEX5 12+512 白",
                    "quote_price": 8250,
                    "normalized": normalize_text("华为MATEX5 12+512白"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "16PLUS 512", "白"),
                    "brand": "苹果",
                    "model_spec": "16PLUS 512",
                    "color": "白",
                    "product_name": "苹果 16PLUS 512 白",
                    "quote_price": 6000,
                    "normalized": normalize_text("苹果16PLUS512白"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "16PRO 512", "白"),
                    "brand": "苹果",
                    "model_spec": "16PRO 512",
                    "color": "白",
                    "product_name": "苹果 16PRO 512 白",
                    "quote_price": 7200,
                    "normalized": normalize_text("苹果16PRO512白"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("三星", "S25+ 12+512", "银"),
                    "brand": "三星",
                    "model_spec": "S25+ 12+512",
                    "color": "银",
                    "product_name": "三星 S25+ 12+512 银",
                    "quote_price": 5800,
                    "normalized": normalize_text("三星S25+12+512银"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("三星", "S25U 12+512", "银"),
                    "brand": "三星",
                    "model_spec": "S25U 12+512",
                    "color": "银",
                    "product_name": "三星 S25U 12+512 银",
                    "quote_price": 6500,
                    "normalized": normalize_text("三星S25U12+512银"),
                    "source_url": "test",
                },
            ]
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                xts = best_arbitrage_match(
                    conn,
                    "华为 Mate XTS ULTIMATE DESIGN 16G+512G 黑 全新未拆封",
                    "明盘 16500 元",
                    assume_sale=True,
                )
                matex5 = best_arbitrage_match(conn, "华为 Mate X5 12+256 白 全新未拆封", "7600元出", assume_sale=True)
                plus = best_arbitrage_match(conn, "苹果 iPhone 16 Plus 512 白 全新未拆封", "5500元出", assume_sale=True)
                s25u = best_arbitrage_match(conn, "Samsung 三星 S25U 12+512 银 全新未拆封", "6100元出", assume_sale=True)
            self.assertIsNotNone(xts)
            assert xts is not None
            self.assertEqual(xts["product_name"], "华为 Mate XTS非凡大师16+512 黑")
            self.assertIsNotNone(matex5)
            assert matex5 is not None
            self.assertEqual(matex5["product_name"], "华为 MATEX5 12+256 白")
            self.assertIsNotNone(plus)
            assert plus is not None
            self.assertEqual(plus["product_name"], "苹果 16PLUS 512 白")
            self.assertIsNotNone(s25u)
            assert s25u is not None
            self.assertEqual(s25u["product_name"], "三星 S25U 12+512 银")

    def test_parse_kdocs_open_et_nonphone_tablet_rows(self):
        line = "华为平板原封MatePad-11.5S寸2025款活力版12+256灰1780银紫1800"
        entries = parse_inline_quote_line(line, nonphone_only=True)
        self.assertEqual(len(entries), 2)
        by_color = {entry["color"]: entry for entry in entries}
        self.assertEqual(by_color["灰"]["model_spec"], "MatePad-11.5S寸2025款活力版12+256")
        self.assertEqual(by_color["灰"]["quote_price"], 1780)
        self.assertEqual(by_color["银紫"]["quote_price"], 1800)

        ipad_line = "苹果2025款iPad11 256白2960蓝2980粉2960黄2900"
        ipad_entries = parse_inline_quote_line(ipad_line, nonphone_only=True)
        self.assertEqual(len(ipad_entries), 4)
        self.assertEqual({entry["color"] for entry in ipad_entries}, {"白", "蓝", "粉", "黄"})
        self.assertEqual({entry["quote_price"] for entry in ipad_entries}, {2960, 2980, 2900})

        # Simulate the UTF-16LE `/open/et` workbook stream. The parser should
        # pick the non-phone quote and ignore unrelated phone lines when asked.
        blob = (line + "\x00ignored" + "荣耀GT 12+256G黑绿白1640").encode("utf-16le")
        workbook_entries = parse_quotes_from_open_et(blob, nonphone_only=True)
        self.assertEqual(len(workbook_entries), 2)
        self.assertTrue(all("MatePad" in entry["model_spec"] for entry in workbook_entries))

    def test_dynamic_nonphone_categories_and_unknown_candidates(self):
        laptop_line = "苹果MacBook Air 13 M4 16+512黑7999银8050"
        laptop_entries = parse_inline_quote_line(laptop_line, nonphone_only=True)
        self.assertEqual(len(laptop_entries), 2)
        self.assertEqual({quote_category(entry) for entry in laptop_entries}, {"laptop"})
        self.assertEqual({entry["quote_price"] for entry in laptop_entries}, {7999, 8050})

        drone_line = "大疆无人机Mini5Pro灰3500白3600"
        drone_entries = parse_inline_quote_line(drone_line, nonphone_only=True)
        self.assertEqual({quote_category(entry) for entry in drone_entries}, {"drone"})
        unknown_line = "AR眼镜VisionGlass黑3500白3600"
        blob = (laptop_line + "\x00" + drone_line + "\x00" + unknown_line).encode("utf-16le")
        candidates = discover_unknown_quote_candidates(blob)
        self.assertTrue(any("AR眼镜" in candidate["line"] for candidate in candidates))
        self.assertFalse(any("MacBook" in candidate["line"] for candidate in candidates))
        self.assertFalse(any("大疆无人机" in candidate["line"] for candidate in candidates))

    def test_real_false_positive_shapes_are_rejected(self):
        cases = [
            (
                "苹果",
                "17 MAX-256G",
                "橙",
                8500,
                "[杭州]苹果Iphone 17 pro max 256G 星宇橙 7400靓机",
                "10月3日过保，使用全程带壳贴膜，外观充新，电池95%，原盒原线未使用。",
            ),
            (
                "苹果",
                "S11 46铁金属米兰尼斯",
                "默认",
                4950,
                "支持快充的美版 Apple Watch S11 GPS 42mm",
                "几乎全新，1900元出。",
            ),
            (
                "华为",
                "Mate XT2 16+1T",
                "玄黑",
                27100,
                "杭州 华为 MATE XT 一代 4.2系统 带care",
                "全新未拆，明盘 11888 元。",
            ),
        ]
        for brand, spec, color, price, title, body in cases:
            with self.subTest(title=title):
                quote = {
                    "sku_key": sku_key(brand, spec, color), "brand": brand,
                    "model_spec": spec, "color": color, "product_name": f"{brand} {spec} {color}",
                    "quote_price": price, "normalized": normalize_text(f"{brand}{spec}{color}"), "source_url": "test",
                }
                self.assertIsNone(best_arbitrage_match_from_quotes([quote], title, body, assume_sale=True))

    def test_multi_item_price_does_not_use_other_item_lowest_price(self):
        quote = {
            "sku_key": sku_key("苹果", "17PRO 256G", "蓝"), "brand": "苹果",
            "model_spec": "17PRO 256G", "color": "蓝", "product_name": "苹果 17PRO 256G 蓝",
            "quote_price": 7850, "normalized": normalize_text("苹果17PRO256G蓝"), "source_url": "test",
        }
        title = "出全新未拆 iPhone 17 Pro 256G 蓝色"
        body = "明盘7800元。另有全新小米手机一台，价格6000元。"
        match = best_arbitrage_match_from_quotes([quote], title, body, assume_sale=True)
        self.assertIsNotNone(match)
        self.assertEqual(match["sale_price"], 7800)

    def test_zero_price_unknown_kdocs_line_is_silent(self):
        blob = "华为MATEX7 1TB 典藏红手写笔套装00000 白00000".encode("utf-16le")
        self.assertEqual(discover_unknown_quote_candidates(blob), [])

    def test_replace_quotes_preserves_then_confirms_removed_categories(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            phone = {
                "sku_key": sku_key("苹果", "17 256G", "黑"),
                "brand": "苹果",
                "model_spec": "17 256G",
                "color": "黑",
                "product_name": "苹果 17 256G 黑",
                "quote_price": 5480,
                "normalized": normalize_text("苹果17 256G黑"),
                "source_url": "test",
            }
            tablet = {
                "sku_key": sku_key("华为", "MatePad-11.5S寸2025款活力版12+256", "灰"),
                "brand": "华为",
                "model_spec": "MatePad-11.5S寸2025款活力版12+256",
                "color": "灰",
                "product_name": "华为 MatePad-11.5S寸2025款活力版12+256 灰",
                "quote_price": 1780,
                "normalized": normalize_text("华为MatePad-11.5S寸2025款活力版12+256灰"),
                "source_url": "test",
            }
            with connect(db_path) as conn:
                replace_quotes(conn, [phone, tablet])
                first = replace_quotes(conn, [phone], protect_removed_categories=True, category_absence_confirmations=2)
                rows_after_first = list(conn.execute("SELECT * FROM quotes"))
                self.assertEqual(len(rows_after_first), 2)
                self.assertEqual(first["preserved_missing"], 1)
                self.assertEqual(first["pending_removed_categories"], {"tablet": 1})
                second = replace_quotes(conn, [phone], protect_removed_categories=True, category_absence_confirmations=2)
                rows_after_second = list(conn.execute("SELECT * FROM quotes"))
                self.assertEqual(len(rows_after_second), 1)
                self.assertEqual(second["confirmed_removed_categories"], ["tablet"])

    def test_variant_discrimination_for_apple_17_family(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("苹果", "17 256G", "黑"),
                    "brand": "苹果",
                    "model_spec": "17 256G",
                    "color": "黑",
                    "product_name": "苹果 17 256G 黑",
                    "quote_price": 5500,
                    "normalized": normalize_text("苹果17 256G黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "17E 256G", "黑"),
                    "brand": "苹果",
                    "model_spec": "17E 256G",
                    "color": "黑",
                    "product_name": "苹果 17E 256G 黑",
                    "quote_price": 3800,
                    "normalized": normalize_text("苹果17E 256G黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "17PRO 512G", "橙"),
                    "brand": "苹果",
                    "model_spec": "17PRO 512G",
                    "color": "橙",
                    "product_name": "苹果 17PRO 512G 橙",
                    "quote_price": 9700,
                    "normalized": normalize_text("苹果17PRO 512G橙"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("苹果", "17 MAX - 512G", "橙"),
                    "brand": "苹果",
                    "model_spec": "17 MAX - 512G",
                    "color": "橙",
                    "product_name": "苹果 17 MAX - 512G 橙",
                    "quote_price": 10750,
                    "normalized": normalize_text("苹果17 MAX 512G橙"),
                    "source_url": "test",
                },
            ]
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                m17e = best_arbitrage_match(conn, "苹果 iPhone 17e 256GB 黑色 全新", "明盘 3600 元", assume_sale=True)
                m17pm = best_arbitrage_match(conn, "苹果 iPhone 17PM 512GB 橙色 全新", "明盘 9000 元", assume_sale=True)
            self.assertIsNotNone(m17e)
            assert m17e is not None
            self.assertIn("17E", m17e["product_name"])
            self.assertIsNotNone(m17pm)
            assert m17pm is not None
            self.assertIn("MAX", m17pm["product_name"])

    def test_variant_discrimination_for_huawei_mate80_pro_max(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "quotes.sqlite3"
            entries = [
                {
                    "sku_key": sku_key("华为", "Mate80Pro 16+512", "黑"),
                    "brand": "华为",
                    "model_spec": "Mate80Pro 16+512",
                    "color": "黑",
                    "product_name": "华为 Mate80Pro 16+512 黑",
                    "quote_price": 5950,
                    "normalized": normalize_text("华为Mate80Pro16+512黑"),
                    "source_url": "test",
                },
                {
                    "sku_key": sku_key("华为", "Mate80Pro MAX 16+512", "黑"),
                    "brand": "华为",
                    "model_spec": "Mate80Pro MAX 16+512",
                    "color": "黑",
                    "product_name": "华为 Mate80Pro MAX 16+512 黑",
                    "quote_price": 6850,
                    "normalized": normalize_text("华为Mate80ProMAX16+512黑"),
                    "source_url": "test",
                },
            ]
            with connect(db_path) as conn:
                replace_quotes(conn, entries)
                match = best_arbitrage_match(conn, "华为 Mate80 Pro Max 16GB+512GB 黑 全新", "明盘 6500 元", assume_sale=True)
            self.assertIsNotNone(match)
            assert match is not None
            self.assertIn("MAX", match["product_name"])



if __name__ == "__main__":
    unittest.main()
