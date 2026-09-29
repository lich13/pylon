import itertools
import os
from pylon.outbox import Outbox
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from webops_test_utils import load_script_module


_MODULE_COUNTER = itertools.count()


def load_replay_module():
    return load_script_module(
        f"software_update_monitor_delivery_replay_test_{next(_MODULE_COUNTER)}",
        "software_update_monitor_delivery_replay.py",
    )


ALERT_TEXT = """# Cron Job: AI/Codex software update monitor

**Job ID:** fa27c561ded6
**Run Time:** 2026-06-08 09:47:21
**Mode:** no_agent (script)

---

## sub2api Releases - Sub2API 0.1.135

发布时间：2026-06-08T01:47:08Z
链接：https://github.com/Wei-Shaw/sub2api/releases/tag/v0.1.135

docker pull weishaw/sub2api:0.1.135
"""


CODEX_RAW_ITEM = {
    "source": {
        "id": "openai_codex_changelog",
        "name": "OpenAI Codex Changelog",
        "url": "https://developers.openai.com/codex/changelog/rss.xml",
        "kind": "rss",
    },
    "item": {
        "source_id": "openai_codex_changelog",
        "id": "https://developers.openai.com/codex/changelog/#github-release-334076532",
        "title": "Codex CLI Release: 0.137.0",
        "url": "https://developers.openai.com/codex/changelog/#github-release-334076532",
        "published_at": "Thu, 04 Jun 2026 00:00:00 GMT",
        "body": "0.137.0",
    },
    "saved_at": "2026-06-04T02:02:15+00:00",
}


