import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]

from pylon.tasks.chiphell import runtime


class ProxyConfigurationTests(unittest.TestCase):
    def test_proxy_is_required_and_fixed_to_local_frps_port(self):
        with self.assertRaisesRegex(RuntimeError, "CHIPHELL_PROXY_URL"):
            runtime.playwright_proxy_options({"CHIPHELL_REQUIRE_PROXY": "1"})
        with self.assertRaisesRegex(RuntimeError, "approved endpoint"):
            runtime.playwright_proxy_options({
                "CHIPHELL_REQUIRE_PROXY": "1",
                "CHIPHELL_PROXY_URL": "socks5://example.com:1080",
            })
        self.assertEqual(
            runtime.playwright_proxy_options({
                "CHIPHELL_REQUIRE_PROXY": "1",
                "CHIPHELL_PROXY_URL": "socks5://127.0.0.1:18080",
            }),
            {"proxy": {"server": "socks5://127.0.0.1:18080"}},
        )

    def test_page_classification_recognizes_567_and_login(self):
        for marker in ["Access Restricted", "567 Restricted Access", "EO_Bot_Ssid"]:
            self.assertEqual(runtime.classify_page(marker, ""), "BLOCKED_567")
        self.assertEqual(runtime.classify_page("抱歉，您尚未登录，没有权限访问该版块", "提示信息"), "LOGIN_REQUIRED")
        self.assertEqual(runtime.classify_page("正常帖子列表", "Chiphell"), "OK")

    def test_network_backoff_is_5_15_60_then_capped(self):
        self.assertEqual([runtime.network_backoff_seconds(i) for i in range(5)], [300, 900, 3600, 3600, 3600])

    def test_fault_transition_alerts_once_six_hourly_and_on_recovery(self):
        state = {}
        state, first = runtime.transition_fault(state, "PROXY_UNAVAILABLE", now=100)
        self.assertTrue(first.alert)
        self.assertFalse(first.recovered)
        state, quiet = runtime.transition_fault(state, "PROXY_UNAVAILABLE", now=100 + 3599)
        self.assertFalse(quiet.alert)
        state, repeat = runtime.transition_fault(state, "PROXY_UNAVAILABLE", now=100 + 21600)
        self.assertTrue(repeat.alert)
        state, recovery = runtime.transition_fault(state, "OK", now=100 + 21601)
        self.assertTrue(recovery.recovered)
        self.assertEqual(state["fault"], None)

    def test_upstream_fault_requires_two_rounds_and_silent_recovery_stays_silent(self):
        state, first = runtime.transition_fault(
            {},
            "UPSTREAM_UNAVAILABLE",
            now=100,
            alert_threshold=2,
        )
        self.assertFalse(first.alert)
        self.assertFalse(first.alert_sent)
        self.assertEqual(first.consecutive_count, 1)
        state, recovered = runtime.transition_fault(state, "OK", now=101)
        self.assertFalse(recovered.recovered)

        state, _ = runtime.transition_fault(
            {},
            "UPSTREAM_UNAVAILABLE",
            now=200,
            alert_threshold=2,
        )
        state, second = runtime.transition_fault(
            state,
            "UPSTREAM_UNAVAILABLE",
            now=500,
            alert_threshold=2,
        )
        self.assertTrue(second.alert)
        self.assertTrue(second.alert_sent)
        self.assertEqual(second.consecutive_count, 2)
        state, alerted_recovery = runtime.transition_fault(state, "OK", now=501)
        self.assertTrue(alerted_recovery.recovered)

    def test_fault_type_change_resets_consecutive_count(self):
        state, _ = runtime.transition_fault(
            {},
            "UPSTREAM_UNAVAILABLE",
            now=100,
            alert_threshold=2,
        )
        state, changed = runtime.transition_fault(state, "PROXY_UNAVAILABLE", now=101)
        self.assertTrue(changed.changed)
        self.assertTrue(changed.alert)
        self.assertEqual(changed.consecutive_count, 1)
        self.assertEqual(state["fault"]["kind"], "PROXY_UNAVAILABLE")

    def test_legacy_fault_state_is_compatible(self):
        legacy = {
            "fault": {
                "kind": "PROXY_UNAVAILABLE",
                "first_seen_at": 100,
                "last_seen_at": 100,
                "last_alert_at": 100,
            }
        }
        state, event = runtime.transition_fault(legacy, "PROXY_UNAVAILABLE", now=101)
        self.assertFalse(event.alert)
        self.assertTrue(event.alert_sent)
        self.assertEqual(event.consecutive_count, 2)
        state, recovery = runtime.transition_fault(state, "OK", now=102)
        self.assertTrue(recovery.recovered)

    def test_interval_is_always_60_to_120_seconds(self):
        self.assertEqual(runtime.choose_interval(lambda low, high: low), 60)
        self.assertEqual(runtime.choose_interval(lambda low, high: high), 120)

    def test_resume_requires_healthy_baselined_state(self):
        with self.assertRaisesRegex(RuntimeError, "status is not OK"):
            runtime.ensure_resume_ready({"status": "LOGIN_REQUIRED"}, {"baseline_at": 1, "notified_ids": ["1"]})
        with self.assertRaisesRegex(RuntimeError, "baseline is required"):
            runtime.ensure_resume_ready({"status": "OK"}, {"notified_ids": ["1"]})
        with self.assertRaisesRegex(RuntimeError, "baseline is required"):
            runtime.ensure_resume_ready({"status": "OK"}, {"baseline_at": 1, "notified_ids": []})
        runtime.ensure_resume_ready({"status": "OK"}, {"baseline_at": 1, "notified_ids": ["1"]})

    def test_forum_target_is_fid328_latest_threads(self):
        self.assertEqual(runtime.CHIPHELL_FID, "328")
        self.assertEqual(
            runtime.CHIPHELL_FORUM_URL,
            "https://www.chiphell.com/forum.php?mod=forumdisplay&fid=328&filter=author&orderby=dateline",
        )
        self.assertEqual(runtime.CHIPHELL_ALERT_SOURCE, "Chiphell fid=328 新帖监控")

    def test_cookie_import_requires_home_and_fid328_validation(self):
        for values in [
            ("LOGIN_REQUIRED", "OK", 10),
            ("OK", "BLOCKED_567", 10),
            ("OK", "OK", 0),
        ]:
            with self.assertRaisesRegex(RuntimeError, "cookie validation failed"):
                runtime.ensure_cookie_pages_valid(*values)
        runtime.ensure_cookie_pages_valid("OK", "OK", 10)

    def test_observe_exit_code_fails_for_non_ok_health(self):
        self.assertEqual(runtime.observe_exit_code({"status": "OK"}), 0)
        self.assertEqual(runtime.observe_exit_code({"status": "LOGIN_REQUIRED"}), 1)
        self.assertEqual(runtime.observe_exit_code({}), 1)

    def test_navigation_retries_twice_and_recovers_on_third_attempt(self):
        class RetryableError(Exception):
            pass

        timeouts: list[int] = []
        sleeps: list[float] = []
        failures: list[runtime.NavigationFailure] = []

        def navigate(timeout_ms: int) -> None:
            timeouts.append(timeout_ms)
            if len(timeouts) < 3:
                raise RetryableError("secret-cookie net::ERR_PROXY_CONNECTION_FAILED")

        outcome = runtime.navigate_with_retries(
            navigate,
            first_timeout_ms=30_000,
            retryable_errors=(RetryableError,),
            sleep=sleeps.append,
            on_failure=failures.append,
        )
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.attempts, 3)
        self.assertEqual(timeouts, [30_000, 15_000, 15_000])
        self.assertEqual(sleeps, [5, 15])
        self.assertEqual(
            [item.error_code for item in failures],
            ["net::ERR_PROXY_CONNECTION_FAILED", "net::ERR_PROXY_CONNECTION_FAILED"],
        )
        self.assertNotIn("secret-cookie", repr(outcome))

    def test_navigation_error_summary_never_returns_raw_message(self):
        class TimeoutErrorForTest(Exception):
            pass

        error_class, error_code = runtime.summarize_navigation_error(
            TimeoutErrorForTest("cookie=secret and private data")
        )
        self.assertEqual(error_class, "TimeoutErrorForTest")
        self.assertEqual(error_code, "NAVIGATION_TIMEOUT")
        self.assertNotIn("secret", error_code)

    def test_proxy_probe_stops_when_listener_is_down(self):
        calls: list[object] = []

        def connect(*args, **kwargs):
            raise OSError("refused")

        def run(*args, **kwargs):
            calls.append(args)
            raise AssertionError("curl must not run when listener is down")

        result = runtime.probe_proxy_path(connect=connect, run=run)
        self.assertEqual(result.status, "LISTENER_DOWN")
        self.assertEqual(result.error_code, "SOCKS_LISTENER_DOWN")
        self.assertEqual(calls, [])

    def test_proxy_probe_uses_only_fixed_socks5h_and_scrubs_proxy_environment(self):
        commands: list[list[str]] = []
        environments: list[dict[str, str]] = []

        class Connection:
            def close(self):
                return None

        def run(command, **kwargs):
            commands.append(command)
            environments.append(kwargs["env"])
            if len(commands) == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="failed")
            return SimpleNamespace(returncode=0, stdout="203.0.113.10\n", stderr="")

        result = runtime.probe_proxy_path(
            connect=lambda *args, **kwargs: Connection(),
            run=run,
            environ={
                "PATH": "/usr/bin",
                "HTTP_PROXY": "http://should-not-be-used",
                "all_proxy": "socks5://should-not-be-used",
            },
        )
        self.assertEqual(result.status, "OK")
        self.assertEqual(len(commands), 2)
        for command in commands:
            self.assertIn(runtime.APPROVED_CURL_PROXY_URL, command)
            self.assertIn("--proxy", command)
        for environment in environments:
            self.assertEqual(environment, {"PATH": "/usr/bin"})

    def test_proxy_probe_reports_egress_failure_after_all_fixed_endpoints(self):
        class Connection:
            def close(self):
                return None

        calls: list[list[str]] = []

        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=7, stdout="", stderr="failed")

        result = runtime.probe_proxy_path(
            connect=lambda *args, **kwargs: Connection(),
            run=run,
            environ={"PATH": "/usr/bin"},
        )
        self.assertEqual(result.status, "EGRESS_FAILED")
        self.assertEqual(result.error_code, "SOCKS_EGRESS_FAILED")
        self.assertEqual(len(calls), len(runtime.PROXY_PROBE_ENDPOINTS))

    def test_navigation_failure_distinguishes_proxy_from_upstream(self):
        healthy_proxy = runtime.ProxyProbeResult("OK", None, 250, "https://ip.3322.net")
        failed_proxy = runtime.ProxyProbeResult("LISTENER_DOWN", "SOCKS_LISTENER_DOWN", 1)
        self.assertEqual(runtime.classify_navigation_failure(healthy_proxy), "UPSTREAM_UNAVAILABLE")
        self.assertEqual(runtime.classify_navigation_failure(failed_proxy), "PROXY_UNAVAILABLE")


