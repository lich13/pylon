from __future__ import annotations

import importlib.util
import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


def install_google_stubs() -> None:
    google = types.ModuleType("google")
    auth = types.ModuleType("google.auth")
    transport = types.ModuleType("google.auth.transport")
    requests = types.ModuleType("google.auth.transport.requests")
    oauth2 = types.ModuleType("google.oauth2")
    credentials = types.ModuleType("google.oauth2.credentials")
    discovery = types.ModuleType("googleapiclient.discovery")

    class Request:
        pass

    class Credentials:
        @classmethod
        def from_authorized_user_file(cls, *args, **kwargs):
            return cls()

        @property
        def expired(self):
            return False

        @property
        def valid(self):
            return True

        @property
        def refresh_token(self):
            return ""

        def refresh(self, request):
            return None

        def to_json(self):
            return "{}"

    def build(*args, **kwargs):
        return None

    requests.Request = Request
    credentials.Credentials = Credentials
    discovery.build = build
    sys.modules.setdefault("google", google)
    sys.modules.setdefault("google.auth", auth)
    sys.modules.setdefault("google.auth.transport", transport)
    sys.modules.setdefault("google.auth.transport.requests", requests)
    sys.modules.setdefault("google.oauth2", oauth2)
    sys.modules.setdefault("google.oauth2.credentials", credentials)
    sys.modules.setdefault("googleapiclient.discovery", discovery)


def load_module():
    install_google_stubs()
    script = Path(__file__).resolve().parents[2] / "src/pylon/tasks/gmail/cleanup.py"
    spec = importlib.util.spec_from_file_location("gmail_cleanup_delete", script)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gmail_cleanup_delete"] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


gmail_cleanup = load_module()