class SoftwareUpdateMonitorDeliveryReplayTests(unittest.TestCase):
    def setUp(self):
        self.replay = load_replay_module()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name) / "webops"
        self.root.mkdir()
        self.env = mock.patch.dict(os.environ, {"PYLON_OUTBOX": str(self.root / "outbox.sqlite3")})
        self.env.start()
        self.replay.PROFILE_ROOT = self.root
        self.replay.AGENT_ROOT = self.root / "hermes-agent"
        self.replay.HERMES_PYTHON = self.replay.AGENT_ROOT / "venv" / "bin" / "python"
        self.replay.JOBS_FILE = self.root / "cron" / "jobs.json"
        self.replay.LOG_FILE = self.root / "logs" / "agent.log"
        self.replay.OUTPUT_DIR = self.root / "cron" / "output" / self.replay.MONITOR_JOB_ID
        self.replay.STATE_FILE = self.root / "cron_state" / "software_update_monitor_delivery_replay.json"
        self.replay.ITEMS_DIR = self.root / "data" / "software_update_monitor" / "items"
        self.replay.MONITOR_SCRIPT = self.root / "scripts" / "software_update_monitor.py"
        self.replay.OUTPUT_DIR.mkdir(parents=True)
        self.replay.ITEMS_DIR.mkdir(parents=True)
        self.replay.LOG_FILE.parent.mkdir(parents=True)
        self.replay.JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.env.stop()
        self.tmpdir.cleanup()

    def queued_text(self):
        with Outbox().connect() as db:
            rows = db.execute("SELECT payload FROM events ORDER BY created_at").fetchall()
        return "\n".join(json.loads(row[0])["text"] for row in rows)

    def write_jobs(self, deliver="telegram:-1001234567890"):
        self.replay.JOBS_FILE.write_text(
            json.dumps(
                [
                    {
                        "id": self.replay.MONITOR_JOB_ID,
                        "name": "AI/Codex software update monitor",
                        "deliver": deliver,
                    }
                ]
            ),
            encoding="utf-8",
        )

    def write_raw_item(self, payload=None, name="codex.json"):
        path = self.replay.ITEMS_DIR / name
        path.write_text(json.dumps(payload or CODEX_RAW_ITEM), encoding="utf-8")
        return path

    def translated_fake_monitor(self):
        test_case = self

        class FakeDeepLWebTranslator:
            constructed = []

            def __init__(self, *, cache_path, translation_state=None, **_):
                self.__class__.constructed.append(
                    {
                        "cache_path": cache_path,
                        "translation_state": translation_state,
                    }
                )

            def translate(self, text, **_):
                return f"ZH:{text}"

        class FakeMonitor:
            TRANSLATION_CACHE_FILE = test_case.root / "data" / "software_update_monitor" / "translation_cache.json"
            DeepLWebTranslator = FakeDeepLWebTranslator

            @staticmethod
            def load_sources():
                return [
                    {
                        "id": "openai_codex_cli_releases",
                        "name": "OpenAI Codex CLI Releases",
                        "url": "https://github.com/openai/codex/releases.atom",
                        "kind": "atom",
                    }
                ]

            @staticmethod
            def fetch_url(url):
                test_case.assertEqual(url, "https://github.com/openai/codex/releases.atom")
                return b"<feed/>"

            @staticmethod
            def parse_source_items(source, raw):
                return [
                    {
                        "source_id": source["id"],
                        "id": "tag:github.com,2008:Repository/965415649/rust-v0.138.0",
                        "title": "0.138.0",
                        "url": "https://github.com/openai/codex/releases/tag/rust-v0.138.0",
                        "published_at": "2026-06-08T23:01:11Z",
                        "body": "stable",
                    },
                    {
                        "source_id": source["id"],
                        "id": "tag:github.com,2008:Repository/965415649/rust-v0.138.0-alpha.7",
                        "title": "0.138.0-alpha.7",
                        "url": "https://github.com/openai/codex/releases/tag/rust-v0.138.0-alpha.7",
                        "published_at": "2026-06-08T14:50:10Z",
                        "body": "alpha",
                    },
                ]

            @staticmethod
            def item_allowed_by_title_filters(source, item):
                return item["title"] == "0.138.0"

            @staticmethod
            def format_alerts(events, *, translator):
                test_case.assertNotIsInstance(translator, test_case.replay.OriginalTextTranslator)
                test_case.assertIsInstance(translator, FakeDeepLWebTranslator)
                return "\n".join(
                    f"{event['source']['name']} — {translator.translate(event['item']['title'])}\n"
                    f"{translator.translate(event['item'].get('body', ''))}"
                    for event in events
                )

        return FakeMonitor, FakeDeepLWebTranslator

    def current_codex_cli_release_monitor(self):
        test_case = self

        class FakeDeepLWebTranslator:
            constructed = []

            def __init__(self, *, cache_path, translation_state=None, **_):
                self.__class__.constructed.append(
                    {
                        "cache_path": cache_path,
                        "translation_state": translation_state,
                    }
                )

            def translate(self, text, **_):
                return f"ZH:{text}"

        class FakeMonitor:
            TRANSLATION_CACHE_FILE = test_case.root / "data" / "software_update_monitor" / "translation_cache.json"
            DeepLWebTranslator = FakeDeepLWebTranslator

            @staticmethod
            def load_sources():
                return [
                    {
                        "id": "openai_codex_cli_releases",
                        "name": "OpenAI Codex CLI Releases",
                        "url": "https://github.com/openai/codex/releases.atom",
                        "kind": "atom",
                        "item_title_include_patterns": ["re:^\\d+\\.\\d+\\.\\d+$"],
                    }
                ]

            @staticmethod
            def fetch_url(url):
                test_case.assertEqual(url, "https://github.com/openai/codex/releases.atom")
                return b"<feed/>"

            @staticmethod
            def parse_source_items(source, raw):
                test_case.assertEqual(source["id"], "openai_codex_cli_releases")
                test_case.assertEqual(raw, b"<feed/>")
                return [
                    {
                        "source_id": source["id"],
                        "id": "tag:github.com,2008:Repository/965415649/rust-v0.139.0",
                        "title": "0.139.0",
                        "url": "https://github.com/openai/codex/releases/tag/rust-v0.139.0",
                        "published_at": "2026-06-09T23:18:40Z",
                        "body": "Codex CLI stable release 0.139.0",
                    },
                    {
                        "source_id": source["id"],
                        "id": "tag:github.com,2008:Repository/965415649/rust-v0.140.0-alpha.1",
                        "title": "0.140.0-alpha.1",
                        "url": "https://github.com/openai/codex/releases/tag/rust-v0.140.0-alpha.1",
                        "published_at": "2026-06-10T00:15:00Z",
                        "body": "Codex CLI alpha release",
                    },
                ]

            @staticmethod
            def item_allowed_by_title_filters(source, item):
                title = str(item.get("title") or "")
                return title.count(".") == 2 and "-alpha." not in title

            @staticmethod
            def format_alerts(events, *, translator):
                test_case.assertIsInstance(translator, FakeDeepLWebTranslator)
                return "\n".join(
                    f"{event['source']['name']} — {translator.translate(event['item']['title'])}\n"
                    f"{translator.translate(event['item'].get('body', ''))}\n"
                    f"{event['item']['url']}"
                    for event in events
                )

        return FakeMonitor

    def test_parse_delivery_errors_for_monitor_job(self):
        self.replay.LOG_FILE.write_text(
            "\n".join(
                [
                    "2026-06-08 09:47:31,000 INFO cron.scheduler: other line",
                    "2026-06-08 09:47:32,951 ERROR cron.scheduler: Job 'fa27c561ded6': delivery error: Telegram send failed: RuntimeError('cannot schedule new futures after interpreter shutdown')",
                    "2026-06-08 09:47:32,951 ERROR cron.scheduler: Job 'b7a84d1d9f21': delivery error: unrelated",
                ]
            ),
            encoding="utf-8",
        )

        errors = self.replay.parse_delivery_errors(self.replay.LOG_FILE)

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].job_id, self.replay.MONITOR_JOB_ID)
        self.assertEqual(errors[0].when, datetime(2026, 6, 8, 9, 47, 32, 951000, tzinfo=timezone.utc))
        self.assertIn("cannot schedule new futures", errors[0].message)

    def test_matches_error_to_nearby_non_silent_output(self):
        self.replay.LOG_FILE.write_text(
            "2026-06-08 09:47:32,951 ERROR cron.scheduler: Job 'fa27c561ded6': delivery error: Telegram send failed\n",
            encoding="utf-8",
        )
        alert = self.replay.OUTPUT_DIR / "2026-06-08_09-47-21.md"
        silent = self.replay.OUTPUT_DIR / "2026-06-08_09-48-21.md"
        alert.write_text(ALERT_TEXT, encoding="utf-8")
        silent.write_text("[SILENT]\n", encoding="utf-8")

        candidates = self.replay.find_replay_candidates(
            self.replay.parse_delivery_errors(self.replay.LOG_FILE),
            window_seconds=45,
        )

        self.assertEqual([candidate.path for candidate in candidates], [alert])
        self.assertIn("v0.1.135", candidates[0].content)
        self.assertTrue(candidates[0].digest)

    def test_state_deduplicates_replayed_output(self):
        alert = self.replay.OUTPUT_DIR / "2026-06-08_09-47-21.md"
        alert.write_text(ALERT_TEXT, encoding="utf-8")
        candidate = self.replay.ReplayCandidate(
            path=alert,
            content=ALERT_TEXT,
            digest=self.replay.sha256_text(ALERT_TEXT),
            error_time=datetime(2026, 6, 8, 9, 47, 32, tzinfo=timezone.utc),
            error_message="delivery error",
        )
        state = {"replayed": {str(alert): {"sha256": candidate.digest}}}

        self.assertTrue(self.replay.was_replayed(state, candidate))
        self.assertFalse(self.replay.was_replayed({"replayed": {}}, candidate))

    def test_send_candidate_persists_before_marking_replayed(self):
        alert = self.replay.OUTPUT_DIR / "2026-06-08_09-47-21.md"
        alert.write_text(ALERT_TEXT, encoding="utf-8")
        candidate = self.replay.ReplayCandidate(path=alert, content=ALERT_TEXT,
            digest=self.replay.sha256_text(ALERT_TEXT),
            error_time=datetime(2026, 6, 8, 9, 47, 32, tzinfo=timezone.utc), error_message="delivery error")
        result = self.replay.send_candidate(candidate, target="telegram:-1001234567890")
        self.assertTrue(result.ok)
        self.assertTrue(result.stdout.startswith("queued:"))
        self.assertIn("【补发】", self.queued_text())
        self.assertEqual(Outbox().counts(), {"pending": 1})

    def test_periodic_replay_checks_outbox_and_never_resends_legacy_logs(self):
        self.write_jobs()
        self.replay.LOG_FILE.write_text("2026-06-08 09:47:32,951 ERROR cron.scheduler: Job 'fa27c561ded6': delivery error: timeout\n")
        (self.replay.OUTPUT_DIR / "2026-06-08_09-47-21.md").write_text(ALERT_TEXT)
        self.assertTrue(self.replay.run_replay()["ok"])
        box = Outbox()
        event = box.enqueue(profile="webops", job_id=self.replay.MONITOR_JOB_ID, kind="text", payload={"text":"new"}, key="new")
        self.assertFalse(self.replay.run_replay()["ok"])
        self.assertEqual(box.counts(), {"pending": 1})
        box.finish(event, "delivered", receipt={"message_id":1})
        self.assertTrue(self.replay.run_replay()["ok"])
        self.assertEqual(box.counts(), {"delivered": 1})

    def test_raw_item_replay_finds_and_formats_codex_changelog_item(self):
        self.write_raw_item()
        fake_monitor, fake_translator = self.translated_fake_monitor()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor):
            candidates = self.replay.find_item_replay_candidates(
                source_id="openai_codex_changelog",
                item_id="https://developers.openai.com/codex/changelog/#github-release-334076532",
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "openai_codex_changelog")
        self.assertEqual(candidates[0].item_id, "https://developers.openai.com/codex/changelog/#github-release-334076532")
        self.assertIn("ZH:Codex CLI Release: 0.137.0", candidates[0].message)
        self.assertIn("ZH:0.137.0", candidates[0].message)
        self.assertEqual(candidates[0].mode, "raw_item")
        self.assertEqual(fake_translator.constructed[0]["cache_path"], fake_monitor.TRANSLATION_CACHE_FILE)

    def test_live_feed_replay_selects_item_by_source_and_title_regex(self):
        fake_monitor, _ = self.translated_fake_monitor()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor):
            candidates = self.replay.find_item_replay_candidates(
                source_id="openai_codex_cli_releases",
                title_regex=r"^0\.138\.0$",
                from_live_feed=True,
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].title, "0.138.0")
        self.assertEqual(candidates[0].mode, "live_feed")
        self.assertIn("OpenAI Codex CLI Releases", candidates[0].message)
        self.assertIn("ZH:0.138.0", candidates[0].message)
        self.assertIn("ZH:stable", candidates[0].message)

    def test_current_codex_stable_live_feed_replay_builds_chinese_backfill_message(self):
        fake_monitor = self.current_codex_cli_release_monitor()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor):
            candidates = self.replay.find_item_replay_candidates(
                source_id="openai_codex_cli_releases",
                title_regex=r"^0\.139\.0$",
                from_live_feed=True,
            )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].source_id, "openai_codex_cli_releases")
        self.assertEqual(candidates[0].item_id, "tag:github.com,2008:Repository/965415649/rust-v0.139.0")
        self.assertEqual(candidates[0].title, "0.139.0")
        self.assertEqual(candidates[0].mode, "live_feed")
        self.assertTrue(candidates[0].message.startswith("【补发】软件更新监控历史条目重放"))
        self.assertIn("OpenAI Codex CLI Releases", candidates[0].message)
        self.assertIn("ZH:0.139.0", candidates[0].message)
        self.assertIn("ZH:Codex CLI stable release 0.139.0", candidates[0].message)

    def test_current_codex_alpha_release_is_not_replayed_by_stable_title_filtering(self):
        fake_monitor = self.current_codex_cli_release_monitor()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor):
            candidates = self.replay.find_item_replay_candidates(
                source_id="openai_codex_cli_releases",
                title_regex=r"^0\.140\.0-alpha\.1$",
                from_live_feed=True,
            )

        self.assertEqual(candidates, [])

    def test_current_codex_live_feed_dry_run_does_not_send_and_send_dedupes_replayed_item(self):
        self.write_jobs()
        fake_monitor = self.current_codex_cli_release_monitor()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor), \
                mock.patch.object(self.replay.subprocess, "run") as dry_run_send:
            dry_run_summary = self.replay.run_item_replay(
                source_id="openai_codex_cli_releases",
                title_regex=r"^0\.139\.0$",
                from_live_feed=True,
                dry_run=True,
            )

        self.assertTrue(dry_run_summary["ok"])
        self.assertEqual(dry_run_summary["sent"], 0)
        self.assertEqual(dry_run_summary["candidates"], 1)
        self.assertEqual(dry_run_summary["items"][0]["title"], "0.139.0")
        dry_run_send.assert_not_called()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor), \
                mock.patch.object(self.replay.subprocess, "run") as send:
            send.return_value = mock.Mock(returncode=0, stdout='{"ok":true}', stderr="")
            send_summary = self.replay.run_item_replay(
                source_id="openai_codex_cli_releases",
                title_regex=r"^0\.139\.0$",
                from_live_feed=True,
            )

        self.assertTrue(send_summary["ok"])
        self.assertEqual(send_summary["sent"], 1)
        self.assertEqual(Outbox().counts(), {"pending": 1})
        self.assertEqual(self.queued_text().splitlines()[0], "【补发】软件更新监控历史条目重放")
        self.assertIn("ZH:0.139.0", self.queued_text())

        state = json.loads(self.replay.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn(
            "openai_codex_cli_releases:tag:github.com,2008:Repository/965415649/rust-v0.139.0",
            state["replayed_items"],
        )

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor), \
                mock.patch.object(self.replay.subprocess, "run") as repeat_send:
            repeat_summary = self.replay.run_item_replay(
                source_id="openai_codex_cli_releases",
                title_regex=r"^0\.139\.0$",
                from_live_feed=True,
            )

        self.assertTrue(repeat_summary["ok"])
        self.assertEqual(repeat_summary["sent"], 0)
        self.assertEqual(repeat_summary["candidates"], 0)
        repeat_send.assert_not_called()

    def test_item_replay_dry_run_does_not_send(self):
        self.write_jobs()
        self.write_raw_item()
        fake_monitor, _ = self.translated_fake_monitor()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor), \
                mock.patch.object(self.replay.subprocess, "run") as run:
            summary = self.replay.run_item_replay(
                source_id="openai_codex_changelog",
                item_id="https://developers.openai.com/codex/changelog/#github-release-334076532",
                dry_run=True,
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["sent"], 0)
        self.assertEqual(summary["candidates"], 1)
        self.assertEqual(summary["items"][0]["title"], "Codex CLI Release: 0.137.0")
        run.assert_not_called()

    def test_item_replay_sends_with_webops_home_and_deduplicates_replayed_items(self):
        self.write_jobs()
        self.write_raw_item()
        fake_monitor, _ = self.translated_fake_monitor()

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor), \
                mock.patch.object(self.replay.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout='{"ok":true}', stderr="")
            summary = self.replay.run_item_replay(
                source_id="openai_codex_changelog",
                item_id="https://developers.openai.com/codex/changelog/#github-release-334076532",
            )

        self.assertEqual(summary["sent"], 1)
        self.assertEqual(Outbox().counts(), {"pending": 1})
        self.assertEqual(self.queued_text().splitlines()[0], "【补发】软件更新监控历史条目重放")
        self.assertIn("ZH:Codex CLI Release: 0.137.0", self.queued_text())

        state = json.loads(self.replay.STATE_FILE.read_text(encoding="utf-8"))
        self.assertIn(
            "openai_codex_changelog:https://developers.openai.com/codex/changelog/#github-release-334076532",
            state["replayed_items"],
        )

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor), \
                mock.patch.object(self.replay.subprocess, "run") as second_run:
            second = self.replay.run_item_replay(
                source_id="openai_codex_changelog",
                item_id="https://developers.openai.com/codex/changelog/#github-release-334076532",
            )

        self.assertEqual(second["sent"], 0)
        second_run.assert_not_called()

    def test_item_replay_deduplicates_by_item_key_even_when_message_sha_changes(self):
        self.write_jobs()
        self.write_raw_item()
        fake_monitor, _ = self.translated_fake_monitor()
        key = "openai_codex_changelog:https://developers.openai.com/codex/changelog/#github-release-334076532"
        self.replay.save_state(
            {
                "replayed": {},
                "replayed_items": {
                    key: {
                        "sha256": "old-english-message-digest",
                        "title": "Codex CLI Release: 0.137.0",
                    }
                },
            }
        )

        with mock.patch.object(self.replay, "load_monitor_module", return_value=fake_monitor), \
                mock.patch.object(self.replay.subprocess, "run") as run:
            summary = self.replay.run_item_replay(
                source_id="openai_codex_changelog",
                item_id="https://developers.openai.com/codex/changelog/#github-release-334076532",
            )

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["sent"], 0)
        self.assertEqual(summary["candidates"], 0)
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
