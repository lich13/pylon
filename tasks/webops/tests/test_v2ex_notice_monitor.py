import importlib.util
import contextlib
import copy
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "v2ex_notice_monitor.py"

spec = importlib.util.spec_from_file_location("v2ex_notice_monitor", SCRIPT_PATH)
v2ex_notice_monitor = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = v2ex_notice_monitor
assert spec.loader is not None
spec.loader.exec_module(v2ex_notice_monitor)


class V2EXNoticeMonitorFilterTests(unittest.TestCase):
    def make_post(self, title: str, body: str = "", tags: list[str] | None = None):
        tags = tags or ["二手交易"]
        parsed_tags, core_title = v2ex_notice_monitor.extract_tags_and_core(title)
        if parsed_tags:
            tags = parsed_tags
        rough_type = v2ex_notice_monitor.classify(tags, title, core_title)
        return v2ex_notice_monitor.Post(
            message_id=1,
            telegram_url="https://t.me/v2ex_notice/1",
            v2ex_url="https://www.v2ex.com/t/1210926",
            posted_at="",
            title=title,
            tags=tags,
            core_title=core_title,
            rough_type=rough_type,
            body_excerpt=body,
        )

    def test_blacklisted_merchant_is_prefiltered(self):
        post = self.make_post(
            "【二手交易】[摩集电商] MacBook Pro 14 16 寸 官翻机/全新机清仓 本月价格更新",
            "联系方式：某宝店铺搜索：摩集电商企业店",
        )
        self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_locked_mdm_and_refurb_device_deals_are_prefiltered(self):
        for body in ["美版有锁机低价出", "监管机/租赁机便宜出", "国行官翻机清仓", "MDM 设备低价"]:
            with self.subTest(body=body):
                post = self.make_post("【二手交易】出 iPhone 15 Pro 256G 明盘 3999", body)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_jd_card_pt_housing_car_and_liquor_posts_are_prefiltered(self):
        cases = [
            ("【二手交易】出两张 1000，一张 500 京东 e 卡", "975 折，走闲鱼交易，直接提供卡密"),
            ("送 3 个 pt 站 HDarea 的邀请码", "有需要的朋友留邮箱我发邀请"),
            ("东坝、东坝北地铁站附近找合租", "预算 2000 以下，住一人"),
            ("【二手交易】出一辆二手车", "明盘 3 万，可看车"),
            ("【二手交易】车位出租", "小区地下车位长租"),
            ("【二手交易】转让五粮液旅游资格和五粮液 1.5L 52 度 1816 品鉴酒", "喝五粮液扫码中奖所得，1399 转让旅游资格，另出品鉴酒。"),
            ("【二手交易】出一箱茅台白酒", "53 度飞天茅台，走闲鱼。"),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_automotive_service_store_promos_are_prefiltered(self):
        cases = [
            (
                "【副业】跟风开了一个自助洗车店",
                "我在回龙观开了一家自助洗车店，欢迎 V 友到店体验，24 小时营业。",
                ["副业"],
            ),
            (
                "【推广】汽车美容贴膜保养门店开业",
                "本地车服门店引流，洗车、贴膜、补胎、保养都有优惠。",
                ["推广"],
            ),
            (
                "【推广】新能源车充电桩停车场活动",
                "附近停车场充电桩活动，扫码领券到店使用。",
                ["推广"],
            ),
        ]
        for title, body, tags in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=tags)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_local_service_offers_are_prefiltered_even_with_price_or_beijing(self):
        cases = [
            (
                "1216265",
                "【二手交易】40 元出京东家政服务",
                "2 小时，55 平以下，一共 2 个。可以联系绿泡泡：MTg2NjM4Mjc1Mzc=",
                ["二手交易"],
                {"local_service_offer"},
            ),
            (
                "1216267",
                "【二手交易】成都出京东家政(40r)/洗衣(15r)",
                "其他的京东 plus 权益有需要也可以出，有意联系绿泡泡: YW1yenNfcm9j",
                ["二手交易"],
                {"local_service_offer", "non_beijing_local"},
            ),
            (
                "1215838",
                "【二手交易】上海京东家政 40 出",
                "绿泡泡：TXJERDQyNzc5 请标注：家政",
                ["二手交易"],
                {"local_service_offer", "non_beijing_local"},
            ),
            (
                "beijing-service",
                "【二手交易】北京出京东家政服务 40 元",
                "2 小时，55 平以下，朝阳可用。",
                ["二手交易"],
                {"local_service_offer"},
            ),
            (
                "onsite-voucher",
                "【二手交易】出到店核销洗衣服务券 15 元",
                "洗鞋洗衣服务券，本地门店到店核销。",
                ["二手交易"],
                {"local_service_offer"},
            ),
            (
                "runner",
                "【二手交易】出跑腿代办上门维修券",
                "搬家、安装、维修、代办服务都可用，明盘 30。",
                ["二手交易"],
                {"local_service_offer"},
            ),
        ]
        for topic_id, title, body, tags, expected_categories in cases:
            with self.subTest(topic_id=topic_id):
                post = self.make_post(title, body, tags=tags)
                post.v2ex_url = f"https://www.v2ex.com/t/{topic_id}"
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "exclude")
                self.assertTrue(expected_categories.issubset(set(decision.matched_categories)))
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_service_exclusion_does_not_block_online_api_or_model_services(self):
        cases = [
            self.make_post(
                "【推广】OpenAI API 服务免费额度",
                "注册送 20 刀免费额度，Base URL 可接入，兑换码 V2EX。",
                tags=["推广"],
            ),
            self.make_post(
                "【推广】Codex 模型服务兑换码",
                "V 友兑换码 CODEX20，领取 20 刀 API credit。",
                tags=["推广"],
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "candidate")
                self.assertNotIn("local_service_offer", decision.matched_categories)

    def test_ai_token_method_or_question_posts_do_not_reach_instant_model(self):
        cases = [
            self.make_post(
                "【水】白嫖 ai-token 的方法",
                "",
                tags=["水"],
            ),
            self.make_post(
                "【问与答】有白嫖 ai-token 的方法吗",
                "",
                tags=["问与答"],
            ),
            self.make_post(
                "【分享发现】整理一些白嫖 AI token 渠道",
                "只是讨论方法和渠道，没有邀请码、金额、兑换码或注册领取方式。",
                tags=["分享发现"],
            ),
        ]
        cases[0].v2ex_url = "https://www.v2ex.com/t/1217875"
        for post in cases:
            with self.subTest(title=post.title):
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                facts = v2ex_notice_monitor.build_post_facts(post)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("offer_method_or_question", decision.matched_categories)
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))
                self.assertIn(facts.offer_facts.intent, {"method_or_tutorial", "question_or_discussion"})
                self.assertFalse(facts.offer_facts.has_direct_benefit)

    def test_direct_ai_benefits_still_reach_instant_model(self):
        cases = [
            self.make_post(
                "【推广】AI 中转站新用户福利",
                "OpenAI API 中转，新用户注册送 10 美元平台余额，Base URL 可直接接入。",
                tags=["推广"],
            ),
            self.make_post(
                "【推广】V 友福利 AI 中转站",
                "V 友福利兑换码 V2EX20，注册后领取平台余额，0.08 元/刀，倍率 0.1。",
                tags=["推广"],
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                facts = v2ex_notice_monitor.build_post_facts(post)
                self.assertEqual(decision.route, "candidate")
                self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))
                self.assertEqual(facts.offer_facts.intent, "direct_offer")
                self.assertTrue(facts.offer_facts.has_direct_benefit)

    def test_low_value_consumer_app_membership_perk_is_excluded_from_instant_and_digest(self):
        post = self.make_post(
            "【问与答】[送永久会员] 手机壁纸工坊 APP，轻松制作有质感、极简壁纸",
            "iOS 和 Android 各 20 个 lifetime 兑换码，回复后随机发放。"
            "主要功能是制作手机壁纸、头像和锁屏图片。",
            tags=["问与答"],
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("consumer_app_perk_low_value", decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_developer_or_ai_tool_benefits_are_not_consumer_app_perks(self):
        developer_tool = self.make_post(
            "【推广】开发者 API 调试工具兑换码",
            "提供 API/SDK 调试工作台，V 友兑换码 DEV20，领取 20 刀 credit。",
            tags=["推广"],
        )
        developer_decision = v2ex_notice_monitor.evaluate_post_for_monitor(developer_tool)
        self.assertNotIn("consumer_app_perk_low_value", developer_decision.matched_categories)

        ai_relay = self.make_post(
            "【推广】OpenAI API 中转站新用户福利",
            "OpenAI API 中转，新用户注册送 10 美元平台余额，Base URL 可直接接入。",
            tags=["推广"],
        )
        ai_decision = v2ex_notice_monitor.evaluate_post_for_monitor(ai_relay)
        self.assertEqual(ai_decision.route, "candidate")
        self.assertNotIn("consumer_app_perk_low_value", ai_decision.matched_categories)

    def test_locality_gate_allows_beijing_district_hardware_and_blocks_non_beijing_local_only(self):
        beijing_terms = [
            "北京",
            "东城",
            "西城",
            "朝阳",
            "丰台",
            "石景山",
            "海淀",
            "门头沟",
            "房山",
            "通州",
            "顺义",
            "昌平",
            "大兴",
            "怀柔",
            "平谷",
            "密云",
            "延庆",
            "亦庄",
        ]
        for term in beijing_terms:
            with self.subTest(term=term):
                post = self.make_post(
                    f"【二手交易】{term} 出 iPhone 15 Pro 3999 元",
                    "国行无锁，本地自提，走闲鱼。",
                )
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "candidate")
                self.assertIn("secondary_market_sale", decision.matched_categories)

        compact_beijing = self.make_post(
            "【二手交易】北京出 iPhone 15 Pro 3999 元",
            "国行无锁，本地自提，走闲鱼。",
        )
        compact_decision = v2ex_notice_monitor.evaluate_post_for_monitor(compact_beijing)
        self.assertEqual(compact_decision.route, "candidate")
        self.assertIn("secondary_market_sale", compact_decision.matched_categories)

        non_beijing_cases = [
            ("【二手交易】成都出 iPhone 15 Pro 3999 元", "仅成都本地自提，国行无锁，走闲鱼。"),
            ("【二手交易】上海出 Mac mini M4 3500 元", "本地面交，走闲鱼。"),
            ("【二手交易】深圳出 NAS 16T 硬盘 900 元", "深圳自提，慢扫全绿。"),
        ]
        for title, body in non_beijing_cases:
            with self.subTest(title=title):
                post = self.make_post(title, body)
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("non_beijing_local", decision.matched_categories)
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_optional_non_beijing_pickup_with_remote_trade_still_reaches_model(self):
        post = self.make_post(
            "【二手交易】大僵 nano64g 标准套餐 1550；询价内存条 DDR5 16G*2",
            "大僵 nano64g 标准套餐 1550，年会奖品没怎么用过，一直吃灰。"
            "64g 标准套餐，无随心换，包装都在，质保也在，自认为 95 新。"
            "支持上海杨浦自提，自提优惠 20。价格不对请指出。"
            "[闲鱼] https://m.tb.cn/h.Ri0aHoO?tk=Nodmgb6vpPk "
            "询价内存条 DDR5 16G*2 长鑫 6000Mhz CL36，"
            "25.9pdd 购入，箱说全，可单卖一根 1200。"
            "上海杨浦可自提，自提优惠 50，价格我没调查过，价格不对请指出。"
            "[闲鱼] https://m.tb.cn/h.RiDWBle?tk=q3Drgb6E8yn",
        )
        post.v2ex_url = "https://www.v2ex.com/t/1218213"

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        facts = v2ex_notice_monitor.build_post_facts(post)

        self.assertEqual(decision.route, "candidate")
        self.assertIn("secondary_market_sale", decision.matched_categories)
        self.assertNotIn("non_beijing_local", decision.matched_categories)
        self.assertEqual(facts.locality.transaction_scope, "optional_pickup_or_remote")
        self.assertIn("上海", facts.locality.locality_names)
        self.assertTrue(facts.locality.optional_pickup_evidence)
        self.assertTrue(facts.locality.remote_trade_evidence)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_optional_non_beijing_pickup_without_remote_trade_still_blocks(self):
        post = self.make_post(
            "【二手交易】上海出 Mac mini M4 3500 元",
            "支持上海杨浦自提，国行无锁，本地面交优先。",
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        facts = v2ex_notice_monitor.build_post_facts(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("non_beijing_local", decision.matched_categories)
        self.assertEqual(facts.locality.transaction_scope, "non_beijing_local_only")

    def test_local_only_unknown_region_is_excluded_but_shippable_hardware_still_reaches_model(self):
        local_only = self.make_post(
            "【二手交易】出 iPhone 15 Pro 3999 元",
            "仅限本地自提，国行无锁。",
        )
        local_decision = v2ex_notice_monitor.evaluate_post_for_monitor(local_only)
        self.assertEqual(local_decision.route, "exclude")
        self.assertIn("unknown_region_local_only", local_decision.matched_categories)

        shippable = self.make_post(
            "【二手交易】出 iPhone 15 Pro 3999 元",
            "国行无锁，走闲鱼，包邮。",
        )
        ship_decision = v2ex_notice_monitor.evaluate_post_for_monitor(shippable)
        self.assertEqual(ship_decision.route, "candidate")
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(shippable))

    def test_low_value_broadband_info_posts_are_prefiltered(self):
        post = self.make_post(
            "【宽带症候群】成都电信专线体验有感",
            "现有套餐：上行 100M，下行 1000M，固定 IP，资费 599 元/月，连接香港 VPS 波动基本一条线。",
            tags=["宽带症候群"],
        )
        self.assertTrue(v2ex_notice_monitor.is_low_value_info_post(post))
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_non_jd_niche_shopping_card_is_not_prefiltered_by_generic_card_word(self):
        post = self.make_post("【二手交易】出冷门商超购物卡 8 折", "余额 500，走闲鱼，具体平台可核验。")
        self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_regular_hard_drive_sale_without_pt_context_still_reaches_agent(self):
        post = self.make_post("【二手交易】出两块东芝 16T 机械硬盘", "慢扫全绿，通电时间分别 39000+、28000+，还剩一年店保，打包可优惠。")
        self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_mixed_gaming_lot_with_non_gaming_value_reaches_model(self):
        cases = [
            (
                "1216150",
                "【二手交易】继续出 switch 卡带+机械硬盘 4Tx2+中兴 CPE",
                "中兴 U50 Pro 5G CPE 支持 wifi6，明盘：650。希捷酷狼 4T x 2 打包出，明盘：1150。switch 游戏卡带打包明盘：260。",
            ),
            (
                "mixed-hdd",
                "【二手交易】出 Switch 卡带和东芝 16T 机械硬盘",
                "Switch 卡带 180，东芝 16T 机械硬盘慢扫全绿，打包 900，走闲鱼。",
            ),
        ]
        for topic_id, title, body in cases:
            with self.subTest(topic_id=topic_id):
                post = self.make_post(title, body)
                self.assertIsNotNone(v2ex_notice_monitor.GAMING_DEVICE_RE.search(" ".join([post.title, post.body_excerpt])))
                self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "candidate")
                self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_pure_gaming_devices_remain_prefiltered_even_with_price(self):
        cases = [
            ("【二手交易】出 Switch 卡带", "卡带 180 包邮，走闲鱼。"),
            ("【二手交易】出 PS5 DualSense 手柄", "全新未拆，明盘 380。"),
            ("【二手交易】出 Switch OLED 主机", "国行 Switch 主机，明盘 1500。"),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_photography_camera_gear_offers_are_prefiltered(self):
        cases = [
            (
                "1216358",
                "【二手交易】出一个 a7c2 自用",
                "[闲鱼] https://m.tb.cn/h.R5SE0fl?tk=lVyW5CDL5eo CZ001 "
                "「我在闲鱼发布了 [灭门国行 999 新索尼 A7C2 微单机身] 」 点击链接直接打开 v2 可小刀",
            ),
            ("sony-a7c2", "【二手交易】出索尼 A7C2 微单机身", "国行 99 新，明盘 9000，走闲鱼，可小刀。"),
            ("canon-rf", "【二手交易】出佳能 RF 24-70 镜头", "自用镜头，明盘 9800，箱说齐全。"),
            ("fuji-x100", "【二手交易】出富士 X100VI", "快门 1200，银色，走闲鱼。"),
            ("nikon-zf", "【二手交易】出尼康 Zf 机身", "全套包装，明盘 9800。"),
            ("action-camera", "【二手交易】出 GoPro 和 Insta360 运动相机", "配件齐全，打包 2600。"),
            ("dji-pocket", "【二手交易】出大疆 Pocket 3 和云台", "摄像设备闲置，明盘 3200。"),
        ]
        for topic_id, title, body in cases:
            with self.subTest(topic_id=topic_id):
                post = self.make_post(title, body)
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("photography_camera_gear", decision.matched_categories)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_photography_preference_ignores_addon_in_mixed_high_value_hardware_lot(self):
        post = self.make_post(
            "【二手交易】出 MacBook Pro 5000，附赠索尼镜头",
            "M2 Pro 16G 512G，明盘 5000，走闲鱼；镜头可不要。",
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "candidate")
        self.assertIn("secondary_market_sale", decision.matched_categories)
        self.assertIn("photography_camera_gear", decision.ignored_low_value_categories)
        self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_photography_camera_gear_rule_does_not_block_non_camera_value(self):
        cases = [
            self.make_post(
                "【二手交易】出 iPhone 15 Pro 256G 明盘 3999",
                "全新未拆封，拍照很好，支持 eSIM，非卡贴，走闲鱼。",
            ),
            self.make_post(
                "【二手交易】出 Mac mini M2 16G 512G 明盘 3500",
                "个人自用，偶尔修图剪视频，走闲鱼。",
            ),
            self.make_post(
                "【问与答】摄像头权限问题怎么排查",
                "Electron 应用调用系统相机权限失败，不是交易。",
                tags=["问与答"],
            ),
            self.make_post(
                "【二手交易】出 Sony PS5 手柄",
                "DualSense 手柄，明盘 380。",
            ),
        ]
        decisions = [v2ex_notice_monitor.evaluate_post_for_monitor(post) for post in cases]

        self.assertEqual(decisions[0].route, "candidate")
        self.assertNotIn("photography_camera_gear", decisions[0].matched_categories)
        self.assertEqual(decisions[1].route, "candidate")
        self.assertNotIn("photography_camera_gear", decisions[1].matched_categories)
        self.assertEqual(decisions[2].route, "exclude")
        self.assertNotIn("photography_camera_gear", decisions[2].matched_categories)
        self.assertEqual(decisions[3].route, "exclude")
        self.assertIn("gaming_device", decisions[3].matched_categories)
        self.assertNotIn("photography_camera_gear", decisions[3].matched_categories)

    def test_photography_camera_gear_is_not_sent_to_arbitrage_scan(self):
        camera = self.make_post(
            "【二手交易】出索尼 A7C2 微单机身",
            "国行 99 新，明盘 9000，走闲鱼。",
        )
        camera.message_id = 1
        hardware = self.make_post(
            "【二手交易】出 iPhone 15 Pro 256G 明盘 3999",
            "全新未拆封，无锁，走闲鱼。",
        )
        hardware.message_id = 2
        posts = [camera, hardware]

        decisions = v2ex_notice_monitor.route_decisions_by_message_id(posts)
        scan_posts = v2ex_notice_monitor.arbitrage_scan_posts(posts, decisions)

        self.assertEqual(scan_posts, [hardware])
        self.assertEqual(decisions[1].route, "exclude")
        self.assertIn("photography_camera_gear", decisions[1].matched_categories)

    def test_digital_game_commerce_offers_are_prefiltered(self):
        cases = [
            (
                "1216294",
                "【推广】007 First Light 游戏已发售！了解价格比 Steam 低的平台 (只要 260)",
                "ChinaPlay 价 260.58，Steam 价 298。别忘记输入优惠码：V2PromoCP。期待已久的 007 詹姆斯·邦德新作正式登场。",
                ["推广"],
            ),
            (
                "steam-key",
                "【二手交易】出 Steam 游戏激活码 / CDK",
                "卡密、兑换码都有，低价出，走闲鱼。",
                ["二手交易"],
            ),
            (
                "epic-dlc",
                "【推广】Epic 游戏 DLC 季票首发折扣",
                "数字版上架，优惠码 V2EXGAME，支持激活码兑换。",
                ["推广"],
            ),
            (
                "game-topup",
                "【推广】游戏平台充值折扣点卡",
                "游戏账号点券、皮肤、道具充值 8 折，提供卡密。",
                ["推广"],
            ),
        ]
        for topic_id, title, body, tags in cases:
            with self.subTest(topic_id=topic_id):
                post = self.make_post(title, body, tags=tags)
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("digital_game_commerce", decision.matched_categories)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_digital_game_commerce_rule_does_not_block_non_game_value(self):
        cases = [
            self.make_post(
                "【推广】开发者工具限时优惠码",
                "API/SDK/GitHub CLI 工具，Chrome Web Store 可下载，优惠码 DEV20。",
                tags=["推广"],
            ),
            self.make_post(
                "【分享创造】Unity 游戏开发资源管理 CLI",
                "开源项目，GitHub repo，处理游戏素材构建流程，不卖游戏 key。",
                tags=["分享创造"],
            ),
            self.make_post(
                "【二手交易】出 MacBook Pro 5000，附赠 Steam 激活码",
                "M2 Pro 16G 512G，明盘 5000，走闲鱼；游戏激活码可不要。",
            ),
        ]
        decisions = [v2ex_notice_monitor.evaluate_post_for_monitor(post) for post in cases]
        self.assertNotIn("digital_game_commerce", decisions[0].matched_categories)
        self.assertNotIn("digital_game_commerce", decisions[1].matched_categories)
        self.assertEqual(decisions[1].route, "exclude")
        self.assertIn("low_economic_technical", decisions[1].matched_categories)
        self.assertEqual(decisions[2].route, "candidate")
        self.assertIn("secondary_market_sale", decisions[2].matched_categories)
        self.assertIn("digital_game_commerce", decisions[2].ignored_low_value_categories)

    def test_normal_phone_deal_still_reaches_agent(self):
        post = self.make_post("【二手交易】出 iPhone 15 Pro 256G 明盘 3999", "全新未拆封，无任何锁，非 MDM 设备。")
        self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_secondary_market_phone_listing_without_title_sale_verb_reaches_model(self):
        post = self.make_post(
            "【二手交易】99.9 新红米 Note13",
            "买来后用的特别少，常年在抽屉里吃灰。手机包装盒、官方送的保护套都在。"
            "售价 600 元，有需要的朋友加绿泡泡。实拍与参数见图。",
        )
        post.v2ex_url = "https://www.v2ex.com/t/1217211"

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        facts = v2ex_notice_monitor.build_post_facts(post)

        self.assertEqual(decision.route, "candidate")
        self.assertIn("secondary_market_sale", decision.matched_categories)
        self.assertTrue(facts.has_sale_offer)
        self.assertTrue(facts.has_high_value_hardware)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_secondary_market_price_check_electronics_with_esim_capability_reaches_model(self):
        post = self.make_post(
            "【二手交易】想出点东西，但是不知什么价合适，大佬们可以看看",
            "1、iPhone 16 pro max 国际版，单实体+esim，256g，成色很好，一直戴套使用，"
            "电池 91%，无磕碰。淘宝百亿换 17pm 可以抵 4850。"
            "2、Apple watch ultra3 国际版，蜂窝版，爱回收估价才 3130。"
            "3、ipad mini 7 128g Wi-Fi 国际版，爱回收估价 1810。自用设备，成色都很好，先询价。",
        )
        post.v2ex_url = "https://www.v2ex.com/t/1217170"

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        facts = v2ex_notice_monitor.build_post_facts(post)

        self.assertEqual(decision.route, "candidate")
        self.assertIn("secondary_market_sale", decision.matched_categories)
        self.assertTrue(facts.has_sale_offer)
        self.assertTrue(facts.has_high_value_hardware)
        self.assertNotIn("telecom_card", facts.primary_marketplace_categories)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_secondary_market_sale_with_device_but_weak_listing_evidence_goes_to_digest(self):
        post = self.make_post(
            "【二手交易】想出 Mac mini M4，16G/512G，成色很好，先询价",
            "自用设备，箱说齐全，暂时没查行情，也还没放闲鱼。",
            tags=["二手交易"],
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        facts = v2ex_notice_monitor.build_post_facts(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("secondary_market_sale_candidate", decision.matched_categories)
        self.assertTrue(facts.has_sale_offer)
        self.assertTrue(facts.has_high_value_hardware)
        self.assertFalse(facts.secondary_listing.money_evidence)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_evaluate_post_for_monitor_returns_single_route_decision(self):
        post = self.make_post("【二手交易】出 iPhone 15 Pro 256G 明盘 3999", "全新未拆封，无锁，走闲鱼。")

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "candidate")
        self.assertIn("secondary_market_sale", decision.matched_categories)
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), decision.route)
        self.assertEqual(v2ex_notice_monitor.is_instant_candidate(post), decision.route == "candidate")

    def test_apply_user_preferences_reports_structured_negative_and_ignored_categories(self):
        camera = self.make_post("【二手交易】出索尼 A7C2 微单机身", "国行 99 新，明盘 9000，走闲鱼。")
        camera_preference = v2ex_notice_monitor.apply_user_preferences(v2ex_notice_monitor.build_post_facts(camera))

        self.assertIn("photography_camera_gear", camera_preference.negative_categories)
        self.assertIn("photography_camera_gear", camera_preference.primary_blocking_categories)
        self.assertEqual(camera_preference.ignored_addon_categories, [])

        mixed = self.make_post(
            "【二手交易】出 MacBook Pro 5000，附赠索尼镜头",
            "M2 Pro 16G 512G，明盘 5000，走闲鱼；镜头可不要。",
        )
        mixed_preference = v2ex_notice_monitor.apply_user_preferences(v2ex_notice_monitor.build_post_facts(mixed))

        self.assertIn("photography_camera_gear", mixed_preference.negative_categories)
        self.assertIn("mixed_high_value_hardware", mixed_preference.relaxation_categories)
        self.assertIn("photography_camera_gear", mixed_preference.ignored_addon_categories)
        self.assertEqual(mixed_preference.primary_blocking_categories, [])

    def test_cursor_is_global_exclusion_even_with_economic_signal(self):
        cases = [
            self.make_post("【二手交易】出 Cursor Pro 账号", "明盘 50 元，走闲鱼。"),
            self.make_post("【推广】Cursor 官方福利", "注册送 $10 credit，兑换码 V2EX。", tags=["推广"]),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("cursor", decision.matched_categories)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_mixed_low_value_addons_with_high_value_hardware_reach_model(self):
        cases = [
            self.make_post(
                "【二手交易】出 iPhone 15 Pro 3999 + giffgaff SIM 20",
                "手机美版无锁，支持 eSIM，走闲鱼；另有 giffgaff 卡可不要。",
            ),
            self.make_post(
                "【二手交易】出 Mac mini M2 3500，附带山姆副卡",
                "Mac mini 16G+512G 个人自用，明盘 3500，山姆副卡可不要。",
            ),
            self.make_post(
                "【二手交易】出 NAS 硬盘 16T，另送 PT 邀请码",
                "东芝 16T 机械硬盘慢扫全绿，明盘 900，PT 邀请码可不要。",
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "candidate")
                self.assertIn("secondary_market_sale", decision.matched_categories)
                self.assertTrue(decision.ignored_low_value_categories)
                self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_pure_low_value_addon_lots_are_excluded(self):
        post = self.make_post(
            "【二手交易】出 giffgaff SIM + 88VIP + 山姆副卡 + 京东 E 卡",
            "打包 200，都是闲置卡券权益。",
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("telecom_card", decision.matched_categories)
        self.assertIn("membership_entitlement", decision.matched_categories)
        self.assertIn("sams_club_card", decision.matched_categories)
        self.assertIn("jd_card", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_subscription_topup_above_75_is_prefiltered(self):
        sample_1216481 = self.make_post(
            "【推广】数字产品，即买，即发",
            "土区 App Store 礼品卡 500 里拉 (¥80.00)。Apple ID 土区注册或转区服务 （ ¥50.00 ）。"
            "ChatGPT Plus 兑换 ( ¥130.00 )。ChatGPT Pro 5x 代充 （¥780.00 ）。"
            "ChatGPT Pro 20x 代充 （¥1,380.00 ）。是否有折扣优惠，你得自己跟我哥们谈。",
            tags=["推广", "礼品卡", "账号", "服务"],
        )
        overpriced = self.make_post(
            "【推广】ChatGPT Plus 76 元 / Claude Pro 88 元 / ChatGPT Pro 780 元",
            "第三方代充兑换，无免费额度，无兑换码，无 V 友福利。",
            tags=["推广"],
        )
        threshold = self.make_post(
            "【推广】ChatGPT Plus 75 元 / Claude Pro 75 元",
            "第三方代充兑换，可自用，来源需确认。",
            tags=["推广"],
        )
        contact_only = self.make_post(
            "【推广】ChatGPT Plus 可用",
            "QQ群 1097355337，欢迎进群咨询。",
            tags=["推广"],
        )

        for post in (sample_1216481, overpriced):
            with self.subTest(title=post.title):
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("overpriced_ai_subscription_topup", decision.matched_categories)
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

        threshold_decision = v2ex_notice_monitor.evaluate_post_for_monitor(threshold)
        self.assertEqual(threshold_decision.route, "candidate")
        self.assertIn("ai_subscription_threshold", threshold_decision.matched_categories)

        contact_decision = v2ex_notice_monitor.evaluate_post_for_monitor(contact_only)
        self.assertEqual(contact_decision.route, "exclude")
        self.assertEqual(v2ex_notice_monitor._extract_openai_subscription_prices("ChatGPT Plus 可用，QQ群 1097355337", v2ex_notice_monitor.OPENAI_PLUS_PRODUCT_RE), [])
        self.assertEqual(v2ex_notice_monitor._extract_ai_subscription_offers("ChatGPT Pro 5x 代充 ¥780，微信 1097355337")[0]["price"], 780.0)

    def test_reasonably_priced_pro20_coupon_sale_gets_instant_alert(self):
        post = self.make_post(
            "【二手交易】出一个 pro 20×优惠码，官方退的。",
            "时间：2026-06-30T11:45:13+00:00\n"
            "摘要：因为个人懒得折腾绑卡激活了，挂出来卖。不太知道行情，先标个 1k，走咸鱼交易。",
            tags=["二手交易"],
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        reason = v2ex_notice_monitor.instant_alert_reason(post)

        self.assertEqual(decision.route, "candidate")
        self.assertIn("ai_subscription_code_threshold", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))
        self.assertIn("Pro20", reason)
        self.assertIn("1000", reason)
        self.assertIn("风险", reason)

    def test_pure_ai_support_or_subscription_questions_are_excluded(self):
        cases = [
            (
                "【龙虾烧香】佬们，小龙虾很久没看配置提示： memorySearch 功能不正常如何解决",
                "外部 embedding model 怎么配？默认模型一直在吃 OpenAI 免费额度，CPA 反代也不太正常，求解决。",
            ),
            (
                "【问与答】Codex Go 与 Plus",
                "Codex 额度耗尽了，想开 Codex Go 或 Plus，哪个方案便宜有效？有没有推荐解决方案？",
            ),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=["问与答"])
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

                self.assertEqual(decision.route, "exclude")
                self.assertIn("ai_support_question_without_offer", decision.matched_categories)
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_app_policy_review_and_account_questions_do_not_enter_tool_digest(self):
        cases = [
            (
                "【程序员】问下大佬们，你们走出海的话，隐私政策和用户协议一般是怎么搞的？直接用 AI 生成吗？",
                "准备上架应用，想问问隐私政策、用户协议是找律师还是直接用 AI 生成。",
                ["程序员"],
            ),
            (
                "【Apple】app store 国区和 chatgpt 相关的都不让上架了",
                "最近审核被拒，想问问大家是不是国区 ChatGPT 相关 App 都不让上架了。",
                ["Apple"],
            ),
            (
                "【问与答】为什么我的 App 每次都在“等待审核”卡一周",
                "App Store 审核一直等待审核，大家有没有遇到过，怎么催审？",
                ["问与答"],
            ),
            (
                "【问与答】Google Play 充值的 ChatGPT 账户这两个有绑定关系吗",
                "如果用 Google Play 充值 ChatGPT，账号和 Google 付款账号是否绑定？",
                ["问与答"],
            ),
        ]
        for title, body, tags in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=tags)
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

                self.assertEqual(decision.route, "exclude")
                self.assertIn("app_policy_support_question", decision.matched_categories)
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_secondary_market_buy_requests_never_wake_instant_model(self):
        sample_1216492 = self.make_post(
            "[40 收] 88VIP 优惠券 1500 - 180",
            "40 收淘宝 88 会员的美妆券，包含：1500 - 180 消费券，1500 - 200 美妆券，88 会员随便领。"
            "交易流程：按我给的流程把东西拍下来，然后闲鱼做一个利润加商品的链接，收货后当天闲鱼确认收货。"
            "微信联系 sheepemail",
            tags=["二手交易"],
        )
        concrete_hardware = self.make_post(
            "【二手交易】求购 Mac mini M4",
            "预算 3500，16G/256G 起，成色正常，北京面交或邮寄都可以。",
            tags=["二手交易"],
        )
        with_ai_code_side_offer = self.make_post(
            "【二手交易】求购 Mac mini M4",
            "预算 3500；另外出一个 OpenAI API 20 刀兑换码，可单出。",
            tags=["二手交易"],
        )
        vague_or_negative_cases = [
            self.make_post("[40 收] 88VIP 优惠券 1500 - 180", "淘宝 88 会员美妆券，闲鱼利润链接。", tags=["二手交易"]),
            self.make_post("【二手交易】收购会员权益优惠券", "长期收各种会员权益和优惠券。", tags=["二手交易"]),
            self.make_post("【二手交易】蹲个 giffgaff 电话卡", "预算 100。", tags=["二手交易"]),
            self.make_post("【二手交易】求购 Switch 卡带", "预算 150。", tags=["二手交易"]),
            self.make_post("【二手交易】求购索尼镜头", "预算 2000。", tags=["二手交易"]),
            self.make_post("【二手交易】求购便宜电脑", "预算看情况。", tags=["二手交易"]),
        ]

        sample_decision = v2ex_notice_monitor.evaluate_post_for_monitor(sample_1216492)
        self.assertEqual(sample_decision.route, "exclude")
        self.assertIn("secondary_market_buy_request", sample_decision.matched_categories)
        self.assertIn("membership_entitlement", sample_decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(sample_1216492))

        concrete_decision = v2ex_notice_monitor.evaluate_post_for_monitor(concrete_hardware)
        self.assertEqual(concrete_decision.route, "exclude")
        self.assertIn("secondary_market_buy_request", concrete_decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(concrete_hardware))

        side_offer_decision = v2ex_notice_monitor.evaluate_post_for_monitor(with_ai_code_side_offer)
        self.assertEqual(side_offer_decision.route, "exclude")
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(with_ai_code_side_offer))

        for post in vague_or_negative_cases:
            with self.subTest(title=post.title):
                facts = v2ex_notice_monitor.build_post_facts(post)
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertTrue(facts.has_buy_request)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("secondary_market_buy_request", decision.matched_categories)
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_software_license_transfers_are_prefiltered(self):
        cases = [
            ("【二手交易】转 likeshop-saas 版授权", "SaaS 版软件授权转让，价格私聊。"),
            ("【二手交易】出 Surge Mac 5 授权", "永久授权，可转让。"),
            ("【二手交易】转让某开发工具永久 license", "正版 license，明盘 300。"),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))


    def test_arbitrage_scan_uses_model_route_only(self):
        model_post = self.make_post("【二手交易】出 iPhone 15 Pro 256G 明盘 3999", "全新未拆封，无锁，走闲鱼。")
        model_post.message_id = 1
        excluded_post = self.make_post("【二手交易】出 giffgaff SIM + 88VIP", "打包 100。")
        excluded_post.message_id = 2
        technical_post = self.make_post("【分享创造】开源许可证合规扫描 CLI", "GitHub repo，支持 Docker。", tags=["分享创造"])
        technical_post.message_id = 3
        posts = [model_post, excluded_post, technical_post]

        decisions = v2ex_notice_monitor.route_decisions_by_message_id(posts)
        scan_posts = v2ex_notice_monitor.arbitrage_scan_posts(posts, decisions)

        self.assertEqual(scan_posts, [model_post])

    def test_openai_activation_code_still_reaches_agent(self):
        post = self.make_post(
            "【二手交易】试出一个 openai chatgpt pro 6 个月 激活码，开源所得",
            "明盘 6500，官方开源项目所得，微信 base64。",
        )
        self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_cloud_server_vps_hosting_promos_are_prefiltered(self):
        cases = [
            (
                "【推广】老李云超级低服务器 1C-512M-5M-10G 仅需 1 元",
                "老李云超级低服务器1C-512M-5M-10G 仅需 1 元百度搜索：老李云第一个就是",
                ["推广"],
            ),
            (
                "【二手交易】转让闲置 VPS 年付套餐",
                "1C 1G 20G SSD，洛杉矶机房，明盘 99 元/年，剩余 10 个月。",
                ["二手交易"],
            ),
        ]
        for title, body, tags in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=tags)
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_paid_only_ai_relay_without_free_or_low_price_is_prefiltered(self):
        post = self.make_post(
            "【推广】Feifeimiao OpenAI API 中转限时特惠，订阅套餐已开放",
            "OpenAI API 兼容接口，Base URL 接入。当前在售套餐：小喵 ¥60 / 30 天，每日 20 美元等值 OpenAI 用量；中喵 ¥150 / 30 天，每日 60 美元等值 OpenAI 用量；两个套餐均为 OpenAI 订阅组，倍率为 1x。",
            tags=["推广"],
        )
        self.assertTrue(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_rate_above_point_one_without_gift_is_prefiltered(self):
        cases = [
            (
                "1216287",
                "【推广】[FK Claude] 满血 Claude 中转站",
                "CCtest 可检测验证 www.fkclaude.xyz kiro 0.4 倍率(官方 0.6 折)可外接 api CC 专用 1.85 倍率(官方 2.6 折)限制客户端 小额使用,按需充值~",
                "exclude",
            ),
            (
                "threshold-011",
                "【推广】Claude API 中转站",
                "支持 Base URL，倍率 0.11，按需充值，小额使用。",
                "exclude",
            ),
            (
                "discount-06",
                "【推广】OpenAI API 中转",
                "中转站限时活动，官方 0.6 折，充值后使用，没有免费额度。",
                "exclude",
            ),
            (
                "rate-185",
                "【推广】Codex 中转站",
                "CC 专用 1.85 倍率，按需充值，无兑换码。",
                "exclude",
            ),
        ]
        for _case_id, title, body, expected_route in cases:
            with self.subTest(case=_case_id):
                post = self.make_post(title, body, tags=["推广"])
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, expected_route)
                if "ai_relay_review" in decision.matched_categories:
                    self.assertIn("ai_relay_review", decision.matched_categories)
                    self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
                else:
                    self.assertIn("paid_only_ai_relay_promo", decision.matched_categories)
                    self.assertTrue(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_with_free_quota_or_clear_low_price_still_reaches_agent(self):
        cases = [
            "OpenAI API 中转，V 友注册送 20 刀免费额度，兑换码 V2EX，Base URL 可直接接入。",
            "OpenAI API 中转，9 元 / 月体验套餐，额度和使用方式写清楚。",
            "OpenAI API 中转，倍率 0.1，按量计费，Base URL 可直接接入。",
            "OpenAI API 中转，0.08 元 / 刀，按量计费，Base URL 可直接接入。",
        ]
        for body in cases:
            with self.subTest(body=body):
                post = self.make_post("【推广】OpenAI API 中转 V 友福利", body, tags=["推广"])
                self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
                self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_registration_balance_day_card_reaches_model(self):
        body = (
            "一代人有一代人的鸡蛋要领 https://aixw.org "
            "注册就送 10 余额日卡，蹬 bug team 分组能蹬 1 到 2 亿 token。"
            "欢迎各位老板验货，这两天 bug team 不知道啥时候拉闸，来薅吧。"
            "plus 池最低 0.12x，pro 池最低 0.16x。"
        )
        post = self.make_post(
            "【推广】最近搭好的中转站，没人来，我也不会推广，就在这推广一下，注册就送 10 余额日卡体验，赶上 bug 号，能爽蹬两亿，赛博鸡蛋不领白不领",
            body,
            tags=["推广"],
        )
        offer_facts = v2ex_notice_monitor.build_offer_facts(v2ex_notice_monitor._post_blob(post))
        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(offer_facts.intent, "direct_offer")
        self.assertTrue(offer_facts.has_direct_benefit)
        self.assertTrue(
            any("余额日卡" in evidence for evidence in offer_facts.benefit_evidence),
            offer_facts.benefit_evidence,
        )
        self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertEqual(decision.route, "candidate")
        self.assertIn("ai_economic_value", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_registration_gift_balance_without_amount_reaches_model(self):
        post = self.make_post(
            "【推广】主打物美价廉、稳定可用的 API 中转站",
            "用 AI 跑任务时真正看重的未必只是价格，而是关键处能不能稳住、智商在线。"
            "地址： https://ai.kisekiremi.moe 注册赠送余额，欢迎试用",
            tags=["推广"],
        )

        facts = v2ex_notice_monitor.build_post_facts(post)
        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(facts.offer_facts.intent, "direct_offer")
        self.assertTrue(facts.offer_facts.has_direct_benefit)
        self.assertTrue(
            any("注册赠送余额" in evidence for evidence in facts.offer_facts.benefit_evidence),
            facts.offer_facts.benefit_evidence,
        )
        self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertEqual(decision.route, "candidate")
        self.assertIn("ai_economic_value", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_reply_and_join_group_gift_reaches_model(self):
        post = self.make_post(
            "【推广】aitokesflux|AI API 中转与 Token 接入服务，按量/套餐计费、稳定转发、开发者友好，回复+进群送 10 刀，给大家试用。",
            "我们的永久承诺：不掺假，不随意调价，数据隐私保护。"
            "充值 1RMB 到账 1 余额，claude 分组 ccMax 1.3X 倍率，caodex 分组 gptPro20 0.5X 倍率。",
            tags=["推广"],
        )

        facts = v2ex_notice_monitor.build_post_facts(post)
        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(facts.offer_facts.intent, "direct_offer")
        self.assertTrue(facts.offer_facts.has_direct_benefit)
        self.assertTrue(
            any("进群送 10 刀" in evidence or "回复+进群送 10 刀" in evidence for evidence in facts.offer_facts.benefit_evidence),
            facts.offer_facts.benefit_evidence,
        )
        self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertEqual(decision.route, "candidate")
        self.assertIn("ai_economic_value", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_high_value_gpt_plus_lottery_1221165_reaches_model(self):
        post = self.make_post(
            "【推广】抽 5 个 智友社的 GPTPLUS 稳定渠道成品号（满月测试稳定度 99% ）",
            "开奖时间：20260622 22 时 00 分 00 秒。"
            "抽奖工具：用的 V 友 shoa 的开源项目：https://github.com/wcwac/v2ex-lottery ，"
            "在本帖回复参与。奖品：5 个智友社 GPTPLUS 稳定渠道成品号。",
            tags=["推广"],
        )

        facts = v2ex_notice_monitor.build_post_facts(post)
        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertTrue(facts.lottery_facts.is_lottery)
        self.assertTrue(facts.lottery_facts.is_high_value)
        self.assertTrue(facts.lottery_facts.has_actionable_entry)
        self.assertIn("GPTPLUS", " ".join(facts.lottery_facts.prize_evidence))
        self.assertIn("5 个", " ".join(facts.lottery_facts.slot_evidence))
        self.assertIn("开奖时间", " ".join(facts.lottery_facts.draw_evidence))
        self.assertIn("回复参与", " ".join(facts.lottery_facts.entry_evidence))
        self.assertIn("high_value_lottery", facts.economic_value_types)
        self.assertEqual(decision.route, "candidate")
        self.assertIn("high_value_lottery", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_lottery_tool_or_method_without_actionable_prize_stays_excluded(self):
        cases = [
            (
                "【分享创造】做了一个 V2EX 抽奖工具",
                "开源项目 https://github.com/wcwac/v2ex-lottery ，支持按回复楼层抽奖，没有奖品。",
            ),
            (
                "【问与答】怎么抽 GPT Plus 成品号比较靠谱？",
                "想问问大家有没有抽奖渠道和方法，没有明确奖品、名额或开奖时间。",
            ),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=["分享发现"])
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                facts = v2ex_notice_monitor.build_post_facts(post)

                self.assertFalse(facts.lottery_facts.is_high_value and facts.lottery_facts.has_actionable_entry)
                self.assertNotIn("high_value_lottery", facts.economic_value_types)
                self.assertEqual(decision.route, "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_low_value_or_hard_excluded_lottery_stays_excluded(self):
        cases = [
            (
                "【推广】抽 10 个壁纸 App 永久会员",
                "回复参与，明晚开奖，奖品是壁纸 App lifetime 会员兑换码。",
                ["推广"],
                "consumer_app_perk_low_value",
            ),
            (
                "【推广】抽 3 个 Steam 游戏激活码",
                "回复参与，开奖时间 20260622 22:00，奖品是游戏 key。",
                ["推广"],
                "digital_game_commerce",
            ),
        ]
        for title, body, tags, category in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=tags)
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

                self.assertEqual(decision.route, "exclude")
                self.assertIn(category, decision.matched_categories)

    def test_ai_relay_rate_above_point_one_with_no_direct_benefit_stays_excluded(self):
        post = self.make_post(
            "【推广】OpenAI API 中转站",
            "支持 Base URL，plus 池最低 0.12x，pro 池最低 0.16x，按需充值，小额使用。",
            tags=["推广"],
        )

        self.assertTrue(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_paid_but_concrete_review_candidate_goes_to_digest(self):
        post = self.make_post(
            "【推广】OpenAI API 中转站",
            "官网 https://relay.example/v1，支持 Base URL，覆盖 GPT-5.4、Claude、Gemini。"
            "plus 池最低 0.12x，pro 池最低 0.16x，按需充值，套餐价格见站内。",
            tags=["推广"],
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        facts = v2ex_notice_monitor.build_post_facts(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("ai_relay_review", decision.matched_categories)
        self.assertFalse(facts.offer_facts.has_direct_benefit)
        self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_large_reply_credit_is_not_misclassified_as_image_generation_only(self):
        post = self.make_post(
            "【推广】中转站限时特惠， 0.075 倍率特惠分组，评论区送 50 刀体验额度",
            "plus 特惠限时 0.075 倍率，高稳定。"
            "纯 pro 0.16 倍率，gpt-image-2 生图，0.03 一张。"
            "注册留言 id 送 50 刀额度测试，网址 https://humanwill.xyz 。"
            "小铺充值 1 比 10 https://pay.ldxp.cn/shop/R1Q56348 ，qq 群：827729569，TG 频道：https://t.me/humanAiTalk",
            tags=["推广"],
        )
        post.v2ex_url = "https://www.v2ex.com/t/1223696"

        facts = v2ex_notice_monitor.build_post_facts(post)
        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
        reason = v2ex_notice_monitor.instant_alert_reason(post)

        self.assertTrue(facts.has_ai_economic_value)
        self.assertTrue(facts.offer_facts.has_direct_benefit)
        self.assertFalse(v2ex_notice_monitor.is_image_generation_only_promo(post))
        self.assertEqual(decision.route, "candidate")
        self.assertIn("ai_economic_value", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))
        self.assertIn("AI API 中转福利", reason)
        self.assertTrue("50" in reason or "0.075" in reason, reason)

    def test_public_ai_relay_site_with_model_facts_goes_to_digest_without_instant(self):
        post = self.make_post(
            "【推广】[DDShub] Claude/Codex/GLM 全部上线 欢迎老板尝试",
            "www.ddshub.cc Claude/Codex/GLM 全部上线，欢迎老板尝试。",
            tags=["推广"],
        )
        post.v2ex_url = "https://www.v2ex.com/t/1223697"

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("ai_relay_review", decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_domestic_access_wording_is_not_network_proxy_service(self):
        post = self.make_post(
            "【推广】[乐沃联 API 中转站] Codex / Claude Code 一键接入，学校企业可开票",
            "官网 https://api.lewolink.example ，一个 key，直连 Codex / Claude Code / OpenAI / Gemini / DeepSeek，"
            "不用翻墙，学校企业可开票。",
            tags=["推广"],
        )
        post.v2ex_url = "https://www.v2ex.com/t/1223706"

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertFalse(v2ex_notice_monitor.is_network_proxy_service_post(post))
        self.assertEqual(decision.route, "exclude")
        self.assertIn("ai_relay_review", decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_true_network_proxy_service_still_excluded(self):
        post = self.make_post(
            "【推广】Proxy001 纯净住宅 IP",
            "不限流量+不限并发，HTTP/SOCKS5 代理，住宅 IP，采集/社媒/防关联神器。",
            tags=["推广"],
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("network_proxy_service", decision.matched_categories)
        self.assertTrue(v2ex_notice_monitor.is_network_proxy_service_post(post))
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_pure_image_generation_site_without_unnegated_relay_value_stays_excluded(self):
        post = self.make_post(
            "【推广】AI 生图网站注册送 3 次生图",
            "生图站，提示词 gallery，注册免费生成图片 3 次，没有 API/Base URL/中转。",
            tags=["推广"],
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertTrue(v2ex_notice_monitor.is_image_generation_only_promo(post))
        self.assertEqual(decision.route, "exclude")
        self.assertIn("image_generation_promo", decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_vague_paid_ai_relay_without_review_facts_stays_excluded(self):
        post = self.make_post(
            "【推广】AI 中转站限时特惠",
            "中转站按需充值，小额使用，欢迎咨询。",
            tags=["推广"],
        )

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("paid_only_ai_relay_promo", decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_token_method_or_question_without_direct_benefit_stays_excluded(self):
        cases = [
            ("【分享发现】整理一些 bug team 蹬号方法渠道", "只有教程和渠道，没有注册链接、兑换码或具体领取额度。"),
            ("【问与答】怎么白嫖 AI token？", "想问问有没有靠谱方法。"),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=["分享发现"])
                offer_facts = v2ex_notice_monitor.build_offer_facts(v2ex_notice_monitor._post_blob(post))

                self.assertFalse(offer_facts.has_direct_benefit)
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_with_v2ex_user_free_balance_still_reaches_agent(self):
        body = (
            "官方站点： https://nexus.example/ 全站限时专属福利（无套路领取）："
            "V 站用户：注册账号后在本帖回复注册邮箱，或者添加官方客服微信，"
            "即可免费领取 $10 美元平台余额。"
            "新用户注册即可免费领取 1 天全站会员权益，会员每日自带 100 刀额度。"
        )
        post = self.make_post(
            "【推广】Nexus 满血 GPT5.5 API 中转｜拒绝降智阉割·长期稳定靠谱",
            body,
            tags=["推广"],
        )
        self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_codex_relay_with_registration_and_reply_dollar_gifts_reaches_model(self):
        post = self.make_post(
            "【推广】[月末活动] 一个真正一目了然的自建 Codex 中转站",
            "本站号池为 Plus/Pro，倍率均为 0.3。注册即送 2 刀，回帖留下邮箱，新用户再送 10 刀，老用户送 5 刀。QQ 群：1014477279。",
            tags=["推广"],
        )

        self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertFalse(v2ex_notice_monitor.is_overpriced_openai_subscription_promo(post))
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "candidate")
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_ai_relay_with_new_user_dollar_credit_reaches_model(self):
        post = self.make_post(
            "【推广】Codex API 中转站新用户福利",
            "支持 Codex API 和 OpenAI Base URL，新用户送 $5 credit，V 友回帖再送 3 USD。",
            tags=["推广"],
        )

        self.assertFalse(v2ex_notice_monitor.is_paid_only_ai_relay_promo(post))
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "candidate")
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_openai_subscription_price_extraction_ignores_contact_numbers_near_products(self):
        plus_blob = "ChatGPT Plus 可用，QQ:1097355337，欢迎进群。"
        pro_blob = "ChatGPT Pro 支持，群号：1097355337，微信客服联系。"

        self.assertEqual(v2ex_notice_monitor._extract_openai_subscription_prices(plus_blob, v2ex_notice_monitor.OPENAI_PLUS_PRODUCT_RE), [])
        self.assertEqual(v2ex_notice_monitor._extract_openai_subscription_prices(pro_blob, v2ex_notice_monitor.OPENAI_PRO_PRODUCT_RE), [])

    def test_openai_subscription_price_extraction_keeps_real_prices_near_contacts(self):
        blob = "ChatGPT Plus 95，ChatGPT Pro 1200，QQ 群 1097355337。"

        self.assertEqual(v2ex_notice_monitor._extract_openai_subscription_prices(blob, v2ex_notice_monitor.OPENAI_PLUS_PRODUCT_RE), [95.0])
        self.assertEqual(v2ex_notice_monitor._extract_openai_subscription_prices(blob, v2ex_notice_monitor.OPENAI_PRO_PRODUCT_RE), [1200.0])

    def test_codex_pool_with_low_usage_price_and_free_group_gets_instant_reason(self):
        post = self.make_post(
            "【推广】自用 Codex 站（今天继续福利分组--5.19）",
            "plus+free 号池（现基本都是 plus 号池，上新渠道），自用不了，推出来略微回收一下成本。"
            "模型支持有 GPT 全家桶 价格如下：按量付费：0.08 元/刀，随充随用，日/周不限。"
            "自己注册的号池，没有任何映射，无虚标不掺水。有问题及时处理，属实包退款。"
            "注册自动分免费订阅分组，免费订阅分组里有 50 个 plus 号，封完为止，V 友速蹬",
            tags=["推广"],
        )
        self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))
        reason = v2ex_notice_monitor.instant_alert_reason(post)
        self.assertIn("AI API 中转福利", reason)
        self.assertIn("0.08元", reason.replace(" ", ""))

    def test_multiple_instant_alert_posts_are_separate_messages(self):
        relay = self.make_post(
            "【推广】中转站这么赚钱，我也来开一家中转站来送福利",
            "AI API 中转站福利：前 18 名留注册邮箱送 18 刀，另有评论抽奖和充值赠额。",
            tags=["推广"],
        )
        relay.message_id = 2
        relay.v2ex_url = "https://www.v2ex.com/t/1213982"

        codex = self.make_post(
            "【推广】自用 Codex 站（今天继续福利分组--5.20）",
            "Codex/OpenAI 号池按量站，今天继续送出 1000 刀，留邮箱可参与，按量付费 0.08 元/刀。",
            tags=["推广"],
        )
        codex.message_id = 1
        codex.v2ex_url = "https://www.v2ex.com/t/1213981"

        messages = v2ex_notice_monitor.format_instant_alert_messages([relay, codex], [])
        self.assertEqual(len(messages), 2)
        self.assertIn("1213982", messages[0])
        self.assertNotIn("1213981", messages[0])
        self.assertIn("1213981", messages[1])
        self.assertNotIn("1213982", messages[1])
        self.assertNotIn("\n", messages[0])
        self.assertNotIn("\n", messages[1])

    def test_limited_free_tool_promo_is_archived_to_digest_not_instant(self):
        post = self.make_post(
            "【推广】[限时免费] windows 离线语音转文本、语音转文本、实时字幕工具",
            "完全本地处理与存储，不依赖网络；支持实时字幕、音视频文件识别、本地 LLM 润色翻译总结；普通话 97%，5.22 号之前免费。",
            tags=["推广"],
        )
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_offer_detail_note_preserves_v2ex_user_free_balance(self):
        note = v2ex_notice_monitor.format_offer_detail_note(
            "前面是很长的平台介绍。全站限时专属福利：V 站用户注册账号后回帖，"
            "即可免费领取 $10 美元平台余额；所有新用户注册即可免费领取 1 天会员权益。"
        )
        self.assertIn("V 站用户", note)
        self.assertIn("$10", note)
        self.assertIn("免费领取", note)

    def test_offer_detail_note_ignores_bare_accuracy_percentages(self):
        note = v2ex_notice_monitor.format_offer_detail_note(
            "语言支持：普通话（ 97%）、中文方言（ 90%）、英语（ 95%）、韩语（ 94%）。"
        )
        self.assertEqual(note, "")


    def test_cursor_related_posts_are_excluded_from_alerts_and_digest(self):
        cases = [
            ("【推广】Cursor 官方福利，新用户首月会员半价", "通过专属邀请链接注册并购买 Cursor 会员，Pro/Pro+/Ultra 首月 50% OFF。", ["推广"]),
            ("【分享创造】写了个 Cursor rules 管理工具", "支持导入 Cursor 规则、插件和工作流。", ["分享创造"]),
            ("【二手交易】出 Cursor Pro 账号", "Cursor 会员转让，明盘 50。", ["二手交易"]),
        ]
        for title, body, tags in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=tags)
                self.assertTrue(v2ex_notice_monitor.is_cursor_related_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_monitor_route_names_exclude_digest_and_model_layers(self):
        excluded = self.make_post(
            "【推广】转让闲置 VPS 年付套餐",
            "1C 1G 20G SSD，洛杉矶机房，明盘 99 元/年，剩余 10 个月。",
            tags=["推广"],
        )
        digest = self.make_post(
            "【分享创造】写了个菜单栏 OCR 小工具",
            "Mac App，支持截图 OCR 和翻译，限时免费。",
            tags=["分享创造"],
        )
        model = self.make_post(
            "【二手交易】出 iPhone 15 Pro 256G 明盘 3999",
            "全新未拆封，无锁，走闲鱼。",
            tags=["二手交易"],
        )

        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(excluded), "exclude")
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(digest), "exclude")
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(model), "candidate")

    def test_user_named_low_value_deal_categories_are_excluded(self):
        cases = [
            (
                "1216042",
                "【推广】香港众安银行开户返 300HKD+50USD 英伟达股票兑换券",
                "活动时间 5.28 日至 7.1 日截止。开户准备邀请码，完成入金任务后返现，可领取股票兑换券。",
                ["推广"],
            ),
            (
                "1215973",
                "【推广】国际阿里云/腾讯云|AWS/谷歌云 sm 账号 7 折充值",
                "云服务账号代充，AWS、GCP、谷歌云、阿里云、腾讯云账号充值和折扣。",
                ["推广"],
            ),
            (
                "1215949",
                "【二手交易】出两个实体 sim 卡，一个是 t-mobile，一个是 VOXI",
                "都是空卡，需要自己购买套餐激活，150 包邮。",
                ["二手交易"],
            ),
            (
                "1215938",
                "【二手交易】全新未拆封 PS5 游戏手柄无线",
                "PlayStation 5 无线控制器，专属 380，主机周边配件。",
                ["二手交易"],
            ),
            (
                "1215901",
                "【二手交易】5.27 刚开的 88VIP，出所有权益",
                "网易云 50，优酷/芒果 40，高德 10，飞猪/希尔顿/万豪每个 5。",
                ["二手交易"],
            ),
            (
                "1216819",
                "【推广】JibaoProxy 注册送 500M 动态住宅流量 静态住宅 2.94 刀起",
                "福利一：注册即送 500M 动态住宅流量。福利二：首次充值翻倍，充 20 送 20。"
                "折算下来等静态住宅 $2.94 起。注册链接：member.jibaoproxy.com",
                ["推广"],
            ),
        ]
        for topic_id, title, body, tags in cases:
            with self.subTest(topic_id=topic_id):
                post = self.make_post(title, body, tags=tags)
                post.v2ex_url = f"https://www.v2ex.com/t/{topic_id}"
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_network_proxy_service_semantic_variants_are_prefiltered(self):
        cases = [
            (
                "【推广】JibaoProxy 注册送 500M 动态住宅流量 静态住宅 2.94 刀起",
                "首次充值翻倍，代理服务注册链接。",
            ),
            (
                "【推广】住宅 IP 流量包首充翻倍",
                "动态住宅流量 10G，静态住宅 3 刀起，适合跨境电商和注册养号。",
            ),
            (
                "【推广】HTTP/SOCKS5 代理节点套餐",
                "注册送测试流量，节点订阅套餐可用。",
            ),
            (
                "【推广】海外代理联盟计划",
                "住宅流量、分佣、Affiliate、CDK 兑换码都有。",
            ),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body, tags=["推广"])
                decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)
                self.assertEqual(decision.route, "exclude")
                self.assertIn("network_proxy_service", decision.matched_categories)
                self.assertTrue(v2ex_notice_monitor.is_network_proxy_service_post(post))
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_network_proxy_rule_does_not_block_unrelated_network_or_api_value(self):
        cases = [
            self.make_post(
                "【二手交易】出 5G CPE 路由器",
                "明盘 300，支持公网 IP，包邮。",
                tags=["二手交易"],
            ),
            self.make_post(
                "【推广】OpenAI API 中转 V 友福利",
                "注册送 20 刀免费额度，Base URL 可接入。",
                tags=["推广"],
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                self.assertFalse(v2ex_notice_monitor.is_network_proxy_service_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "candidate")

    def test_cloud_service_account_recharge_is_excluded_with_title_only_context(self):
        post = self.make_post(
            "【推广】AWS/GCP/阿里云/腾讯云账号充值 7 折 sm 账号",
            "",
            tags=["推广"],
        )

        self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_new_exclusion_synonyms_are_covered_without_topic_body(self):
        cases = [
            ("【推广】港股券商开户礼，首入金送股票兑换券", ["推广"]),
            ("【推广】Cloudflare/UCloud 云账号代付充值券 7 折", ["推广"]),
            ("【二手交易】出 PayGo 保号套餐实体卡，可接码", ["二手交易"]),
            ("【二手交易】出 giffgaff 卡，余额 20 磅", ["二手交易"]),
            ("【二手交易】出 GiffGaff SIM 保号卡", ["二手交易"]),
            ("【二手交易】gg 卡，余额 20 磅", ["二手交易"]),
            ("【二手交易】出 Switch Joy-Con 和 DualSense 手柄", ["二手交易"]),
            ("【二手交易】出 88VIP 拆分权益包，饿了么盒马夸克天猫超市", ["二手交易"]),
            ("【二手交易】今天开山姆卡，出副卡 100", ["二手交易"]),
            ("【二手交易】出山姆会员亲友卡 100", ["二手交易"]),
            ("【二手交易】出 Sam's Club 副卡", ["二手交易"]),
            ("【二手交易】出山姆超市购物卡 95 折", ["二手交易"]),
        ]
        for title, tags in cases:
            with self.subTest(title=title):
                post = self.make_post(title, "", tags=tags)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_japanese_rakuten_esim_phone_cards_are_prefiltered(self):
        post = self.make_post(
            "【二手交易】300 元出一个日本乐天预付费 e-sim",
            "6000 日元买的，每年续费 5400 日元，需要支持海外 e-sim 的手机，国行 iphone 没法用。"
            "能接短信，漫游上网（每月 1g 海外流量，原生日本 ip），不能打电话发短信；用途：注册 claude/paypay。",
        )
        post.v2ex_url = "https://www.v2ex.com/t/1216221"

        self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
        self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_telecom_card_esim_variants_are_prefiltered(self):
        cases = [
            ("【二手交易】日本乐天预付费 e-sim", "可收短信，支持 PayPay 注册。"),
            ("【二手交易】出 Rakuten Mobile eSIM", "日本手机号，能接短信，套餐未激活。"),
            ("【二手交易】出 楽天モバイル SIM", "实体卡，原生日本 IP，可接短信。"),
            ("【二手交易】海外 e sim 可收短信", "预付费号码，接码注册服务可用。"),
            ("【二手交易】实体 sim 未激活", "需要自己购买套餐激活，包邮。"),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body)
                self.assertTrue(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_secondary_market_money_evidence_separates_specs_from_prices(self):
        spec_only = self.make_post(
            "【二手交易】iPhone 参数确认",
            "iPhone 16 Pro Max 256g，电池 91%，iPad mini 7 128g Wi-Fi。",
        )
        firm_price = self.make_post("【二手交易】红米 Note13", "手机 99.9 新，售价 600 元，包装齐全。")
        appraisal = self.make_post(
            "【二手交易】想出 iPhone 和 iPad",
            "iPhone 16 Pro Max 256g 成色很好，淘宝百亿换 17pm 可以抵 4850，iPad 爱回收估价 1810。",
        )

        spec_facts = v2ex_notice_monitor.build_post_facts(spec_only)
        firm_facts = v2ex_notice_monitor.build_post_facts(firm_price)
        appraisal_facts = v2ex_notice_monitor.build_post_facts(appraisal)

        self.assertEqual(spec_facts.price_evidence_kind, "")
        self.assertEqual(firm_facts.price_evidence_kind, "explicit_unit_price")
        self.assertEqual(appraisal_facts.price_evidence_kind, "appraisal_or_trade_in_price")

        title_price = self.make_post(
            "【二手交易】大僵 nano64g 标准套餐 1550；询价内存条 DDR5 16G*2",
            "自认为 95 新，支持上海杨浦自提，自提优惠 20。"
            "询价内存条 DDR5 16G*2 长鑫 6000Mhz CL36，可单卖一根 1200，上海杨浦可自提，自提优惠 50。",
        )
        title_price_facts = v2ex_notice_monitor.build_post_facts(title_price)
        self.assertIn(title_price_facts.price_evidence_kind, {"prefixed_bare_price", "device_bare_price"})
        self.assertIn("1550", title_price_facts.secondary_listing.money_evidence)
        self.assertIn("1200", title_price_facts.secondary_listing.money_evidence)
        self.assertNotIn("64", title_price_facts.secondary_listing.money_evidence)
        self.assertNotIn("16", title_price_facts.secondary_listing.money_evidence)
        self.assertNotIn("600", title_price_facts.secondary_listing.money_evidence)
        self.assertNotIn("6000", title_price_facts.secondary_listing.money_evidence)
        self.assertNotIn("95", title_price_facts.secondary_listing.money_evidence)
        self.assertNotIn("20", title_price_facts.secondary_listing.money_evidence)
        self.assertNotIn("50", title_price_facts.secondary_listing.money_evidence)

    def test_telecom_card_regex_does_not_match_simple_or_similar_words(self):
        cases = [
            self.make_post(
                "【分享创造】写了个 simple markdown 工具",
                "一个 simple but useful 的开源 CLI，只处理文本格式。",
                tags=["分享创造"],
            ),
            self.make_post(
                "【分享创造】similarity text diff 小工具",
                "计算文本 similarity 的本地命令行工具。",
                tags=["分享创造"],
            ),
            self.make_post("【问与答】这波 gg 了", "普通吐槽，不是交易。", tags=["问与答"]),
            self.make_post("【二手交易】出 Google gift card", "余额 50 美元，走闲鱼。"),
            self.make_post("【二手交易】出 Google Play 礼品卡", "余额 100，走闲鱼。"),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))

    def test_secondary_market_warning_posts_are_excluded_before_model(self):
        cases = [
            (
                "【二手交易】曝光 AI 拼车跑路",
                "V2EX ID、微信 ID、原帖链接都有，提醒大家避坑，退款维权中。",
            ),
            (
                "【二手交易】投诉闲鱼卖家不发货",
                "对方收款后拉黑，交易纠纷，提醒避坑。",
            ),
            (
                "【二手交易】避坑一个卖家",
                "骗子卖家，微信联系后不退款。",
            ),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body)
                self.assertTrue(v2ex_notice_monitor.is_secondary_market_warning_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_secondary_market_warning_regex_does_not_block_real_sale_titles(self):
        cases = [
            ("【二手交易】跑路价出 Mac mini M2 明盘 3500", "个人自用，走闲鱼。"),
            ("【二手交易】出显示器，曝光正常，明盘 2000", "个人自用，成色好。"),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                post = self.make_post(title, body)
                self.assertFalse(v2ex_notice_monitor.is_secondary_market_warning_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "candidate")

    def test_new_exclusion_rules_do_not_block_existing_high_value_categories(self):
        cases = [
            self.make_post("【二手交易】出 iPhone 15 Pro 256G 明盘 3999", "全新未拆封，无任何锁，非 MDM 设备。"),
            self.make_post("【二手交易】出 iPhone 15 Pro 美版无锁 256G 明盘 3999", "支持 eSIM，非卡贴，走闲鱼。"),
            self.make_post("【二手交易】出 Mac mini M2 16G 512G 主机 明盘 3500", "个人自用，剩余保修 6 个月，带原盒。"),
            self.make_post("【二手交易】出 Mac mini，偶尔玩游戏，可当客厅游戏主机", "M2 16G 512G，个人自用，走闲鱼。"),
            self.make_post("【二手交易】出两块东芝 16T 机械硬盘", "慢扫全绿，通电时间分别 39000+、28000+，还剩一年店保，打包可优惠。"),
            self.make_post("【二手交易】低价出两块东芝 16T 机械硬盘", "慢扫全绿，打包 900，圆你的 PT 大佬梦，V 友还可优惠。"),
            self.make_post("【二手交易】出冷门商超购物卡 8 折", "余额 500，走闲鱼，具体平台可核验。"),
            self.make_post("【二手交易】出飞猪旅行购物卡 8 折", "余额 500，正常闲置卡券，走闲鱼。"),
            self.make_post("【二手交易】出三星 Samsung S25 Ultra 12+512 明盘 6100", "国行无锁，个人自用，走闲鱼。"),
            self.make_post("【二手交易】出 Sam Altman 签名书 100", "个人收藏，明盘 100，走闲鱼。"),
            self.make_post(
                "【二手交易】试出一个 openai chatgpt pro 6 个月 激活码，开源所得",
                "明盘 6500，官方开源项目所得，微信 base64。",
            ),
            self.make_post(
                "【推广】Simple OpenAI API 中转 V 友福利",
                "注册送 20 刀免费额度，Base URL 可接入。",
                tags=["推广"],
            ),
            self.make_post(
                "【推广】OpenAI API 中转云平台账号福利",
                "注册账号送 $10 平台余额，低价体验套餐。",
                tags=["推广"],
            ),
            self.make_post(
                "【推广】OpenAI API 中转 V 友福利",
                "OpenAI API 中转，V 友注册送 20 刀免费额度，兑换码 V2EX，Base URL 可直接接入。",
                tags=["推广"],
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                self.assertFalse(v2ex_notice_monitor.is_blacklisted_marketplace_post(post))
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "candidate")
                self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_pure_membership_entitlement_cards_are_excluded(self):
        post = self.make_post("【二手交易】出希尔顿权益卡", "房券权益卡一张，个人闲置转让。")

        decision = v2ex_notice_monitor.evaluate_post_for_monitor(post)

        self.assertEqual(decision.route, "exclude")
        self.assertIn("membership_entitlement", decision.matched_categories)
        self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_technical_value_without_economic_value_is_excluded(self):
        cases = [
            self.make_post(
                "【分享创造】写了个 PostgreSQL cursor 分页库，开源",
                "GitHub repo: https://github.com/example/pg-cursor，Docker demo，支持 API。",
                tags=["分享创造"],
            ),
            self.make_post(
                "【Visual Studio Code】VSCode 1.122.0 可以离线使用 BYOK",
                "发现可以直接填自己的模型，不需要第三方插件，也不需要登录账户可以离线使用，详细发布说明 https://code.visualstudio.com/updates/v1_122。",
                tags=["Visual Studio Code"],
            ),
            self.make_post(
                "【OpenAI】好像没有人说 ChatGPT 账户登录的 Codex，不支持 GPT-5.3 Codex、GPT-5.4 等模型了",
                "今天用 GPT-5.3 Codex 模型返回 400，GPT-5.5 还能使用，但是 GPT-5.4 等模型不能用了。",
                tags=["OpenAI"],
            ),
            self.make_post(
                "【Kubernetes】把 10.8GB vLLM 镜像的 Pod Ready 从 4m35s 降到 14s： Hermes + SOCI lazy loading 实测",
                "我们做了一个小项目 Hermes：https://github.com/cloudpilot-ai/hermes ，controller 自动为镜像构建并缓存 SOCI index。",
                tags=["Kubernetes"],
            ),
            self.make_post(
                "【分享创造】开源许可证合规扫描 CLI",
                "用于 repo 合规检查，GitHub 开源，支持 Docker 和 CI。",
                tags=["分享创造"],
            ),
            self.make_post(
                "【分享创造】开源 MDM 管理平台",
                "GitHub repo，可自托管，提供 API 和 Docker compose。",
                tags=["分享创造"],
            ),
            self.make_post(
                "【分享创造】开源 web hosting benchmark 工具",
                "CLI 测试 hosting 性能，GitHub 开源，支持 Docker。",
                tags=["分享创造"],
            ),
            self.make_post(
                "【分享创造】做了一个开源 Agent 通知工具",
                "GitHub repo，前 100 名 V 友送 100 美元 credit，兑换码 V2EX100。",
                tags=["分享创造"],
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "exclude")
                self.assertFalse(v2ex_notice_monitor.is_instant_candidate(post))

    def test_economic_value_signals_bypass_soft_exclusions_to_model(self):
        cases = [
            self.make_post(
                "【宽带症候群】北京联通公网 IP 低价名额",
                "V 友兑换码 V2EXIP，10 元/月固定公网 IP，今天截止。",
                tags=["宽带症候群"],
            ),
            self.make_post(
                "【问与答】MacBook Air M4 6599 值不值得买？",
                "教育优惠叠加 800 券，官网今晚截止，朋友全新未拆北京可自提。",
                tags=["问与答"],
            ),
            self.make_post(
                "【推广】OpenAI 全家桶 Plus 月卡 88，Codex 按量站送福利",
                "Codex/OpenAI 号池 0.08 元/刀，V 友注册送 100 刀免费额度。",
                tags=["推广"],
            ),
            self.make_post(
                "【问与答】有没有 OpenAI API 免费额度领取渠道？",
                "官方活动链接 https://example.com/openai-credit ，V 友免费额度今天截止。",
                tags=["问与答"],
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                self.assertEqual(v2ex_notice_monitor.route_post_for_monitor(post), "candidate")
                self.assertTrue(v2ex_notice_monitor.is_instant_candidate(post))

    def test_topic_enrichment_keeps_potentially_valuable_soft_excluded_posts(self):
        cases = [
            self.make_post(
                "【宽带症候群】北京联通公网 IP 活动",
                "",
                tags=["宽带症候群"],
            ),
            self.make_post(
                "【分享创造】写了个 Cursor rules 工具",
                "",
                tags=["分享创造"],
            ),
        ]
        for post in cases:
            with self.subTest(title=post.title):
                self.assertTrue(v2ex_notice_monitor.topic_excerpt_enrichment_candidate(post))


class V2EXNoticeMonitorSanitizeTests(unittest.TestCase):

    def test_strip_tags_removes_invisible_unicode(self):
        self.assertEqual(v2ex_notice_monitor.strip_tags("<b>A\u200dB</b>"), "AB")


class V2EXNoticeMonitorPendingAlertTests(unittest.TestCase):
    def test_pending_alert_queue_pops_one_alert_at_a_time(self):
        state = {"pending_instant_alerts": [" first ", "", "\u200d", "second"]}

        self.assertEqual(v2ex_notice_monitor.pop_pending_instant_alert(state), "first")
        self.assertEqual(state, {"pending_instant_alerts": ["second"]})
        self.assertEqual(v2ex_notice_monitor.pop_pending_instant_alert(state), "second")
        self.assertEqual(state, {})

    def test_pending_alert_queue_appends_clean_values(self):
        state = {"pending_instant_alerts": ["existing"]}
        v2ex_notice_monitor.queue_pending_instant_alerts(state, ["", " new ", "\u200d"])
        self.assertEqual(state, {"pending_instant_alerts": ["existing", "new"]})


class V2EXNoticeMonitorModelBackendTests(unittest.TestCase):


    def test_agent_post_payload_includes_routing_context_and_listing_facts(self):
        title = "【二手交易】想出点东西，但是不知什么价合适，大佬们可以看看"
        tags, core_title = v2ex_notice_monitor.extract_tags_and_core(title)
        tags = tags or ["二手交易"]
        post = v2ex_notice_monitor.Post(
            message_id=1,
            telegram_url="https://t.me/v2ex_notice/1",
            v2ex_url="https://www.v2ex.com/t/1217170",
            posted_at="",
            title=title,
            tags=tags,
            core_title=core_title,
            rough_type=v2ex_notice_monitor.classify(tags, title, core_title),
            body_excerpt=(
                "iPhone 16 pro max 国际版，单实体+esim，256g，成色很好，电池 91%。"
                "淘宝百亿换 17pm 可以抵 4850。Apple watch ultra3 蜂窝版，爱回收估价才 3130。"
            ),
        )

        payload = v2ex_notice_monitor.agent_post_payload(post)

        self.assertIn("routing_context", payload)
        self.assertEqual(payload["routing_context"]["route"], "candidate")
        self.assertIn("secondary_market_sale", payload["routing_context"]["matched_categories"])
        self.assertIn("locality_facts", payload)
        self.assertEqual(payload["locality_facts"]["transaction_scope"], "not_local")
        self.assertIn("listing_facts", payload)
        self.assertEqual(payload["listing_facts"]["transaction_role"], "seller")
        self.assertEqual(payload["listing_facts"]["listing_maturity"], "appraisal")
        self.assertIn("iPhone", " ".join(payload["listing_facts"]["device_evidence"]))
        self.assertIn("eSIM", payload["listing_facts"]["capability_terms"])
        self.assertIn("offer_facts", payload)
        self.assertEqual(payload["offer_facts"]["intent"], "ambiguous")
        self.assertNotIn("\u200d", json.dumps(payload, ensure_ascii=False))

    def test_agent_post_payload_includes_lottery_facts(self):
        title = "【推广】抽 5 个 智友社的 GPTPLUS 稳定渠道成品号（满月测试稳定度 99% ）"
        tags, core_title = v2ex_notice_monitor.extract_tags_and_core(title)
        tags = tags or ["推广"]
        post = v2ex_notice_monitor.Post(
            message_id=1221165,
            telegram_url="https://t.me/v2ex_notice/1221165",
            v2ex_url="https://www.v2ex.com/t/1221165",
            posted_at="2026-06-17T11:59:54Z",
            title=title,
            tags=tags,
            core_title=core_title,
            rough_type=v2ex_notice_monitor.classify(tags, title, core_title),
            body_excerpt=(
                "开奖时间：20260622 22 时 00 分 00 秒。"
                "抽奖工具：用的 V 友 shoa 的开源项目：https://github.com/wcwac/v2ex-lottery ，"
                "在本帖回复参与。奖品：5 个智友社 GPTPLUS 稳定渠道成品号。"
            ),
        )

        payload = v2ex_notice_monitor.agent_post_payload(post)

        self.assertIn("lottery_facts", payload)
        self.assertTrue(payload["lottery_facts"]["is_lottery"])
        self.assertTrue(payload["lottery_facts"]["is_high_value"])
        self.assertTrue(payload["lottery_facts"]["has_actionable_entry"])
        self.assertIn("high_value_lottery", payload["routing_context"]["economic_value_types"])
        self.assertIn("GPTPLUS", " ".join(payload["lottery_facts"]["prize_evidence"]))
        self.assertIn("回复参与", " ".join(payload["lottery_facts"]["entry_evidence"]))


if __name__ == "__main__":
    unittest.main()