class QueueAndStateTests(unittest.TestCase):
    def test_delivery_queue_is_persistent_unique_and_claimed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "delivery.sqlite3"
            queue = runtime.DeliveryQueue(path)
            self.assertTrue(queue.enqueue("post:1", "message one", now=1))
            self.assertFalse(queue.enqueue("post:1", "duplicate", now=2))
            self.assertTrue(queue.enqueue("post:2", "message two", now=3))
            self.assertEqual(queue.pending_count(), 2)
            self.assertEqual(queue.claim(limit=10, now=4), ["message one", "message two"])
            self.assertEqual(queue.pending_count(), 0)
            self.assertEqual(runtime.DeliveryQueue(path).claim(limit=10, now=5), [])

    def test_baseline_merges_all_visible_ids_without_duplicates(self):
        state = {"notified_ids": ["1", "2"]}
        updated = runtime.apply_baseline(state, ["2", "3", "4"], max_ids=4)
        self.assertEqual(updated["notified_ids"], ["1", "2", "3", "4"])

    def test_cookie_payload_accepts_only_chiphell_domains(self):
        valid = {"cookies": [{
            "name": "sid",
            "value": "secret",
            "domain": ".chiphell.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Lax",
        }]}
        self.assertEqual(runtime.validate_cookie_payload(json.dumps(valid))[0]["domain"], ".chiphell.com")
        valid["cookies"][0]["domain"] = ".example.com"
        with self.assertRaisesRegex(ValueError, "domain"):
            runtime.validate_cookie_payload(json.dumps(valid))


if __name__ == "__main__":
    unittest.main()
