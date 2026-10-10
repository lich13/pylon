import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
from email.message import Message


from tests.helpers import load_script_module as load_module

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = ROOT / "src/pylon/tasks/xianbao"


worker = load_module("xianbao_hermes_worker_test", "xianbao_hermes_worker.py")


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, content_type: str = "application/json"):
        self._body = body
        self.status = status
        self.code = status
        self.msg = "OK"
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._body

    def info(self):
        return self.headers


def load_rules():
    rules_path = ROOT / "config/xianbao-rules.json"
    return json.loads(rules_path.read_text(encoding="utf-8"))


class XianbaoClassificationTests(unittest.TestCase):
    def test_gray_candidate_uses_rules_without_loading_model_or_other_database(self):
        rules = load_rules()
        rules.setdefault("evaluation", {})["fetchBodyForCandidates"] = False
        post = {"id": "script-only-gray", "title": "京东猜大小", "body": "京东搜福利天天领632，猜大小红包", "url": "https://new.xianbao.fun/weibo/script-only-gray.html", "catename": "微博线报"}
        decision = worker.Decision(False, 0, 55, "weak-signal", review_candidate=True, evidence={})
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            db = worker.ensure_db(data_dir / "state.sqlite3")
            try:
                with (
                    patch.object(worker, "fetch_json_cached", return_value=([post], {})),
                    patch.object(worker, "classify", return_value=decision),
                    patch.object(worker, "push") as push,
                    patch.dict(sys.modules, {"xianbao_model_review": None, "openai": None}),
                    patch.dict(os.environ, {
                        "XIANBAO_MODEL_REVIEW_MAX_PER_POLL": "100",
                        "PYLON_OUTBOX": str(data_dir / "outbox.sqlite3"),
                    }),
                ):
                    sent = worker.process_once(rules, db, data_dir, True, False, False, 62)
                self.assertEqual(sent, 0)
                push.assert_not_called()
                self.assertFalse(decision.keep)
                self.assertEqual(decision.evidence["decision_engine"], "deterministic-v1")
                self.assertEqual(decision.evidence["rule_version"], worker.rules_hash(rules))
                self.assertNotIn("model_reviews", worker.LAST_POLL_SUMMARY)
                self.assertTrue((data_dir / "decisions.jsonl").exists())
                self.assertFalse((data_dir / "review_candidates.jsonl").exists())
            finally:
                db.close()

    def assert_review_candidate(self, decision, *, status: str | None = None):
        self.assertFalse(decision.keep)
        self.assertTrue(decision.review_candidate)
        if status:
            self.assertEqual(decision.review_status, status)
        self.assertFalse(worker.should_push_decision(decision, True, True, 62))
        self.assertIsInstance(decision.evidence, dict)


    def test_chatty_repayment_question_is_rejected_as_noise(self):
        rules = load_rules()
        post = {
            "id": "6342752",
            "title": "哥哥们，哪里有还款优惠呀",
            "url": "https://new.xianbao.fun/zuankeba/6342752.html",
            "catename": "赚客吧",
            "body": "哥哥们，哪里有还款优惠呀，求大佬指路",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "chatty-question-noise")

    def test_chatty_ecard_binding_problem_is_rejected_as_noise(self):
        rules = load_rules()
        post = {
            "id": "6338711",
            "title": "京东e卡绑定多了操作繁忙",
            "url": "https://new.xianbao.fun/zuankeba/6338711.html",
            "catename": "赚客吧",
            "body": "绑第50张就操作繁忙，多久能恢复，强子算是越来越恶心了",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "chatty-question-noise")

    def test_chatty_payment_question_without_path_is_rejected_as_noise(self):
        rules = load_rules()
        post = {
            "id": "chatty-repay-question",
            "title": "老哥们，支付宝还款哪里有优惠",
            "url": "https://new.xianbao.fun/zuankeba/chatty-repay-question.html",
            "catename": "赚客吧",
            "body": "老哥们，支付宝还款哪里有优惠，求推荐",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "chatty-question-noise")

    def test_chatty_ecard_recovery_question_is_rejected_as_noise(self):
        rules = load_rules()
        post = {
            "id": "chatty-ecard-recovery",
            "title": "京东E卡绑定提示操作繁忙，什么时候恢复",
            "url": "https://new.xianbao.fun/zuankeba/chatty-ecard-recovery.html",
            "catename": "赚客吧",
            "body": "京东E卡绑定提示操作繁忙，什么时候恢复，有知道的吗",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "chatty-question-noise")

    def test_chatty_question_filter_preserves_specific_bank_link_offer(self):
        rules = load_rules()
        post = {
            "id": "6284394",
            "title": "建行社保卡60-20 的链接哪里有",
            "url": "https://new.xianbao.fun/zuankeba/6284394.html",
            "catename": "赚客吧",
            "body": "建行社保卡60-20 的链接哪里有，社保卡支付优惠",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertTrue(decision.keep)
        self.assertNotEqual(decision.reason, "chatty-question-noise")

    def test_chatty_question_filter_preserves_salutation_with_specific_bank_offer(self):
        rules = load_rules()
        post = {
            "id": "salutation-specific-bank-offer",
            "title": "老哥们，建行社保卡60-20 的链接哪里有",
            "url": "https://new.xianbao.fun/zuankeba/salutation-specific-bank-offer.html",
            "catename": "赚客吧",
            "body": "老哥们，建行社保卡60-20 的链接哪里有，社保卡支付优惠",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertTrue(decision.keep)
        self.assertNotEqual(decision.reason, "chatty-question-noise")

    def test_chatty_question_filter_preserves_specific_bank_reward(self):
        rules = load_rules()
        post = {
            "id": "bank-sms-reward",
            "title": "工行信使有没有人玩 必中20-30吗",
            "url": "https://new.xianbao.fun/zuankeba/bank-sms-reward.html",
            "catename": "赚客吧",
            "body": "工行信使活动，必中20-30，入口在工行APP消息信使",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertTrue(decision.keep)
        self.assertNotEqual(decision.reason, "chatty-question-noise")

    def test_chatty_question_filter_preserves_self_contained_repayment_offer(self):
        rules = load_rules()
        post = {
            "id": "unionpay-repayment-offer",
            "title": "云闪付还款优惠 10-5",
            "url": "https://new.xianbao.fun/zuankeba/unionpay-repayment-offer.html",
            "catename": "赚客吧",
            "body": "云闪付还款优惠 10-5，可领，路径：云闪付-信用卡还款",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertTrue(decision.keep)
        self.assertNotEqual(decision.reason, "chatty-question-noise")

    def test_ysf_search_red_packet_is_normalized_as_unionpay_value(self):
        rules = load_rules()
        post = {
            "id": "ysf-return-gift",
            "title": "ysf，1元左右小毛",
            "url": "https://new.xianbao.fun/zuankeba/ysf-return-gift.html",
            "catename": "赚客吧",
            "body": "ysf 搜索有礼臻享家，页面右边老友回归礼，随机红包，1元左右。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertTrue(decision.keep or decision.review_candidate)
        self.assertNotEqual(decision.reason, "tiny-point-lottery-reward")
        self.assertIn("payment", decision.evidence.get("entity_types", []))
        identity = decision.evidence.get("activity_identity", {})
        self.assertEqual(identity.get("platform"), "云闪付")

    def test_personal_withdrawal_account_notice_is_rejected(self):
        rules = load_rules()
        post = {
            "id": "personal-withdrawal-notice",
            "title": "汇付天下尊敬的用户您好",
            "url": "https://new.xianbao.fun/xiaodigu/personal-withdrawal-notice.html",
            "catename": "小嘀咕",
            "body": "尊敬的用户您好，您尾号1234的账户提现9.40元至建设银行，资金已到账，请注意查收。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)
        self.assertEqual(decision.reason, "personal-transaction-notice")

    def test_broad_region_bank_digest_is_rejected_before_push(self):
        rules = load_rules()
        post = {
            "id": "ccb-many-region-digest",
            "title": "每月更新 符合去拿下哈 建行app如下搜 各地区有立减金可抽 每月1次",
            "url": "https://new.xianbao.fun/weibo/ccb-many-region-digest.html",
            "catename": "微博线报",
            "body": "建行APP各地区活动合集：江西、厦门、广东、广西、北京、浙江、湖北、河北、河南、山东、安徽等，搜建融家园可抽。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)
        self.assertEqual(decision.reason, "broad-region-digest-noise")

    def test_chatty_question_filter_preserves_mobile_pay_card_add_reward(self):
        rules = load_rules()
        post = {
            "id": "mobile-pay-card-add",
            "title": "Apple Pay 新增信用卡送10元立减金",
            "url": "https://new.xianbao.fun/zuankeba/mobile-pay-card-add.html",
            "catename": "赚客吧",
            "body": "Apple Pay 新增信用卡送10元立减金，打开钱包添加信用卡即可领",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertTrue(decision.keep)
        self.assertNotEqual(decision.reason, "chatty-question-noise")

    def test_personal_idle_rant_is_rejected_as_chat_noise(self):
        rules = load_rules()
        post = {
            "id": "6311108",
            "title": "今天0收入还倒贴",
            "url": "https://new.xianbao.fun/zuankeba/6311108.html",
            "catename": "赚客吧",
            "body": (
                "一个人都没住，还交了1700多的物业费\n"
                "打开赚吧撸毛，刚用京豆充了300话费点开一个京豆bug贴\n"
                "打开招行4元红包贴，我点开都是0.1\n"
                "打开京东618豆的贴，我点开是1豆\n"
                "今天拼多多也没保底1.2开福袋了"
            ),
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "personal-idle-rant")

    def test_payment_problem_question_is_rejected_as_qa_noise(self):
        rules = load_rules()
        post = {
            "id": "6310331",
            "title": "钱丢哪去了？",
            "url": "https://new.xianbao.fun/xinzuanba/6310331.html",
            "catename": "新赚吧",
            "body": (
                "请教一下！哪位大佬知道！中行数字币扫微信收款码15-10实付5付款扣款成功，"
                "微信没收到记录都没有，又试了扫一个0.1秒到微信。非试点地区。这钱卡哪了丢哪去了？"
            ),
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "payment-problem-question")

    def test_merchant_tax_discussion_is_rejected_as_noise(self):
        rules = load_rules()
        post = {
            "id": "6332755",
            "title": "实体店没开了，营业执照办的收款码一直在用。以后会不会收税的",
            "url": "https://new.xianbao.fun/zuankeba/6332755.html",
            "catename": "赚客吧",
            "body": (
                "实体店没开了，营业执照办的收款码一直在用。以后会不会收税的，"
                "这个是讨论税务和收款码。"
            ),
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "merchant-tax-discussion")

    def test_electric_bike_restock_with_ecard_rebate_is_rejected_as_product_noise(self):
        rules = load_rules()
        post = {
            "id": "6307427",
            "title": "雅迪无界E30plus2768电动车补货了",
            "url": "https://new.xianbao.fun/zuankeba/6307427.html",
            "catename": "赚客吧",
            "body": (
                "雅迪无解E30plus这款电摩在张大妈上的讨论好像比其他型号和2699的雅迪冠能T35-M还要多，"
                "我取消了2699的t35换了这款，昨天下单的了日期选的最后27号派送，这期间有更划算的电动车可以取消买其他的。"
                "还有莱战2599，台铃的萌啦1999赤兔2999，其他型号就没去了解了，雅迪这几款应该就是历史最低的，"
                "外省的朋友T35这款3000都想买，虽然线上的都是乞丐版但是市场价应该都是3000以上的。"
                "e30plus比t35升级了石墨烯电池，边撑感应，下坡自动刹车，座桶电子锁等方面，还是很划算的，"
                "但是这款抖音搜不到太多信息，搜出来基本都是e30旧款，T35搜出来的超级多视频。"
                "E卡下单前登记，晒单20现在好像被隐藏了，找客服问问"
            ),
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertIn(decision.reason, {"product-text", "product-arbitrage", "ecommerce-physical-arbitrage-noise"})

    def test_feedback_product_purchase_ecard_rebate_is_not_phone_push_or_gray_review(self):
        rules = load_rules()
        post = {
            "id": "feedback-blower-ecard",
            "title": "几块钱买两个吹风机 看图进页面加购这两个吹风机，一起买每个39.5，然后到货每个返...",
            "url": "https://new.xianbao.fun/zuankeba/feedback-blower-ecard.html",
            "catename": "赚客吧",
            "body": "几块钱买两个吹风机，看图进页面加购这两个吹风机，一起买每个39.5，然后到货每个返30e卡和每个好评返现5元京豆",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)
        self.assertFalse(worker.should_push_decision(decision, True, True, 62))
        self.assertIn(decision.reason, {"product-arbitrage", "ecommerce-physical-arbitrage-noise", "product-text"})

    def test_feedback_ineligible_bank_and_region_are_not_push_or_review(self):
        rules = load_rules()
        samples = [
            {
                "id": "feedback-fudian",
                "title": "富滇银行卡支付宝618立减能连续三天每天报名一次",
                "url": "https://new.xianbao.fun/xiaodigu/feedback-fudian.html",
                "catename": "小嘀咕",
                "body": "富滇银行卡支付宝618立减能连续三天每天报名一次，每次减3左右。",
                "expected_reason": "eligibility:unsupported-bank",
            },
            {
                "id": "feedback-neimeng",
                "title": "内蒙通用1元 小程序",
                "url": "https://new.xianbao.fun/xiaodigu/feedback-neimeng.html",
                "catename": "小嘀咕",
                "body": "内蒙通用1元 #小程序://工行服务/6HyG54q0QswCXph",
                "expected_reason": "region:other-region",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                expected_reason = str(post.pop("expected_reason"))
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(decision.keep)
                self.assertFalse(decision.review_candidate)
                self.assertEqual(decision.reason, expected_reason)
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_feedback_area_code_region_and_borrow_merchant_code_question_are_rejected(self):
        rules = load_rules()
        samples = [
            {
                "id": "feedback-020-gas",
                "title": "020工行9折加油券",
                "url": "https://new.xianbao.fun/zuankeba/feedback-020-gas.html",
                "catename": "赚客吧",
                "body": "rt",
                "expected_reason": "region:other-region",
            },
            {
                "id": "feedback-borrow-merchant-code",
                "title": "谁有商家码套一下zfb到店红包？6元一人一半",
                "url": "https://new.xianbao.fun/zuankeba/feedback-borrow-merchant-code.html",
                "catename": "赚客吧",
                "body": "谁有商家码套一下zfb到店红包？6元一人一半",
                "expected_reason": "borrow-merchant-code-question",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                expected_reason = str(post.pop("expected_reason"))
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(decision.keep)
                self.assertFalse(decision.review_candidate)
                self.assertEqual(decision.reason, expected_reason)
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_feedback_rules_do_not_block_national_self_service_offers(self):
        rules = load_rules()
        samples = [
            {
                "id": "national-gas",
                "title": "工行全国加油9折券 手机银行领取",
                "url": "https://new.xianbao.fun/zuankeba/national-gas.html",
                "catename": "赚客吧",
                "body": "工行全国加油9折券，手机银行领取，全国中石化可用。",
            },
            {
                "id": "official-store-red-packet",
                "title": "支付宝到店红包官方活动",
                "url": "https://new.xianbao.fun/zuankeba/official-store-red-packet.html",
                "catename": "赚客吧",
                "body": "支付宝到店红包官方活动，搜关键词领取，线下任意商户可核销，无需找别人商家码。",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertNotIn(decision.reason, {"region:other-region", "borrow-merchant-code-question"})

    def test_douban_personal_trade_listing_is_rejected_as_noise(self):
        rules = load_rules()
        post = {
            "id": "6315449",
            "title": "交易｜sleepwell褪黑素",
            "url": "https://new.xianbao.fun/douban-pinzu/6315449.html",
            "catename": "豆瓣线报-拼组",
            "body": "出2，93.2-18.6返利＝74.6\n74.6÷3=24.9＋闲鱼特惠寄",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertEqual(decision.reason, "soft-source-personal-trade-listing")

    def test_bank_points_balance_alert_reward_is_gray_review_not_push(self):
        rules = load_rules()
        post = {
            "id": "6404095",
            "title": "工行2000i豆",
            "url": "https://new.xianbao.fun/zuankeba/6404095.html",
            "catename": "赚客吧",
            "body": "αpp任务中心 余额变动提醒开通有礼 现在必中",
        }

        decision = worker.classify(post, rules, [], [])

        self.assert_review_candidate(decision, status="高分待复核")
        self.assertIn("银行/支付", decision.review_label)
        self.assertIn("bank", decision.evidence["entity_types"])
        self.assertIn("points", decision.evidence["benefit_types"])
        self.assertTrue(decision.evidence["body_confirmed"])

    def test_bank_lottery_water_posts_are_gray_review_not_push(self):
        rules = load_rules()
        samples = [
            {
                "id": "6406244",
                "title": "光大转盘毛 有水",
                "url": "https://new.xianbao.fun/zuankeba/6406244.html",
                "catename": "赚客吧",
                "body": "光大转盘毛，有水",
            },
            {
                "id": "6407074",
                "title": "小程序 //工行服务/i6ocxcYxiP1Q3ei有水",
                "url": "https://new.xianbao.fun/zuankeba/6407074.html",
                "catename": "赚客吧",
                "body": "#小程序://工行服务/i6ocxcYxiP1Q3ei 有水，速度",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assert_review_candidate(decision, status="高分待复核")
                self.assertIn("抽奖/转盘", decision.review_label)

    def test_jd_guess_size_red_packet_is_gray_review_not_push(self):
        rules = load_rules()
        post = {
            "id": "6407118",
            "title": "京东猜大小红包 最高膨胀10元",
            "url": "https://new.xianbao.fun/weibo/6407118.html",
            "catename": "微博线报-线报活动-京东",
            "body": "京东 APP 搜猜大小，10点猜大小最高膨胀10元红包。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assert_review_candidate(decision, status="疑似价值")
        self.assertIn("红包/券", decision.review_label)
        self.assertIn("red_packet", decision.evidence["benefit_types"])

    def test_plain_product_price_is_not_gray_review_candidate(self):
        rules = load_rules()
        post = {
            "id": "plain-product",
            "title": "普通牛奶24盒 39元",
            "url": "https://new.xianbao.fun/haodan/plain-product.html",
            "catename": "好单线报-食品-京东",
            "body": "普通牛奶24盒 39元，到手低价。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)

    def test_product_with_vip_price_is_not_gray_review_candidate(self):
        rules = load_rules()
        post = {
            "id": "dog-food-vip-price",
            "title": "领先烘焙鸭肉梨犬粮5.4kg 193元",
            "url": "https://new.xianbao.fun/haodan/dog-food-vip-price.html",
            "catename": "好单线报-宠物-淘宝|猫超",
            "body": '猫超 领先烘焙鸭肉梨犬粮5.4kg 右上角[ ]-点"淘金币 221元，88vip209元 金币193元',
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)

    def test_feedback_story_machine_rebate_ecard_is_product_noise(self):
        rules = load_rules()
        post = {
            "id": "feedback-story-machine-ecard",
            "title": "奇奇学牛津树故事机kipper 63元，奇奇学牛津树故事机kipper赠50e卡实付 113元",
            "url": "https://new.xianbao.fun/haodan/feedback-story-machine-ecard.html",
            "catename": "好单线报-娱乐-京东",
            "body": "📚速‼️奇奇学牛津树故事机 kipper💰63‼️好忦‼️ ⚠️贈50e卡，实符💰113 ⭕️plus领标题下200-25卷 ⭕️领无门槛 停留弹惊.喜卷 https://u.jd.com/76WYveX 拍 https://u.jd.com/71W6ySH 凑褪 https://u.jd.com/76WqMBu ⚠️kipper好忦速度‼️手慢🈚️啊啊啊啊啊",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)
        self.assertIn(decision.reason, {"product-arbitrage", "product-commodity", "product-text", "ecommerce-physical-arbitrage-noise"})
        self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_feedback_plain_clothing_discount_is_product_noise(self):
        rules = load_rules()
        post = {
            "id": "feedback-diss-shirt",
            "title": "DISS重磅纯棉短袖 18.8元",
            "url": "https://new.xianbao.fun/haodan/feedback-diss-shirt.html",
            "catename": "好单线报-服饰-淘宝",
            "body": "18.8元 神价 速度锁 DISS重磅纯棉短袖 https://uland.taobao.com/coupon/edetail?e=V2EJrZmOzR6lhHvvyUNXZfh8CuWt5YH5 精梳棉面料很透气，简约印花+宽松版型很有美式街头感，多安排几件换着穿。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)
        self.assertIn(decision.reason, {"product-arbitrage", "product-commodity", "product-text", "product-category", "ecommerce-physical-arbitrage-noise"})
        self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_source_platform_alone_does_not_make_membership_a_physical_product(self):
        self.assertFalse(
            worker.physical_product_dominance(
                "腾讯视频会员月卡 0.01 抽",
                "好单线报-线报活动-淘宝",
            )
        )

    def test_product_with_attached_platform_rewards_is_not_kept(self):
        rules = load_rules()
        samples = [
            {
                "id": "product-ecards",
                "title": "学习机实付399赠100京东卡",
                "catename": "好单线报-数码-京东",
                "body": "京东下单学习机，实付399元，晒单赠100京东卡。",
            },
            {
                "id": "product-coupon-red-packet",
                "title": "洗衣液凑单用微信红包券到手9.9元",
                "catename": "好单线报-家清-京东",
                "body": "洗衣液凑单，下单用微信红包券，实付9.9元到手。",
            },
            {
                "id": "product-member-price",
                "title": "美妆精华88会员价49元",
                "catename": "好单线报-美妆-淘宝",
                "body": "淘宝88会员价，美妆精华49元，下单好价。",
            },
            {
                "id": "product-health-unit-spec",
                "title": "GNC健安喜97%高纯度epa鱼油omega3dha 60粒/盒 42.6元",
                "catename": "微博线报-健康-京东",
                "body": "京东健康商品，鱼油60粒/盒，券后42.6元。",
            },
            {
                "id": "product-device-price",
                "title": "京东AI动态血糖仪 88元",
                "catename": "好单线报-线报活动-京东|整点",
                "body": "京东抢血糖仪，价格88元就是对的，前1万名免单。",
            },
            {
                "id": "product-shopping-ecard-draw",
                "title": "a+b下单抽50e卡和豆子",
                "catename": "微博线报-线报活动-京东",
                "body": "京东a+b下单抽50e卡和豆子，先下单再抽。",
            },
            {
                "id": "product-reward-without-price",
                "title": "楼上血糖仪可用鸿包 E卡等等 最终都是给88",
                "catename": "微博线报-线报活动-京东",
                "body": "楼上血糖仪可用红包 E卡等等，最终都是给88，20点京东超市专场留意红包雨。",
            },
            {
                "id": "product-shopping-red-packet-offset",
                "title": "红包 随领随用 妙洁 抵扣红包后 买200付30左右 妙洁抗菌保鲜袋",
                "catename": "微博线报-线报活动-京东",
                "body": "京东妙洁保鲜袋用随领红包抵扣，买200付30左右，按需下单。",
            },
            {
                "id": "product-filter-coupon-brush",
                "title": "感觉算好价的复旦申花水龙头过滤器 需刷券",
                "catename": "豆瓣线报-买组",
                "body": "复旦申花水龙头过滤器，刷券后价格可以，商品好价。",
            },
            {
                "id": "product-microwave-rebate",
                "title": "昨天下单微波炉 今天凑单到更便宜 限时141 返10e卡的",
                "catename": "微博超话",
                "body": "微波炉凑单更便宜，下单后返10e卡，仍是实物购物返卡。",
            },
            {
                "id": "feedback-microwave-short-rebate",
                "title": "微波炉直接搜限时50 不用凑126🍎反10e卡",
                "catename": "微博超话",
                "body": "微波炉直接搜限时50，不用凑126，返10e卡",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(decision.keep)
                self.assertFalse(decision.review_candidate)
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_quantity_grocery_purchase_note_is_product_noise(self):
        rules = load_rules()
        post = {
            "id": "feedback-strawberry-drink",
            "title": "昨天姐妹的6瓶草莓小甜水作业可以买8瓶的也很合适 憧憬",
            "catename": "微博超话",
            "url": "https://new.xianbao.fun/weibo/feedback-strawberry-drink.html",
            "body": "昨天姐妹的6瓶草莓小甜水作业可以买8瓶的也很合适，按作业下单就是饮料到手价。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertFalse(decision.review_candidate)
        self.assertIn(
            decision.reason,
            {
                "product-arbitrage",
                "product-commodity",
                "dining-grocery-coupon-noise",
                "ecommerce-physical-arbitrage-noise",
                "soft-source-commodity-rebate-noise",
            },
        )
        self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_feedback_positive_bank_payment_samples_are_promoted_or_high_review(self):
        rules = load_rules()
        samples = [
            {
                "id": "feedback-icbc-service-water",
                "title": "小程序 //工行服务/i6ocxcYxiP1Q3ei有水",
                "url": "https://new.xianbao.fun/zuankeba/feedback-icbc-service-water.html",
                "catename": "赚客吧",
                "body": "#小程序://工行服务/i6ocxcYxiP1Q3ei 有水，工行服务小程序抽立减金，速度",
            },
            {
                "id": "feedback-abc-alipay",
                "title": "老农 支付宝",
                "url": "https://new.xianbao.fun/zuankeba/feedback-abc-alipay.html",
                "catename": "赚客吧",
                "body": "农业银行支付宝活动，APP 搜索支付宝，抽支付红包，有水可领",
            },
            {
                "id": "feedback-comm-live",
                "title": "交行直播福袋",
                "url": "https://new.xianbao.fun/zuankeba/feedback-comm-live.html",
                "catename": "赚客吧",
                "body": "交通银行买单吧直播福袋，直播间抽贴金券和支付券，当前有水",
            },
            {
                "id": "feedback-ceb-150",
                "title": "光大速度150字",
                "url": "https://new.xianbao.fun/zuankeba/feedback-ceb-150.html",
                "catename": "赚客吧",
                "body": "光大银行活动，按要求输入150字，抽刷卡金，刚中，有水",
            },
            {
                "id": "feedback-comm-cloud-coupon",
                "title": "交行 云网券 1~8.8",
                "url": "https://new.xianbao.fun/zuankeba/feedback-comm-cloud-coupon.html",
                "catename": "赚客吧",
                "body": "交通银行云网支付券 1~8.8，买单吧入口可抽，当前有水",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertTrue(
                    decision.keep or decision.review_candidate,
                    f"{post['title']} should be pushed or high-priority review, got {decision.reason}",
                )
                if decision.review_candidate:
                    self.assertIn(decision.review_status, {"高分待复核", "疑似价值"})

    def test_feedback_negative_samples_are_not_phone_pushes(self):
        rules = load_rules()
        samples = [
            {
                "id": "feedback-gas",
                "title": "刚需石化加油的来",
                "url": "https://new.xianbao.fun/zuankeba/feedback-gas.html",
                "catename": "赚客吧",
                "body": "中石化加油优惠，满200随机减，刚需可上",
            },
            {
                "id": "feedback-cloud-pay",
                "title": "云缴费还有",
                "url": "https://new.xianbao.fun/zuankeba/feedback-cloud-pay.html",
                "catename": "赚客吧",
                "body": "云缴费还有优惠，生活缴费随机减",
            },
            {
                "id": "feedback-fake-shoes",
                "title": "新百伦鞋子 69元",
                "url": "https://new.xianbao.fun/haodan/feedback-fake-shoes.html",
                "catename": "好单线报-服饰-淘宝",
                "body": "新百伦鞋子 69元，非官方渠道，疑似假鞋，低价下单",
            },
            {
                "id": "feedback-vague-chat",
                "title": "大毛，有能力的上。能不能撸你说了算",
                "url": "https://new.xianbao.fun/zuankeba/feedback-vague-chat.html",
                "catename": "赚客吧",
                "body": "大毛，有能力的上。能不能撸你说了算，没路径没入口没金额",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_non_mobile_utility_bill_feedback_is_rejected_not_gray(self):
        rules = load_rules()
        samples = [
            {
                "id": "feedback-sgcc-traffic-bank",
                "title": "网上国网app-交电.费 选择云闪.付-切交.行App付歀",
                "url": "https://new.xianbao.fun/weibo/feedback-sgcc-traffic-bank.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "网上国网APP交电费，选择云闪付切交通银行APP付款，满8元随机减0.1-5元，月限4次。",
            },
            {
                "id": "feedback-ccb-bill-pay",
                "title": "建`行缴费抽2 5亓立减J 建`行APP搜 缴费小美好 冲水电燃气費满30亓",
                "url": "https://new.xianbao.fun/weibo/feedback-ccb-bill-pay.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "建行APP搜缴费小美好，冲水电燃气费满30元，可抽2-5元立减金。",
            },
            {
                "id": "feedback-cloud-bill-pay",
                "title": "云缴费APP缴费50元立减6.18元",
                "url": "https://new.xianbao.fun/zuankeba/feedback-cloud-bill-pay.html",
                "catename": "赚客吧",
                "body": "云缴费APP生活缴费50元立减6.18元，目前还有名额。",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(decision.keep)
                self.assertFalse(decision.review_candidate)
                self.assertEqual(decision.reason, "non-mobile-utility-bill-noise")
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_mobile_topup_is_not_blocked_by_non_mobile_utility_bill_rule(self):
        rules = load_rules()
        post = {
            "id": "mobile-topup-coupon",
            "title": "移动话费券口令",
            "url": "https://new.xianbao.fun/weibo/mobile-topup-coupon.html",
            "catename": "微博线报-线报活动-其他活动",
            "body": "中国移动APP底部菜单灵犀发送口令，试试领话费券，当前可领。",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertNotEqual(decision.reason, "non-mobile-utility-bill-noise")
        self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_mobile_topup_poor_discount_is_rejected_not_gray(self):
        rules = load_rules()
        samples = [
            {
                "id": "mobile-topup-95-for-100",
                "title": "移动话费95充100",
                "url": "https://new.xianbao.fun/weibo/mobile-topup-95-for-100.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "中国移动话费95充100，当前可充值。",
            },
            {
                "id": "mobile-topup-95-percent",
                "title": "联通话费9.5折充100",
                "url": "https://new.xianbao.fun/weibo/mobile-topup-95-percent.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "联通手机充值9.5折充100元。",
            },
            {
                "id": "mobile-topup-no-discount",
                "title": "电信话费100充100",
                "url": "https://new.xianbao.fun/weibo/mobile-topup-no-discount.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "电信手机充值100充100，无额外优惠。",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(decision.keep)
                self.assertFalse(decision.review_candidate)
                self.assertEqual(decision.reason, "mobile-topup-poor-discount-noise")
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_mobile_topup_nine_discount_boundary_is_not_poor_discount(self):
        rules = load_rules()
        samples = [
            {
                "id": "mobile-topup-5-for-10",
                "title": "京东话费5充10 当前可用",
                "url": "https://new.xianbao.fun/weibo/mobile-topup-5-for-10.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "京东话费5充10，当前可充值，未抢光。",
            },
            {
                "id": "mobile-topup-90-percent",
                "title": "移动话费9折充100",
                "url": "https://new.xianbao.fun/weibo/mobile-topup-90-percent.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "中国移动手机充值9折充100元，当前可用。",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertNotEqual(decision.reason, "mobile-topup-poor-discount-noise")

    def test_airline_mileage_enters_high_priority_review(self):
        rules = load_rules()
        post = {
            "id": "feedback-csair-mileage",
            "title": "南航 三个可抽里程",
            "url": "https://new.xianbao.fun/weibo/feedback-csair-mileage.html",
            "catename": "微博线报-线报活动-其他活动",
            "body": "南航三个入口可抽里程 ①mp://9yjuk94KSoVHKJJ ②mp://wp2e4xeuPhFXett ③mp://LnkcwvqaUwITeQn",
        }

        decision = worker.classify(post, rules, [], [])

        self.assertFalse(decision.keep)
        self.assertTrue(decision.review_candidate)
        self.assertEqual(decision.review_status, "高分待复核")
        self.assertEqual(decision.reason, "airline-mileage-high-review")
        self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_low_value_watch_minutes_and_banner_tasks_are_rejected(self):
        rules = load_rules()
        samples = [
            {
                "id": "feedback-tencent-watch-membership",
                "title": "腾讯云包场 ⏰18:30 看30分钟 免费领30天会员",
                "url": "https://new.xianbao.fun/weibo/feedback-tencent-watch-membership.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "腾讯云包场活动，18:30 开始，看 30 分钟可免费领 30 天会员。",
            },
            {
                "id": "feedback-rocket-banner",
                "title": "小程序 红色火箭 13点中间第二个横幅",
                "url": "https://new.xianbao.fun/weibo/feedback-rocket-banner.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "小程序红色火箭，13 点点中间第二个横幅，按页面任务参与。",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(decision.keep)
                self.assertFalse(decision.review_candidate)
                self.assertEqual(decision.reason, "low-value-promo-task-noise")
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_shopping_survey_reward_is_rejected_not_reviewable(self):
        rules = load_rules()
        samples = [
            {
                "id": "feedback-soy-sauce-survey",
                "title": "酱油蚝油人群问卷 礼金20元",
                "url": "https://new.xianbao.fun/xiaodigu/6459894.html",
                "catename": "小嘀咕",
                "body": (
                    "酱油蚝油人群问卷 礼金20元 参与条件：城市：北京、上海、广州、青岛、南宁、武汉、沧州、邯郸、阜阳、安庆。"
                    "品类购买决策与使用人，非以上城市人群勿做。每周做饭不少于2次，买过普通酱油/蚝油。"
                    "小红书用户，问卷要上传：提供购买凭证/使用实拍图/小红书主页截图（带ID）。48小时红包审核发放到微信服务通知。"
                ),
            },
            {
                "id": "feedback-category-consumer-recruit",
                "title": "消费人群调研 30元红包",
                "url": "https://new.xianbao.fun/xiaodigu/category-consumer-recruit.html",
                "catename": "小嘀咕",
                "body": (
                    "招募经常购买母婴用品的人群，需填写调研问卷，上传购物订单截图和小红书主页截图，"
                    "审核通过后发放30元微信红包。"
                ),
            },
            {
                "id": "feedback-title-only-survey-reward",
                "title": "酱油蚝油人群问卷 礼金20元",
                "url": "https://new.xianbao.fun/xiaodigu/title-only-survey-reward.html",
                "catename": "小嘀咕",
            },
            {
                "id": "feedback-jd-shopping-survey-reward",
                "title": "京东购物人群调研 礼金20元",
                "url": "https://new.xianbao.fun/xiaodigu/jd-shopping-survey-reward.html",
                "catename": "小嘀咕",
                "body": "京东购物用户调研，需提交订单截图和购买凭证，审核通过后发放20元微信红包。",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertFalse(decision.keep)
                self.assertFalse(decision.review_candidate)
                self.assertEqual(decision.reason, "shopping-survey-reward-noise")
                self.assertFalse(worker.should_push_decision(decision, True, True, 62))

    def test_bank_payment_questionnaire_value_path_is_not_suppressed_as_shopping_survey(self):
        rules = load_rules()
        samples = [
            {
                "id": "bank-questionnaire-value-path",
                "title": "工行服务小程序问卷抽立减金有水",
                "url": "https://new.xianbao.fun/weibo/bank-questionnaire-value-path.html",
                "catename": "微博线报-线报活动-其他活动",
                "body": "工行服务小程序，完成活动问卷后抽微信立减金，入口清楚，当前有水。",
            },
            {
                "id": "jd-guess-questionnaire-value-path",
                "title": "京东福利天天领632 猜大小红包加码",
                "url": "https://new.xianbao.fun/weibo/jd-guess-questionnaire-value-path.html",
                "catename": "微博线报-线报活动-京东",
                "body": "京东APP搜福利天天领632，猜大小赢红包加码，当前有水。",
            },
        ]

        for post in samples:
            with self.subTest(post["id"]):
                decision = worker.classify(post, rules, [], [])
                self.assertNotEqual(decision.reason, "shopping-survey-reward-noise")
                self.assertTrue(decision.keep or decision.review_candidate)
                self.assertFalse(
                    decision.review_candidate and decision.reason in {"shopping-survey-reward-noise", "survey-only-low-value"}
                )

    def test_activity_identity_exact_and_loose_duplicate_fingerprints(self):
        first = {
            "id": "a",
            "title": "招行 红包2000份，手慢无(新)",
            "catename": "赚客吧",
            "body": "招商银行 APP 红包2000份，入口首页，今天可领",
        }
        same = {
            "id": "b",
            "title": "招行 红包2000份，手慢无",
            "catename": "赚客吧",
            "body": "招商银行 APP 红包2000份，入口首页，今天可领",
        }
        ambiguous = {
            "id": "c",
            "title": "招行 红包 2个",
            "catename": "赚客吧",
            "body": "招商银行红包 2 个，速度",
        }

        a = worker.extract_activity_identity(first)
        b = worker.extract_activity_identity(same)
        c = worker.extract_activity_identity(ambiguous)

        self.assertEqual(a["exact_identity_fp"], b["exact_identity_fp"])
        self.assertNotEqual(a["exact_identity_fp"], c["exact_identity_fp"])
        self.assertEqual(a["loose_identity_fp"], c["loose_identity_fp"])

    def test_jd_guess_size_variants_share_activity_identity(self):
        first = {
            "id": "jd-a",
            "title": "猜大小赢加码 见好就收 不要贪 小号可京东搜 福利天天领632",
            "catename": "微博线报-线报活动-京东",
            "body": "京东 APP 搜 福利天天领632，猜大小赢加码红包，10点开始",
        }
        second = {
            "id": "jd-b",
            "title": "赢了没 猜大小赢加码 见好就收 不要贪",
            "catename": "微博线报-线报活动-京东",
            "body": "京东猜大小赢加码，福利天天领632 红包活动",
        }

        self.assertEqual(
            worker.extract_activity_identity(first)["exact_identity_fp"],
            worker.extract_activity_identity(second)["exact_identity_fp"],
        )

    def test_activity_duplicate_cache_suppresses_repeat_without_material_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            try:
                first = {
                    "id": "jd-a",
                    "title": "猜大小赢加码 见好就收 不要贪 小号可京东搜 福利天天领632",
                    "url": "https://new.xianbao.fun/weibo/jd-a.html",
                    "catename": "微博线报-线报活动-京东",
                    "body": "京东 APP 搜 福利天天领632，猜大小赢加码红包，10点开始",
                }
                repeat = {
                    "id": "jd-b",
                    "title": "赢了没 猜大小赢加码 见好就收 不要贪",
                    "url": "https://new.xianbao.fun/weibo/jd-b.html",
                    "catename": "微博线报-线报活动-京东",
                    "body": "京东猜大小赢加码，福利天天领632 红包活动",
                }
                changed = {
                    "id": "jd-c",
                    "title": "猜大小赢加码补了20元红包",
                    "url": "https://new.xianbao.fun/weibo/jd-c.html",
                    "catename": "微博线报-线报活动-京东",
                    "body": "京东 APP 搜 福利天天领632，猜大小补了20元红包，新金额",
                }
                decision = worker.Decision(True, 2, 70, "test", value_label="test")
                worker.record_activity_observation(db, first, decision, pushed=True)

                duplicate = worker.activity_duplicate_check(db, repeat)
                material_change = worker.activity_duplicate_check(db, changed)

                self.assertTrue(duplicate["duplicate"])
                self.assertFalse(duplicate["material_change"])
                self.assertEqual(duplicate["duplicate_of"], "jd-a")
                self.assertFalse(material_change["duplicate"])
                self.assertTrue(material_change["material_change"])
                self.assertIn("新金额", material_change["material_change_reason_cn"])
            finally:
                db.close()

    def test_feedback_ccb_open_box_variants_are_strict_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            try:
                first = {
                    "id": "ccb-box-a",
                    "title": "建.行 开宝箱 反馈3/5ljj 非bi中 2⃣️建.行建融家园 每月可抽1次",
                    "url": "https://new.xianbao.fun/weibo/ccb-box-a.html",
                    "catename": "微博线报-线报活动-其他活动",
                    "body": "建.行 开宝箱 反馈3/5立减金 非必中，建行建融家园每月可抽1次，抽1～168立减金。",
                }
                repeat_time = {
                    "id": "ccb-box-b",
                    "title": "建.行-开宝箱 部分有新任务 自测哈 现在有中10ljj 看自己开 1点",
                    "url": "https://new.xianbao.fun/weibo/ccb-box-b.html",
                    "catename": "微博线报-线报活动-其他活动|整点",
                    "body": "建行开宝箱，部分有新任务，1点自测，中10立减金。",
                }
                worker.record_activity_observation(db, first, worker.Decision(True, 2, 90, "test"), pushed=True)

                duplicate = worker.activity_duplicate_check(db, repeat_time)

                self.assertTrue(duplicate["duplicate"])
                self.assertFalse(duplicate["material_change"])
                self.assertEqual(duplicate["duplicate_of"], "ccb-box-a")
            finally:
                db.close()

    def test_feedback_alipay_icbc_receipt_variants_are_strict_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            try:
                first = {
                    "id": "icbc-receipt-a",
                    "title": "吱咐寳 大小号发起收 寛 工行2000-3.88 2 次",
                    "url": "https://new.xianbao.fun/weibo/icbc-receipt-a.html",
                    "catename": "微博线报-线报活动-其他活动",
                    "body": "支付宝大小号发起收款，工行2000-3.88，2次。",
                }
                repeat = {
                    "id": "icbc-receipt-b",
                    "title": "zfb大小号发起收歀，工行2000-3.88 三次",
                    "url": "https://new.xianbao.fun/zuankeba/icbc-receipt-b.html",
                    "catename": "赚客吧",
                    "body": "支付宝大小号发起收款，工行2000-3.88，三次。",
                }
                worker.record_activity_observation(db, first, worker.Decision(True, 2, 90, "test"), pushed=True)

                duplicate = worker.activity_duplicate_check(db, repeat)

                self.assertTrue(duplicate["duplicate"])
                self.assertFalse(duplicate["material_change"])
                self.assertEqual(duplicate["duplicate_of"], "icbc-receipt-a")
            finally:
                db.close()

    def test_product_noise_identity_does_not_create_exact_activity_or_material_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            try:
                first = {
                    "id": "paper-a",
                    "title": "洁云绒触感抽纸100抽×30包 32.1元",
                    "url": "https://new.xianbao.fun/haodan/paper-a.html",
                    "catename": "好单线报-食品-京东",
                    "body": "洁云绒触感抽纸100抽×30包 32.1元，到手好价",
                }
                changed_price = {
                    "id": "paper-b",
                    "title": "洁云绒触感抽纸100抽×30包 29.9元",
                    "url": "https://new.xianbao.fun/haodan/paper-b.html",
                    "catename": "好单线报-食品-京东",
                    "body": "洁云绒触感抽纸100抽×30包 29.9元，到手好价",
                }
                identity = worker.extract_activity_identity(first)
                self.assertTrue(identity["noise_fp"])
                self.assertFalse(identity["exact_identity_fp"])

                worker.record_activity_observation(db, first, worker.Decision(False, 0, 0, "product-category"))
                self.assertEqual(
                    db.execute("select count(*) from activity_observations").fetchone()[0],
                    0,
                )
                duplicate = worker.activity_duplicate_check(db, changed_price)
                self.assertFalse(duplicate["duplicate"])
                self.assertFalse(duplicate["material_change"])
            finally:
                db.close()


class XianbaoFetchJsonTests(unittest.TestCase):
    def test_json_text_and_redirects_ignore_environment_and_legacy_proxies(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("X-Xianbao-Proxy-Mode")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/data")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'[{"id": "direct"}]')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {key: "http://127.0.0.1:1" for key in (
            "http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "XIANBAO_SOURCE_PROXY",
        )}
        env.update({"no_proxy": "", "NO_PROXY": "", "XIANBAO_SOURCE_PROXY_MODE": "always", "XIANBAO_FETCH_JSON_RETRIES": "1"})
        try:
            url = f"http://127.0.0.1:{server.server_port}/redirect"
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(worker.fetch_json(url, timeout=2), [{"id": "direct"}])
                self.assertIn('"direct"', worker.fetch_text(url, timeout=2))
            self.assertEqual(requests, [("/redirect", None), ("/data", None)] * 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_https_source_never_loads_environment_proxy_settings(self):
        req = worker.urllib.request.Request("https://new.xianbao.fun/plus/json/push.json")
        with patch.object(worker.urllib.request, "getproxies", side_effect=AssertionError("environment proxy lookup")), patch.object(
            worker.urllib.request.HTTPSHandler, "https_open", return_value=FakeResponse(b"[]")
        ) as https_open:
            with worker.source_urlopen(req, timeout=2) as response:
                self.assertEqual(response.read(), b"[]")
        https_open.assert_called_once()

    def test_fetch_json_retries_empty_body_then_recovers(self):
        responses = [
            FakeResponse(b"", content_type="application/json"),
            FakeResponse(b'[{"id": "ok", "title": "ok"}]', content_type="application/json"),
        ]
        sleeps = []

        def fake_urlopen(req, timeout):
            return responses.pop(0)

        env = {
            "XIANBAO_FETCH_JSON_RETRIES": "2",
            "XIANBAO_FETCH_JSON_RETRY_BACKOFF_SECONDS": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(worker, "source_urlopen", side_effect=fake_urlopen) as urlopen, patch(
            worker.__name__ + ".time.sleep", side_effect=lambda value: sleeps.append(value)
        ):
            payload = worker.fetch_json("https://new.xianbao.fun/plus/json/push.json")

        self.assertEqual(payload, [{"id": "ok", "title": "ok"}])
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleeps, [])

    def test_fetch_json_invalid_response_raises_contextual_retryable_error(self):
        def fake_urlopen(req, timeout):
            return FakeResponse(b"<html>bad gateway</html>", status=200, content_type="text/html")

        env = {
            "XIANBAO_FETCH_JSON_RETRIES": "2",
            "XIANBAO_FETCH_JSON_RETRY_BACKOFF_SECONDS": "0",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(worker, "source_urlopen", side_effect=fake_urlopen), patch(
            worker.__name__ + ".time.sleep", side_effect=lambda value: None
        ):
            with self.assertRaises(worker.FetchJsonDecodeError) as ctx:
                worker.fetch_json("https://new.xianbao.fun/plus/json/push.json")

        message = str(ctx.exception)
        self.assertIn("invalid json response", message)
        self.assertIn("status=200", message)
        self.assertIn("content_type=text/html", message)
        self.assertIn("bad gateway", message)
        self.assertTrue(worker.transient_fetch_error(ctx.exception))

    def test_fetch_json_cached_returns_empty_source_unavailable_on_url_timeout_without_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            timeout_error = worker.urllib.error.URLError("timed out")
            with patch.object(worker, "fetch_json", side_effect=timeout_error):
                posts, fallback = worker.fetch_json_cached(db, "https://new.xianbao.fun/plus/json/push.json")

        self.assertEqual(posts, [])
        self.assertTrue(fallback["poll_json_source_unavailable"])
        self.assertEqual(fallback["poll_json_source_error_type"], "URLError")
        self.assertIn("timed out", fallback["poll_json_source_error"])

    def test_fetch_json_cached_uses_stale_cache_on_url_timeout_after_normal_ttl(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            url = "https://new.xianbao.fun/plus/json/push.json"
            db.execute(
                "insert or replace into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
                (
                    worker.poll_json_cache_key(url),
                    json.dumps([{"id": "stale", "title": "旧缓存"}], ensure_ascii=False),
                    "push-json",
                    None,
                    100,
                ),
            )
            db.commit()
            timeout_error = worker.urllib.error.URLError("timed out")
            env = {"XIANBAO_POLL_JSON_STALE_IF_ERROR_SECONDS": "1000"}
            with patch.dict(os.environ, env, clear=False), patch.object(worker, "fetch_json", side_effect=timeout_error), patch.object(
                worker.time, "time", return_value=900
            ):
                posts, fallback = worker.fetch_json_cached(db, url, ttl_seconds=60)

        self.assertEqual(posts, [{"id": "stale", "title": "旧缓存"}])
        self.assertTrue(fallback["poll_json_fallback"])
        self.assertTrue(fallback["poll_json_stale_fallback"])
        self.assertEqual(fallback["poll_json_fallback_age_seconds"], 800)
        self.assertEqual(fallback["poll_json_fallback_error_type"], "URLError")

    def test_fetch_json_cached_tries_multiple_urls_and_caches_successful_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            urls = [
                "https://new.xianbao.fun/plus/json/push.json",
                "https://backup.example/plus/json/push.json",
            ]
            timeout_error = worker.urllib.error.URLError("timed out")

            def fake_fetch(url, timeout=15, log_retries=False):
                if url == urls[0]:
                    raise timeout_error
                return [{"id": "ok", "title": "备用源"}]

            with patch.object(worker, "fetch_json", side_effect=fake_fetch), patch.object(worker.time, "time", return_value=1234):
                posts, fallback = worker.fetch_json_cached(db, urls)

            self.assertEqual(posts, [{"id": "ok", "title": "备用源"}])
            self.assertFalse(fallback.get("poll_json_fallback", False))
            self.assertEqual(fallback["poll_json_source_url"], urls[1])
            cached = db.execute(
                "select body, source, fetched_at from article_cache where url = ?",
                (worker.poll_json_cache_key(urls[1]),),
            ).fetchone()
            self.assertIsNotNone(cached)
            self.assertEqual(json.loads(cached[0]), posts)
            self.assertEqual(cached[1], "push-json")
            self.assertEqual(cached[2], 1234)

    def test_fetch_json_cached_records_direct_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            url = "https://new.xianbao.fun/plus/json/push.json"
            def fake_fetch(url_arg, log_retries=False):
                self.assertEqual(url_arg, url)
                return [{"id": "direct-ok"}]

            with patch.object(worker, "fetch_json", side_effect=fake_fetch), patch.object(worker.time, "time", return_value=1234):
                posts, fallback = worker.fetch_json_cached(db, url)

        self.assertEqual(posts, [{"id": "direct-ok"}])
        self.assertEqual(fallback["poll_json_transport"], "direct")
        self.assertFalse(fallback.get("poll_json_fallback", False))

    def test_fetch_json_cached_records_direct_failure_when_using_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            url = "https://new.xianbao.fun/plus/json/push.json"
            db.execute(
                "insert or replace into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
                (
                    worker.poll_json_cache_key(url),
                    json.dumps([{"id": "cached"}], ensure_ascii=False),
                    "push-json",
                    None,
                    100,
                ),
            )
            db.commit()
            direct_error = worker.urllib.error.URLError("direct timed out")
            env = {"XIANBAO_POLL_JSON_STALE_IF_ERROR_SECONDS": "1000"}
            with patch.dict(os.environ, env, clear=False), patch.object(worker, "fetch_json", side_effect=direct_error), patch.object(
                worker.time, "time", return_value=900
            ):
                posts, fallback = worker.fetch_json_cached(db, url, ttl_seconds=60)

        self.assertEqual(posts, [{"id": "cached"}])
        self.assertEqual(fallback["poll_json_transport"], "cache")
        failure = fallback["poll_json_failures"][0]
        self.assertEqual(failure, {"url": url, "error_type": "URLError", "error": str(direct_error)})

    def test_fetch_text_does_not_retry_through_legacy_proxy_after_direct_timeout(self):
        env = {
            "XIANBAO_SOURCE_PROXY": "http://127.0.0.1:2323",
            "XIANBAO_SOURCE_PROXY_MODE": "fallback",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            worker, "source_urlopen", side_effect=worker.urllib.error.URLError("timed out")
        ) as open_source:
            with self.assertRaises(worker.urllib.error.URLError):
                worker.fetch_text("https://new.xianbao.fun/zuankeba/1.html")
        open_source.assert_called_once()

    def test_fetch_json_cached_records_all_url_failures_when_using_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            primary = "https://new.xianbao.fun/plus/json/push.json"
            backup = "https://backup.example/plus/json/push.json"
            db.execute(
                "insert or replace into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
                (
                    worker.poll_json_cache_key(primary),
                    json.dumps([{"id": "cached"}], ensure_ascii=False),
                    "push-json",
                    None,
                    100,
                ),
            )
            db.commit()
            timeout_error = worker.urllib.error.URLError("timed out")
            env = {"XIANBAO_POLL_JSON_STALE_IF_ERROR_SECONDS": "1000"}
            with patch.dict(os.environ, env, clear=False), patch.object(worker, "fetch_json", side_effect=timeout_error), patch.object(
                worker.time, "time", return_value=900
            ):
                posts, fallback = worker.fetch_json_cached(db, [primary, backup], ttl_seconds=60)

        self.assertEqual(posts, [{"id": "cached"}])
        self.assertTrue(fallback["poll_json_fallback"])
        self.assertEqual(fallback["poll_json_fallback_url"], primary)
        self.assertEqual(fallback["poll_json_fallback_age_seconds"], 800)
        failures = fallback["poll_json_failures"]
        self.assertEqual([x["url"] for x in failures], [primary, backup])
        self.assertTrue(all(x["error_type"] == "URLError" for x in failures))

    def test_fetch_json_cached_skips_expired_cache_without_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = worker.ensure_db(Path(tmp) / "state.sqlite3")
            url = "https://new.xianbao.fun/plus/json/push.json"
            db.execute(
                "insert or replace into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
                (
                    worker.poll_json_cache_key(url),
                    json.dumps([{"id": "too-old"}], ensure_ascii=False),
                    "push-json",
                    None,
                    100,
                ),
            )
            db.commit()
            timeout_error = worker.urllib.error.URLError("timed out")
            env = {"XIANBAO_POLL_JSON_STALE_IF_ERROR_SECONDS": "300"}
            with patch.dict(os.environ, env, clear=False), patch.object(worker, "fetch_json", side_effect=timeout_error), patch.object(
                worker.time, "time", return_value=900
            ):
                posts, fallback = worker.fetch_json_cached(db, url, ttl_seconds=60)
            self.assertEqual(posts, [])
            self.assertTrue(fallback["poll_json_source_unavailable"])
            self.assertEqual(fallback["poll_json_fallback_age_seconds"], 800)
            self.assertNotIn("poll_json_fallback", fallback)

    def test_once_exits_successfully_when_push_json_times_out_without_cache(self):
        rules = {
            "polling": {
                "pushJson": "https://new.xianbao.fun/plus/json/push.json",
                "maxPushRowsPerPoll": 20,
                "intervalSeconds": 20,
            },
            "evaluation": {"maxBodyFetchesPerPoll": 0},
        }
        timeout_error = worker.urllib.error.URLError("timed out")
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "xianbao_hermes_worker.py",
                "--once",
                "--state",
                str(Path(tmp) / "state.sqlite3"),
                "--data-dir",
                tmp,
            ]
            with patch.object(worker.sys, "argv", argv), patch.object(worker, "load_json", return_value=rules), patch.object(
                worker, "validate_rules", return_value=([], [])
            ), patch.object(worker, "validate_runtime_config", return_value=([], [])), patch.object(
                worker, "fetch_json", side_effect=timeout_error
            ), patch.object(worker, "alert_runtime_error") as alert_runtime_error:
                rc = worker.main()

            self.assertEqual(rc, 0)
            alert_runtime_error.assert_not_called()
            summary_path = Path(tmp) / "poll_summaries.jsonl"
            summary = json.loads(summary_path.read_text(encoding="utf-8").strip())
            self.assertTrue(summary["poll_json_source_unavailable"])
            self.assertEqual(summary["poll_json_source_error_type"], "URLError")
            self.assertEqual(summary["seen"], 0)

    def test_source_unavailable_only_updates_local_state_even_past_old_alert_threshold(self):
        summary = {
            "poll_json_source_unavailable": True,
            "poll_json_source_error_type": "URLError",
            "poll_json_source_error": "<urlopen error timed out>",
            "poll_json_source_url": "https://new.xianbao.fun/plus/json/push.json",
            "poll_json_fallback_age_seconds": 321,
        }
        env = {
            "XIANBAO_SOURCE_UNAVAILABLE_ALERT_CONSECUTIVE_POLLS": "2",
            "XIANBAO_SOURCE_UNAVAILABLE_ALERT_COOLDOWN_SECONDS": "900",
            "XIANBAO_ALERT_SCRIPT": "/tmp/xianbao_alert.py",
        }
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            with patch.dict(os.environ, env, clear=False), patch.object(worker.Path, "exists", return_value=True), patch.object(
                worker.subprocess, "run"
            ) as run, patch.object(worker.time, "time", return_value=1000):
                run.return_value.returncode = 0
                worker.update_source_unavailable_state(data_dir, summary)
                worker.update_source_unavailable_state(data_dir, summary)

            run.assert_not_called()
            state = json.loads((data_dir / "poll_json_source_unavailable_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["consecutive"], 2)
            self.assertNotIn("last_alert_at", state)
            self.assertEqual(state["first_seen_at"], 1000)
            self.assertEqual(state["cache_age_seconds"], 321)
            self.assertEqual(state["url"], "https://new.xianbao.fun/plus/json/push.json")

    def test_once_source_failures_stay_local_with_missing_expired_or_valid_cache_and_recover(self):
        url = "https://new.xianbao.fun/plus/json/push.json"
        failures = [
            worker.urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
            worker.ssl.SSLEOFError("unexpected EOF"),
            TimeoutError("timed out"),
            worker.urllib.error.HTTPError(url, 403, "Forbidden", {}, None),
            worker.urllib.error.HTTPError(url, 503, "Service Unavailable", {}, None),
            worker.FetchJsonDecodeError(url, 200, "text/html", b"invalid", "invalid json response"),
            worker.http.client.IncompleteRead(b"partial", 100),
        ]
        for error in failures:
            for cache in ("missing", "expired", "valid"):
                with self.subTest(error=type(error).__name__, cache=cache), tempfile.TemporaryDirectory() as tmp:
                    data_dir = Path(tmp)
                    state_path = data_dir / "state.sqlite3"
                    db = worker.ensure_db(state_path)
                    if cache != "missing":
                        db.execute(
                            "insert into article_cache(url, body, source, error, fetched_at) values (?, ?, ?, ?, ?)",
                            (worker.poll_json_cache_key(url), "[]", "push-json", None, 1 if cache == "expired" else int(worker.time.time())),
                        )
                        db.commit()
                    db.close()
                    argv = ["worker", "--once", "--dry-run", "--state", str(state_path), "--data-dir", tmp]
                    env = {
                        "XIANBAO_POLL_JSON_CACHE_TTL_SECONDS": "60",
                        "XIANBAO_POLL_JSON_STALE_IF_ERROR_SECONDS": "300",
                        "PUSH_WEBHOOK_URL": "https://notify.invalid/test",
                        "BARK_URL": "",
                        "XIANBAO_USE_WEBOPS_BARK": "0",
                    }
                    with (
                        patch.dict(os.environ, env),
                        patch.object(worker.sys, "argv", argv),
                        patch.object(worker, "load_json", return_value=load_rules()),
                        patch.object(worker, "fetch_json", side_effect=error),
                        patch.object(worker, "alert_runtime_error") as runtime_alert,
                        patch.object(worker.subprocess, "run") as notify,
                        redirect_stdout(io.StringIO()),
                        redirect_stderr(io.StringIO()) as stderr,
                    ):
                        self.assertEqual(worker.main(), 0)
                        runtime_alert.assert_not_called()
                        notify.assert_not_called()
                        self.assertEqual(stderr.getvalue(), "")
                        self.assertTrue(worker.source_unavailable_summary(worker.LAST_POLL_SUMMARY))
                        self.assertNotIn("poll_error", worker.LAST_POLL_SUMMARY)
                        with patch.object(worker, "fetch_json", return_value=[]):
                            self.assertEqual(worker.main(), 0)
                        self.assertEqual(worker.LAST_POLL_SUMMARY["poll_json_transport"], "direct")
                        self.assertFalse((data_dir / worker.SOURCE_UNAVAILABLE_STATE_FILE).exists())
                        runtime_alert.assert_not_called()
                        notify.assert_not_called()

    def test_source_unavailable_state_resets_after_normal_source_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            state_path = data_dir / "poll_json_source_unavailable_state.json"
            state_path.write_text(json.dumps({"consecutive": 5, "last_alert_at": 1000}), encoding="utf-8")

            worker.update_source_unavailable_state(data_dir, {"seen": 3})

            self.assertFalse(state_path.exists())

    def test_once_returns_nonzero_and_logs_poll_error(self):
        error = worker.sqlite3.OperationalError("database is locked")
        with tempfile.TemporaryDirectory() as tmp:
            argv = [
                "xianbao_hermes_worker.py",
                "--once",
                "--state",
                str(Path(tmp) / "state.sqlite3"),
                "--data-dir",
                tmp,
            ]
            with patch.object(worker.sys, "argv", argv), patch.object(worker, "load_json", return_value={"polling": {"intervalSeconds": 20}}), patch.object(
                worker, "validate_rules", return_value=([], [])
            ), patch.object(worker, "validate_runtime_config", return_value=([], [])), patch.object(worker, "ensure_db", return_value=object()), patch.object(
                worker, "process_once", side_effect=error
            ), patch.object(worker, "alert_runtime_error") as alert_runtime_error:
                rc = worker.main()

            self.assertEqual(rc, 1)
            alert_runtime_error.assert_called_once_with(error, 1)
            summary_path = Path(tmp) / "poll_summaries.jsonl"
            summary = json.loads(summary_path.read_text(encoding="utf-8").strip())
            self.assertTrue(summary["poll_error"])
            self.assertEqual(summary["error_type"], "OperationalError")
            self.assertEqual(summary["error"], "database is locked")


if __name__ == "__main__":
    unittest.main()