class GmailCleanupRuleDefinitionTests(unittest.TestCase):
    def rule_by_name(self, name: str) -> dict:
        for rule in gmail_cleanup.RULES:
            if rule["name"] == name:
                return rule
        raise AssertionError(f"Missing rule: {name}")

    def test_builtin_rule_names_are_unique_and_protected(self):
        names = [rule["name"] for rule in gmail_cleanup.RULES]
        self.assertEqual(len(names), len(set(names)))
        for rule in gmail_cleanup.RULES:
            with self.subTest(rule=rule["name"]):
                self.assertTrue(rule["query"].startswith(gmail_cleanup.PROTECTION))

    def test_bilingual_invoice_receipt_rule_covers_current_noise(self):
        query = self.rule_by_name("invoice_receipt_documents")["query"]
        expected_terms = [
            'subject:"电子发票"',
            'subject:"发票下载"',
            'subject:"水单"',
            'subject:"receipt"',
            'subject:"invoice"',
            'subject:"tax invoice"',
            'subject:"First Invoice Overdue Notice"',
            'from:hotel-noreply@notice.alitrip.com',
            'from:yun2@vip.baiwang.com',
        ]
        for term in expected_terms:
            with self.subTest(term=term):
                self.assertIn(term, query)

    def test_bilingual_digest_rule_covers_summary_newsletter_language(self):
        query = self.rule_by_name("digests_newsletters_summaries")["query"]
        expected_terms = [
            'subject:"daily digest"',
            'subject:"weekly summary"',
            'subject:"newsletter"',
            'subject:"总结"',
            'subject:"摘要"',
            'subject:"简报"',
            'subject:"日报"',
            'subject:"周报"',
        ]
        for term in expected_terms:
            with self.subTest(term=term):
                self.assertIn(term, query)

    def test_ecommerce_fulfillment_rule_covers_shipping_and_order_receipts(self):
        query = self.rule_by_name("ecommerce_order_fulfillment")["query"]
        expected_terms = [
            'subject:"您下单的商品已发货"',
            'subject:"订单已发货"',
            'subject:"shipment"',
            'subject:"your order has shipped"',
            'from:tz3@email.ldxp.cn',
        ]
        for term in expected_terms:
            with self.subTest(term=term):
                self.assertIn(term, query)

    def test_account_settings_rule_covers_mail_forwarding_notices(self):
        query = self.rule_by_name("account_settings_change_notices")["query"]
        expected_terms = [
            'subject:"邮件现被转发至"',
            'subject:"mail is being forwarded"',
            'subject:"email forwarding"',
            'from:noreply@email.apple.com',
        ]
        for term in expected_terms:
            with self.subTest(term=term):
                self.assertIn(term, query)

    def test_low_value_service_login_rule_covers_spotify_japanese_login(self):
        query = self.rule_by_name("low_value_service_login_notices")["query"]
        expected_terms = [
            "from:no-reply@spotify.com",
            'subject:"新しいログイン"',
            'subject:"ログインがありました"',
            '-subject:"不審なログイン"',
            '-subject:"password reset"',
        ]
        for term in expected_terms:
            with self.subTest(term=term):
                self.assertIn(term, query)

    def test_account_security_alerts_are_not_a_builtin_delete_rule(self):
        names = [rule["name"] for rule in gmail_cleanup.RULES]
        self.assertNotIn("account_security_alerts", names)

    def test_pending_candidate_classes_are_now_builtin_rules(self):
        builtin_queries = "\n".join(rule["query"] for rule in gmail_cleanup.RULES)
        expected_terms = [
            'subject:"First Invoice Overdue Notice"',
            'from:hotel-noreply@notice.alitrip.com',
            'subject:"飞猪水单开具通知"',
            'from:yun2@vip.baiwang.com',
            'subject:"电子发票下载"',
            'from:tz3@email.ldxp.cn',
            'subject:"您下单的商品已发货，点击查看详情"',
            'from:noreply@email.apple.com',
            'subject:"邮件现被转发至"',
        ]
        for term in expected_terms:
            with self.subTest(term=term):
                self.assertIn(term, builtin_queries)

    def test_builtin_covered_candidates_are_not_reported_as_pending(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "new_type_candidates.json"
            state_path.write_text(json.dumps({
                "version": 1,
                "candidates": {
                    "gmail-nt-covered": {
                        "id": "gmail-nt-covered",
                        "status": "pending",
                        "from": "hotel-noreply@notice.alitrip.com",
                        "subject_family": "飞猪水单开具通知",
                        "suggested_query": '-is:starred from:hotel-noreply@notice.alitrip.com subject:"飞猪水单开具通知"',
                        "sample_count": 1,
                        "last_seen": "2026-05-27",
                    },
                    "gmail-nt-partial-subject": {
                        "id": "gmail-nt-partial-subject",
                        "status": "pending",
                        "from": "noreply@email.apple.com",
                        "subject_family": "user@example.com 的邮件现被转发至 user2@example.com",
                        "suggested_query": '-is:starred from:noreply@email.apple.com subject:"user@example.com 的邮件现被转发至 user2@example.com"',
                        "sample_count": 1,
                        "last_seen": "2026-05-27",
                    }
                },
            }), encoding="utf-8")
            result = gmail_cleanup.update_candidate_state(state_path, [], run_date="2026-05-28")
            self.assertEqual(result["pending_candidates"], [])

    def test_spotify_japanese_login_candidate_is_covered_builtin(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "new_type_candidates.json"
            state_path.write_text(json.dumps({
                "version": 1,
                "candidates": {
                    "gmail-nt-spotify": {
                        "id": "gmail-nt-spotify",
                        "status": "pending",
                        "from": "no-reply@spotify.com",
                        "subject_family": "Spotify に新しいログインがありました",
                        "suggested_query": '-is:starred from:no-reply@spotify.com subject:"Spotify に新しいログインがありました"',
                        "sample_count": 1,
                        "last_seen": "2026-05-28",
                    },
                },
            }), encoding="utf-8")
            result = gmail_cleanup.update_candidate_state(state_path, [], run_date="2026-05-29")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(result["pending_candidates"], [])
            self.assertEqual(state["candidates"]["gmail-nt-spotify"]["status"], "covered_builtin")

    def test_new_spotify_japanese_login_candidate_is_not_reported(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "new_type_candidates.json"
            result = gmail_cleanup.update_candidate_state(state_path, [{
                "id": "m-spotify",
                "from": "Spotify <no-reply@spotify.com>",
                "subject": "Spotify に新しいログインがありました",
                "date": "Thu, 28 May 2026 13:58:47 +0000",
                "snippet": "お客さまのアカウントに新しいログインがありました。",
                "labels": ["INBOX"],
            }], run_date="2026-05-29")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            candidates = state["candidates"]
            self.assertEqual(result["new_candidates"], [])
            self.assertEqual(result["pending_candidates"], [])
            self.assertEqual(len(candidates), 1)
            self.assertEqual(next(iter(candidates.values()))["status"], "covered_builtin")

    def test_risky_security_candidate_is_not_covered_by_low_value_login_rule(self):
        risky_candidates = [
            {
                "from": "no-reply@spotify.com",
                "subject_family": "Spotify に不審なログインがありました",
            },
            {
                "from": "no-reply@spotify.com",
                "subject_family": "Spotify password reset requested",
            },
            {
                "from": "alerts@example.com",
                "subject_family": "Suspicious sign-in blocked",
            },
            {
                "from": "alerts@example.com",
                "subject_family": "异常登录提醒",
            },
        ]
        for candidate in risky_candidates:
            with self.subTest(subject=candidate["subject_family"]):
                self.assertFalse(gmail_cleanup.is_candidate_covered_by_builtin_rules(candidate))


class GmailCleanupDynamicCandidatesTests(unittest.TestCase):
    def test_main_dry_run_does_not_delete_and_reports_candidates(self):
        calls = {"delete": 0}
        original_build = gmail_cleanup.build_gmail_service
        original_list = gmail_cleanup.list_message_ids
        original_meta = gmail_cleanup.get_message_metadata
        original_delete = gmail_cleanup.batch_delete
        original_state = gmail_cleanup.NEW_TYPE_STATE_PATH
        original_extra = gmail_cleanup.EXTRA_RULES_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                gmail_cleanup.NEW_TYPE_STATE_PATH = Path(td) / "candidates.json"
                gmail_cleanup.EXTRA_RULES_PATH = Path(td) / "extra_rules.json"
                gmail_cleanup.build_gmail_service = lambda: object()

                def fake_list(service, query, max_messages=None):
                    if "newer_than:14d" in query:
                        return ["new1", "known1"]
                    if "category:promotions" in query:
                        return ["known1"]
                    return []

                gmail_cleanup.list_message_ids = fake_list
                gmail_cleanup.get_message_metadata = lambda service, mid: {
                    "id": mid,
                    "from": "new@example.com" if mid == "new1" else "promo@example.com",
                    "subject": "New digest 20260517" if mid == "new1" else "Promo",
                    "date": "Sun, 17 May 2026 09:00:00 +0800",
                    "snippet": "snippet",
                    "labels": ["INBOX"],
                }

                def fake_delete(service, ids):
                    calls["delete"] += 1
                    return len(ids)

                gmail_cleanup.batch_delete = fake_delete
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gmail_cleanup.main(["--dry-run", "--format", "text"]), 0)
        finally:
            gmail_cleanup.build_gmail_service = original_build
            gmail_cleanup.list_message_ids = original_list
            gmail_cleanup.get_message_metadata = original_meta
            gmail_cleanup.batch_delete = original_delete
            gmail_cleanup.NEW_TYPE_STATE_PATH = original_state
            gmail_cleanup.EXTRA_RULES_PATH = original_extra
        self.assertEqual(calls["delete"], 0)
        self.assertIn("Gmail 自动清理试运行完成", out.getvalue())
        self.assertIn("新类型邮件待判断", out.getvalue())

    def test_main_quiet_noop_suppresses_text_when_only_existing_pending_candidates(self):
        original_build = gmail_cleanup.build_gmail_service
        original_list = gmail_cleanup.list_message_ids
        original_meta = gmail_cleanup.get_message_metadata
        original_delete = gmail_cleanup.batch_delete
        original_state = gmail_cleanup.NEW_TYPE_STATE_PATH
        original_extra = gmail_cleanup.EXTRA_RULES_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                gmail_cleanup.NEW_TYPE_STATE_PATH = Path(td) / "candidates.json"
                gmail_cleanup.EXTRA_RULES_PATH = Path(td) / "extra_rules.json"
                gmail_cleanup.NEW_TYPE_STATE_PATH.write_text(json.dumps({
                    "version": 1,
                    "candidates": {
                        "gmail-nt-existing": {
                            "id": "gmail-nt-existing",
                            "status": "pending",
                            "from": "robot@example.com",
                            "subject_family": "Build finished",
                            "suggested_query": '-is:starred from:robot@example.com subject:"Build finished"',
                            "sample_count": 2,
                            "last_seen": "2026-06-11",
                        }
                    },
                }), encoding="utf-8")
                gmail_cleanup.build_gmail_service = lambda: object()
                gmail_cleanup.list_message_ids = lambda service, query, max_messages=None: []
                gmail_cleanup.get_message_metadata = lambda service, mid: {}
                gmail_cleanup.batch_delete = lambda service, ids: len(ids)

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gmail_cleanup.main(["--format", "text", "--quiet-noop"]), 0)
        finally:
            gmail_cleanup.build_gmail_service = original_build
            gmail_cleanup.list_message_ids = original_list
            gmail_cleanup.get_message_metadata = original_meta
            gmail_cleanup.batch_delete = original_delete
            gmail_cleanup.NEW_TYPE_STATE_PATH = original_state
            gmail_cleanup.EXTRA_RULES_PATH = original_extra
        self.assertEqual(out.getvalue(), "")

    def test_main_quiet_noop_still_reports_new_candidates(self):
        original_build = gmail_cleanup.build_gmail_service
        original_list = gmail_cleanup.list_message_ids
        original_meta = gmail_cleanup.get_message_metadata
        original_delete = gmail_cleanup.batch_delete
        original_state = gmail_cleanup.NEW_TYPE_STATE_PATH
        original_extra = gmail_cleanup.EXTRA_RULES_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                gmail_cleanup.NEW_TYPE_STATE_PATH = Path(td) / "candidates.json"
                gmail_cleanup.EXTRA_RULES_PATH = Path(td) / "extra_rules.json"
                gmail_cleanup.build_gmail_service = lambda: object()

                def fake_list(service, query, max_messages=None):
                    if "newer_than:14d" in query:
                        return ["new1"]
                    return []

                gmail_cleanup.list_message_ids = fake_list
                gmail_cleanup.get_message_metadata = lambda service, mid: {
                    "id": mid,
                    "from": "Robot <robot@example.com>",
                    "subject": "Build finished 20260612",
                    "date": "Fri, 12 Jun 2026 01:00:00 +0800",
                    "snippet": "finished",
                    "labels": ["INBOX"],
                }
                gmail_cleanup.batch_delete = lambda service, ids: len(ids)

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gmail_cleanup.main(["--format", "text", "--quiet-noop"]), 0)
        finally:
            gmail_cleanup.build_gmail_service = original_build
            gmail_cleanup.list_message_ids = original_list
            gmail_cleanup.get_message_metadata = original_meta
            gmail_cleanup.batch_delete = original_delete
            gmail_cleanup.NEW_TYPE_STATE_PATH = original_state
            gmail_cleanup.EXTRA_RULES_PATH = original_extra
        self.assertIn("新类型邮件待判断：新增 1 个", out.getvalue())

    def test_main_quiet_noop_still_reports_deletions(self):
        original_build = gmail_cleanup.build_gmail_service
        original_list = gmail_cleanup.list_message_ids
        original_meta = gmail_cleanup.get_message_metadata
        original_delete = gmail_cleanup.batch_delete
        original_state = gmail_cleanup.NEW_TYPE_STATE_PATH
        original_extra = gmail_cleanup.EXTRA_RULES_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                gmail_cleanup.NEW_TYPE_STATE_PATH = Path(td) / "candidates.json"
                gmail_cleanup.EXTRA_RULES_PATH = Path(td) / "extra_rules.json"
                gmail_cleanup.build_gmail_service = lambda: object()
                gmail_cleanup.list_message_ids = lambda service, query, max_messages=None: ["m1"] if "category:promotions" in query else []
                gmail_cleanup.get_message_metadata = lambda service, mid: {}
                gmail_cleanup.batch_delete = lambda service, ids: len(ids)

                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gmail_cleanup.main(["--format", "text", "--quiet-noop"]), 0)
        finally:
            gmail_cleanup.build_gmail_service = original_build
            gmail_cleanup.list_message_ids = original_list
            gmail_cleanup.get_message_metadata = original_meta
            gmail_cleanup.batch_delete = original_delete
            gmail_cleanup.NEW_TYPE_STATE_PATH = original_state
            gmail_cleanup.EXTRA_RULES_PATH = original_extra
        self.assertIn("已永久删除 1 封", out.getvalue())

    def test_main_accept_candidate_command_exits_before_gmail_api(self):
        original_build = gmail_cleanup.build_gmail_service
        original_state = gmail_cleanup.NEW_TYPE_STATE_PATH
        original_extra = gmail_cleanup.EXTRA_RULES_PATH
        try:
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                gmail_cleanup.NEW_TYPE_STATE_PATH = root / "candidates.json"
                gmail_cleanup.EXTRA_RULES_PATH = root / "extra_rules.json"
                result = gmail_cleanup.update_candidate_state(gmail_cleanup.NEW_TYPE_STATE_PATH, [{
                    "id": "m1",
                    "from": "Robot <robot@example.com>",
                    "subject": "Daily robot digest",
                    "date": "Sun, 17 May 2026 09:00:00 +0800",
                    "snippet": "digest",
                    "labels": ["INBOX"],
                }], run_date="2026-05-17")
                cid = result["new_candidates"][0]["id"]

                def fail_build():
                    raise AssertionError("Gmail API should not be built for accept-candidate")

                gmail_cleanup.build_gmail_service = fail_build
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gmail_cleanup.main(["--accept-candidate", cid, "--format", "text"]), 0)
                self.assertIn("已加入 Gmail 自动清理规则", out.getvalue())
                self.assertTrue(gmail_cleanup.EXTRA_RULES_PATH.exists())
        finally:
            gmail_cleanup.build_gmail_service = original_build
            gmail_cleanup.NEW_TYPE_STATE_PATH = original_state
            gmail_cleanup.EXTRA_RULES_PATH = original_extra

    def test_candidate_id_is_stable_for_same_sender_and_subject_family(self):
        first = gmail_cleanup.build_candidate_from_message({
            "id": "m1",
            "from": "Example News <news@example.com>",
            "subject": "Your weekly update #12345",
            "date": "Fri, 15 May 2026 10:00:00 +0800",
            "snippet": "latest digest",
            "labels": ["INBOX"],
        })
        second = gmail_cleanup.build_candidate_from_message({
            "id": "m2",
            "from": "news@example.com",
            "subject": "Your weekly update #67890",
            "date": "Sat, 16 May 2026 10:00:00 +0800",
            "snippet": "latest digest",
            "labels": ["INBOX"],
        })
        self.assertEqual(first["id"], second["id"])
        self.assertIn("from:news@example.com", first["suggested_query"])
        self.assertIn('subject:"Your weekly update"', first["suggested_query"])

    def test_update_candidate_state_reports_new_only_once(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "new_type_candidates.json"
            messages = [{
                "id": "m1",
                "from": "Robot <robot@example.com>",
                "subject": "Build finished 20260517",
                "date": "Sun, 17 May 2026 09:00:00 +0800",
                "snippet": "finished",
                "labels": ["INBOX"],
            }]
            first = gmail_cleanup.update_candidate_state(state_path, messages, run_date="2026-05-17")
            second = gmail_cleanup.update_candidate_state(state_path, messages, run_date="2026-05-17")
            self.assertEqual(len(first["new_candidates"]), 1)
            self.assertEqual(second["new_candidates"], [])
            self.assertEqual(len(second["pending_candidates"]), 1)

    def test_accept_candidate_writes_extra_rule(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            state_path = root / "new_type_candidates.json"
            rules_path = root / "extra_rules.json"
            result = gmail_cleanup.update_candidate_state(state_path, [{
                "id": "m1",
                "from": "Alerts <alerts@example.com>",
                "subject": "Quota warning 2026-05-17",
                "date": "Sun, 17 May 2026 09:00:00 +0800",
                "snippet": "quota warning",
                "labels": ["INBOX"],
            }], run_date="2026-05-17")
            cid = result["new_candidates"][0]["id"]
            accepted = gmail_cleanup.accept_candidate(
                cid,
                state_path=state_path,
                extra_rules_path=rules_path,
                created_at_utc="2026-05-17T01:00:00Z",
            )
            self.assertEqual(accepted["source_candidate_id"], cid)
            data = json.loads(rules_path.read_text(encoding="utf-8"))
            self.assertEqual(data["rules"][0]["source_candidate_id"], cid)
            self.assertIn("from:alerts@example.com", data["rules"][0]["query"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["candidates"][cid]["status"], "accepted")

    def test_format_text_report_includes_new_type_candidates(self):
        text = gmail_cleanup.format_text_report(
            dry_run=False,
            total_unique_matches=2,
            deleted_count=2,
            per_rule=[{"description": "测试规则", "matched_count": 2, "new_unique_count": 2}],
            candidate_report={
                "new_candidates": [{
                    "id": "gmail-nt-deadbeef",
                    "from": "robot@example.com",
                    "subject_family": "Build finished",
                    "sample_count": 1,
                    "suggested_query": '-is:starred from:robot@example.com subject:"Build finished"',
                }],
                "pending_candidates": [],
            },
        )
        self.assertIn("新类型邮件待判断", text)
        self.assertIn("gmail-nt-deadbeef", text)
        self.assertIn("robot@example.com", text)

    def test_format_text_report_is_compact_for_rule_details_and_queries(self):
        text = gmail_cleanup.format_text_report(
            dry_run=False,
            total_unique_matches=11,
            deleted_count=11,
            per_rule=[
                {"description": "验证码邮件", "matched_count": 0, "new_unique_count": 0},
                {"description": "发票/收据/水单/票据", "matched_count": 7, "new_unique_count": 6},
            ],
            candidate_report={
                "new_candidates": [{
                    "id": "gmail-nt-deadbeef",
                    "from": "robot@example.com",
                    "subject_family": "Build finished",
                    "sample_count": 1,
                    "suggested_query": '-is:starred from:robot@example.com subject:"Build finished"',
                }],
                "pending_candidates": [],
            },
        )
        self.assertIn("Gmail 自动清理已执行：已永久删除 11 封", text)
        self.assertNotIn("验证码邮件", text)
        self.assertNotIn("发票/收据/水单/票据", text)
        self.assertNotIn("建议查询", text)
        self.assertNotIn("-is:starred", text)

    def test_format_text_report_dry_run_uses_non_destructive_wording(self):
        text = gmail_cleanup.format_text_report(
            dry_run=True,
            total_unique_matches=3,
            deleted_count=0,
            per_rule=[{"description": "测试规则", "matched_count": 3, "new_unique_count": 3}],
            candidate_report={"new_candidates": [], "pending_candidates": []},
        )
        self.assertIn("Gmail 自动清理试运行完成：若执行将永久删除 3 封", text)
        self.assertNotIn("永久删除 0 封", text)


if __name__ == "__main__":
    unittest.main()
