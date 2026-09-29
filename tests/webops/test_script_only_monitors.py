import contextlib
import copy
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pylon.tasks.forums import linuxdo
from pylon.tasks.forums import v2ex
from pylon.shared import state_json


class ScriptOnlyMonitorTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(sys.modules, {"openai": None, "anthropic": None, "litellm": None}))
        self.enterContext(mock.patch.object(socket, "create_connection", side_effect=AssertionError("unexpected network")))

    def v2ex_post(self, message_id, title=None):
        title = title or "【二手交易】出 iPhone 15 Pro 256G 明盘 3999"
        tags, core = v2ex.extract_tags_and_core(title)
        return v2ex.Post(message_id, f"https://t.me/v2ex_notice/{message_id}",
                         f"https://www.v2ex.com/t/{message_id}", "", title, tags, core,
                         v2ex.classify(tags, title, core), "全新未拆，无锁，走闲鱼可邮寄")

    def linuxdo_post(self, topic_id, *, unknown=False):
        return linuxdo.LinuxDoPost(
            channel="LinuxDoNew", message_id=int(topic_id),
            telegram_url=f"https://t.me/LinuxDoNew/{topic_id}", posted_at="",
            topic_id=topic_id, topic_url=f"https://linux.do/t/topic/{topic_id}",
            topic_post_no="", title="AI 公益站新开放" if unknown else "Codex 公益站开放不限额度",
            author="#tester", section="#福利羊毛", topic_created_at="", source_role="primary",
            summary="Base URL: https://resource.example/v1，key填任意值，不限额度。" + ("模型未列明" if unknown else "支持 GPT-5.4 Codex"),
        )

    def run_v2ex(self, state, posts, *, dry_run=False):
        saved = []
        out = io.StringIO()
        with (mock.patch.object(v2ex, "load_state", return_value=state),
              mock.patch.object(v2ex, "save_state", side_effect=lambda s: saved.append(copy.deepcopy(s))),
              mock.patch.object(v2ex, "collect_recent_pages", return_value=(posts, {"hit_last_seen": True})),
              mock.patch.object(v2ex, "enrich_posts_with_topic_excerpt"),
              mock.patch.object(v2ex, "collect_arbitrage_messages", return_value=[]) as arbitrage,
              contextlib.redirect_stdout(out)):
            self.assertEqual(v2ex.run_once(dry_run=dry_run), 0)
        return out.getvalue(), saved, arbitrage

    def test_v2ex_queue_resumes_and_only_advances_cursor_after_draining(self):
        original = {"last_seen_id": 100}
        text, saved, _ = self.run_v2ex(original, [self.v2ex_post(103), self.v2ex_post(102)])
        self.assertIn("/103)", text)
        self.assertEqual(saved[-1]["last_seen_id"], 100)
        text2, saved2, _ = self.run_v2ex(saved[-1], [])
        self.assertIn("/102)", text2)
        self.assertEqual(saved2[-1]["last_seen_id"], 103)
        self.assertNotIn(v2ex.AGENT_BATCH_STATE_KEY, saved2[-1])
        self.assertEqual(original, {"last_seen_id": 100})

    def test_v2ex_revalidates_old_queue_and_skips_excluded_item(self):
        state = {"last_seen_id": 100}
        blocked = self.v2ex_post(102, "【二手交易】出京东 e 卡 1000 元")
        good = self.v2ex_post(103)
        batch = v2ex._agent_batch_initialize(state, source_last_seen_id=100, target_last_seen_id=103,
            items=v2ex.build_agent_delivery_queue([blocked, good], []), now_iso="")
        text = v2ex._agent_batch_process_current(state, batch, "2026-09-07T00:00:00Z")
        self.assertIn("/103)", text)
        self.assertNotIn("/102)", text)
        self.assertEqual(state["last_seen_id"], 103)

    def test_v2ex_dry_run_does_not_persist_or_record_arbitrage(self):
        state = {"last_seen_id": 100}
        text, saved, arbitrage = self.run_v2ex(state, [self.v2ex_post(101)], dry_run=True)
        self.assertIn("/101)", text)
        self.assertEqual(saved, [])
        arbitrage.assert_not_called()
        self.assertEqual(state, {"last_seen_id": 100})

    def test_unknown_text_never_becomes_a_push_from_model_settings_or_old_ocr(self):
        post = self.linuxdo_post("101", unknown=True)
        post.photo_urls = ["https://resource.example/image.png"]
        state = {"image_ocr_cache": {post.photo_urls[0]: {"result": {"model_names": ["GPT-5.4"], "frontline_hint": "frontline"}}}}
        config = {**linuxdo.default_config(), "image_ocr_enabled": True, "unknown_ai_public_site_policy": "model"}
        with mock.patch.object(linuxdo, "fetch_topic_evidence", return_value={}):
            pushes, audit = linuxdo._candidate_items([post], config, state)
        self.assertEqual(pushes, [])
        self.assertEqual(audit[0]["decision"], "silent")
        self.assertIn("unknown_ai_public_site", audit[0]["post"]["routing_context"]["matched_categories"])

    def test_linuxdo_processes_all_candidates_and_resumes_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            log_path = Path(tmp) / "decisions.jsonl"
            posts = [self.linuxdo_post("101"), self.linuxdo_post("102")]
            with (mock.patch.object(linuxdo, "STATE_FILE", state_path),
                  mock.patch.object(linuxdo, "DECISION_LOG_FILE", log_path),
                  mock.patch.object(linuxdo, "load_config", return_value=linuxdo.default_config()),
                  mock.patch.object(linuxdo, "collect_recent_posts", return_value=(posts, {})),
                  mock.patch.object(linuxdo, "fetch_topic_evidence", return_value={})):
                first = linuxdo.run_once()
                second = linuxdo.run_once()
                third = linuxdo.run_once()
            self.assertIn("/101)", first)
            self.assertIn("/102)", second)
            self.assertEqual(json.loads(third), {"wakeAgent": False})
            state = json.loads(state_path.read_text())
            self.assertEqual(set(state["delivered_topics"]), {"101", "102"})
            self.assertEqual(state[linuxdo.DELIVERY_QUEUE_STATE_KEY], [])
            self.assertIn('"rule_version": "deterministic-v1"', log_path.read_text())

    def test_linuxdo_dry_run_leaves_no_state_or_log(self):
        with (mock.patch.object(linuxdo, "load_config", return_value=linuxdo.default_config()),
              mock.patch.object(linuxdo, "load_state", return_value={}),
              mock.patch.object(linuxdo, "collect_recent_posts", return_value=([self.linuxdo_post("101")], {})),
              mock.patch.object(linuxdo, "fetch_topic_evidence", return_value={}),
              mock.patch.object(linuxdo, "save_state") as save,
              mock.patch.object(linuxdo, "append_decision_log_entries") as log):
            result = json.loads(linuxdo.run_once(dry_run=True))
        self.assertEqual(len(result["push_messages"]), 1)
        save.assert_not_called()
        log.assert_not_called()

    def test_low_signal_enrichment_has_no_retired_digest_dependency(self):
        post = self.v2ex_post(101, "【生活】今天天气")
        with mock.patch.object(v2ex, "fetch", side_effect=AssertionError("unnecessary fetch")):
            v2ex.enrich_posts_with_topic_excerpt([post])

    def test_linuxdo_queue_preserves_more_than_fifty_confirmed_items(self):
        state = {}
        config = {**linuxdo.default_config(), "delivery_queue_max": 50}
        messages = [f"[item](https://linux.do/t/topic/{i})" for i in range(100, 160)]
        linuxdo._enqueue_push_messages(state, messages, config)
        actual = [linuxdo._pop_next_queued_delivery(state, config) for _ in messages]
        self.assertEqual(actual, messages)
        self.assertEqual(len(state["delivered_topics"]), 60)

    def test_atomic_save_failure_keeps_previous_queue_and_removes_temp_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            initial = {"last_seen_id": 100, "queue": ["pending"]}
            state_json.save_atomic(path, initial)
            before = path.read_bytes()
            with mock.patch.object(state_json.os, "replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    state_json.save_atomic(path, {"last_seen_id": 101})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_corrupt_monitor_state_fails_without_resetting_cursor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text('{"last_seen_id":')
            for module in (v2ex, linuxdo):
                with mock.patch.object(module, "STATE_FILE", path):
                    with self.assertRaises(json.JSONDecodeError):
                        module.load_state()


if __name__ == "__main__":
    unittest.main()
