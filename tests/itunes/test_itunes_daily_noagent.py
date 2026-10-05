import contextlib
import datetime as dt
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pylon.outbox import Outbox
from tests.helpers import load_script_module

noagent = load_script_module("itunes_daily_noagent", "itunes_daily_noagent.py")


def finalization(market, date, report_json):
    return {
        "market": market,
        "release_date": date,
        "reports": {"report_json": str(report_json)},
        "summary": {"tracks_after_filter": 1},
    }


def report_payload(market, date):
    return {
        "aggregation": {
            "market": market,
            "release_date": date,
            "unique_tracks_before_filter": 1,
            "tracks_after_filter": 1,
            "dropped_count": 0,
            "tracks": [
                {
                    "final_rank": 1,
                    "title": f"{market.upper()} Song",
                    "artist": "Artist",
                    "album": "Album",
                    "duration_label": "3:00",
                    "total_points": 100,
                    "best_rank": 1,
                    "occurrence_count": 2,
                    "apple_track_id": f"{market}-1",
                    "canonical_key": f"apple:{market}-1",
                    "source_track_url": f"https://music.apple.com/{market}/album/x?i={market}-1",
                    "preview_url": f"https://audio.example/{market}.m4a",
                    "artwork_url": f"https://img.example/{market}.jpg",
                }
            ],
        }
    }


def empty_report_payload(market, date):
    payload = report_payload(market, date)
    payload["aggregation"]["unique_tracks_before_filter"] = 0
    payload["aggregation"]["tracks_after_filter"] = 0
    payload["aggregation"]["tracks"] = []
    return payload


def report_payload_with_switch(market, date, switch_detected_at_utc):
    payload = report_payload(market, date)
    payload["switch_info"] = {
        "switch_detected_at_utc": switch_detected_at_utc,
    }
    return payload


class ChartooItunesDailyNoAgentHtmlTests(unittest.TestCase):
    def setUp(self):
        self.original_fetch_livedoor_entries = noagent.fetch_livedoor_entries
        noagent.fetch_livedoor_entries = lambda release_date: []

    def tearDown(self):
        noagent.fetch_livedoor_entries = self.original_fetch_livedoor_entries

    def test_livedoor_history_paths_use_previous_three_calendar_days_and_skip_invalid_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            for date in ("2026-07-07", "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-11"):
                for market in ("jp", "hk"):
                    path = reports / f"{date}-{market}.json"
                    path.write_text(json.dumps(report_payload(market, date)), encoding="utf-8")
            (reports / "2026-07-09-hk.json").write_text("{", encoding="utf-8")
            (reports / "2026-07-08-jp.json").unlink()

            original_data_root = noagent.DATA_ROOT
            try:
                noagent.DATA_ROOT = root
                paths = noagent.livedoor_history_report_paths("2026-07-11")
            finally:
                noagent.DATA_ROOT = original_data_root

        self.assertEqual(noagent.LIVEDOOR_SUPPRESSION_DAYS, 3)
        self.assertEqual(
            [path.name for path in paths],
            [
                "2026-07-10-jp.json",
                "2026-07-10-hk.json",
                "2026-07-09-jp.json",
                "2026-07-08-hk.json",
            ],
        )

    def test_write_html_suppresses_d_minus_three_from_either_market_but_keeps_d_minus_four(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            for market in ("jp", "hk"):
                (reports / f"2026-07-11-{market}.json").write_text(
                    json.dumps(report_payload(market, "2026-07-11")),
                    encoding="utf-8",
                )
            inside_payload = report_payload("hk", "2026-07-08")
            inside_payload["aggregation"]["tracks"][0]["source_track_url"] = (
                "https://music.apple.com/hk/album/3000000000?i=3000000001"
            )
            outside_payload = report_payload("jp", "2026-07-07")
            outside_payload["aggregation"]["tracks"][0]["source_track_url"] = (
                "https://music.apple.com/jp/album/4000000000?i=4000000001"
            )
            (reports / "2026-07-08-hk.json").write_text(json.dumps(inside_payload), encoding="utf-8")
            (reports / "2026-07-07-jp.json").write_text(json.dumps(outside_payload), encoding="utf-8")
            inside_entry = noagent.livedoor_reference.LivedoorEntry(
                album_id="3000000000",
                title="Inside window",
                url="https://music.apple.com/hk/album/3000000000",
                body_text="Inside window body",
            )
            outside_entry = noagent.livedoor_reference.LivedoorEntry(
                album_id="4000000000",
                title="Outside window",
                url="https://music.apple.com/jp/album/4000000000",
                body_text="Outside window body",
            )

            original_data_root = noagent.DATA_ROOT
            noagent.DATA_ROOT = root
            noagent.fetch_livedoor_entries = lambda release_date: [inside_entry, outside_entry]
            try:
                result = noagent.write_html_for_date("2026-07-11", finalizations=[])
            finally:
                noagent.DATA_ROOT = original_data_root

        self.assertIsNotNone(result)
        self.assertEqual(result.livedoor_suppressed, [inside_entry])
        self.assertEqual(result.livedoor_unmatched, [outside_entry])
        self.assertNotIn("Inside window body", result.html)
        self.assertIn("Outside window body", result.html)

    def test_main_waits_silently_until_both_markets_are_finalized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            jp.write_text(json.dumps(report_payload("jp", "2026-05-31")), encoding="utf-8")
            payload = {"action": "finalize", "finalizations": [finalization("jp", "2026-05-31", jp)]}

            original_run_monitor = noagent.run_monitor
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.run_monitor = lambda: json.dumps(payload, ensure_ascii=False)
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(noagent.main(), 0)
            finally:
                noagent.run_monitor = original_run_monitor
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

        self.assertEqual(out.getvalue().strip(), '{"wakeAgent": false}')

    def test_main_generates_and_sends_dual_market_html_with_total_count_caption(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            jp = reports / "2026-05-31-jp.json"
            hk = reports / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload("jp", "2026-05-31")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "2026-05-31")), encoding="utf-8")
            payload = {
                "action": "finalize",
                "finalizations": [
                    finalization("jp", "2026-05-31", jp),
                    finalization("hk", "2026-05-31", hk),
                ],
            }
            sent = []

            original_run_monitor = noagent.run_monitor
            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.run_monitor = lambda: json.dumps(payload, ensure_ascii=False)
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: sent.append((result.path, result.caption))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(noagent.main(), 0)
            finally:
                noagent.run_monitor = original_run_monitor
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

            html_path = root / "html" / "2026-05-31-chartoo.html"
            state = json.loads((root / "html_notifications.json").read_text(encoding="utf-8"))
            html_exists = html_path.exists()
            html_text = html_path.read_text(encoding="utf-8")
            tunemymusic_dir_exists = (root / "tunemymusic").exists()

        self.assertEqual(out.getvalue().strip(), '{"wakeAgent": false}')
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], html_path)
        self.assertEqual(sent[0][1], "Chartoo 2026-05-31\n总歌曲数：2")
        self.assertNotIn("JP Song", sent[0][1])
        self.assertNotIn("HK Song", sent[0][1])
        self.assertTrue(html_exists)
        self.assertFalse(tunemymusic_dir_exists)
        self.assertNotIn("TuneMyMusic", html_text)
        self.assertNotIn("导入文本", html_text)
        self.assertNotIn("Artist – JP Song", html_text)
        self.assertNotIn("Artist – HK Song", html_text)
        self.assertIn("2026-05-31", state)
        self.assertEqual(state["2026-05-31"]["markets"], ["hk", "jp"])
        self.assertIn("artifacts", state["2026-05-31"])
        self.assertIn("html", state["2026-05-31"]["artifacts"])

    def test_main_skips_complete_date_when_audio_queue_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            jp = reports / "2026-06-14-jp.json"
            hk = reports / "2026-06-14-hk.json"
            jp.write_text(json.dumps(empty_report_payload("jp", "2026-06-14")), encoding="utf-8")
            hk.write_text(json.dumps(empty_report_payload("hk", "2026-06-14")), encoding="utf-8")
            payload = {
                "action": "finalize",
                "finalizations": [
                    finalization("jp", "2026-06-14", jp),
                    finalization("hk", "2026-06-14", hk),
                ],
            }
            sent = []

            original_run_monitor = noagent.run_monitor
            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.run_monitor = lambda: json.dumps(payload, ensure_ascii=False)
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: sent.append(result.path)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(noagent.main(), 0)
            finally:
                noagent.run_monitor = original_run_monitor
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

            html_path = root / "html" / "2026-06-14-chartoo.html"
            state_path = root / "html_notifications.json"
            html_exists = html_path.exists()
            state_exists = state_path.exists()

        self.assertEqual(out.getvalue().strip(), '{"wakeAgent": false}')
        self.assertEqual(sent, [])
        self.assertTrue(html_exists)
        self.assertFalse(state_exists)

    def test_main_skips_complete_date_when_tracks_have_no_preview_urls(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            jp = reports / "2026-06-14-jp.json"
            hk = reports / "2026-06-14-hk.json"
            jp_payload = report_payload("jp", "2026-06-14")
            hk_payload = report_payload("hk", "2026-06-14")
            jp_payload["aggregation"]["tracks"][0]["preview_url"] = ""
            hk_payload["aggregation"]["tracks"][0]["preview_url"] = ""
            jp.write_text(json.dumps(jp_payload), encoding="utf-8")
            hk.write_text(json.dumps(hk_payload), encoding="utf-8")
            sent = []

            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: sent.append(result.path)
                text = noagent.run_for_dates(["2026-06-14"], finalizations=[])
            finally:
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

            state_path = root / "html_notifications.json"
            state_exists = state_path.exists()

        self.assertEqual(text, '{"wakeAgent": false}')
        self.assertEqual(sent, [])
        self.assertFalse(state_exists)

    def test_main_suppresses_duplicate_html_report_by_delivered_date(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            jp = reports / "2026-05-31-jp.json"
            hk = reports / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload("jp", "2026-05-31")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "2026-05-31")), encoding="utf-8")
            payload = {
                "action": "finalize",
                "finalizations": [
                    finalization("jp", "2026-05-31", jp),
                    finalization("hk", "2026-05-31", hk),
                ],
            }
            (root / "html_notifications.json").write_text(
                json.dumps(
                    {
                        "2026-05-31": {
                            "content_hash": "old-template-hash",
                            "markets": ["hk", "jp"],
                            "path": str(root / "html" / "2026-05-31-chartoo.html"),
                            "sent_at_utc": "2026-06-02T06:30:21Z",
                        }
                    }
                ),
                encoding="utf-8",
            )
            sent = []

            original_run_monitor = noagent.run_monitor
            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.run_monitor = lambda: json.dumps(payload, ensure_ascii=False)
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: sent.append(result.path)
                self.assertEqual(noagent.main(), 0)
                self.assertEqual(noagent.main(), 0)
            finally:
                noagent.run_monitor = original_run_monitor
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

        self.assertEqual(sent, [])

    def test_force_send_overrides_delivered_date_lock(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            jp = reports / "2026-05-31-jp.json"
            hk = reports / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload("jp", "2026-05-31")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "2026-05-31")), encoding="utf-8")
            (root / "html_notifications.json").write_text(
                json.dumps(
                    {
                        "2026-05-31": {
                            "content_hash": "old-template-hash",
                            "markets": ["hk", "jp"],
                            "path": str(root / "html" / "2026-05-31-chartoo.html"),
                            "sent_at_utc": "2026-06-02T06:30:21Z",
                        }
                    }
                ),
                encoding="utf-8",
            )
            sent = []

            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: sent.append(result.path.name)
                self.assertEqual(
                    noagent.run_for_dates(["2026-05-31"], finalizations=[], force_send=True),
                    '{"wakeAgent": false}',
                )
            finally:
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

        self.assertEqual(sent, ["2026-05-31-chartoo.html"])

    def test_delivered_date_lock_requires_sent_at_and_both_markets(self):
        class Result:
            release_date = "2026-05-31"
            content_hash = "new-hash"

        self.assertFalse(
            noagent.report_already_sent(
                Result(),
                {"2026-05-31": {"content_hash": "old-hash", "markets": ["hk", "jp"]}},
            )
        )
        self.assertFalse(
            noagent.report_already_sent(
                Result(),
                {"2026-05-31": {"content_hash": "old-hash", "markets": ["jp"], "sent_at_utc": "2026-06-02T06:30:21Z"}},
            )
        )
        self.assertTrue(
            noagent.report_already_sent(
                Result(),
                {"2026-05-31": {"content_hash": "old-hash", "markets": ["jp", "hk"], "sent_at_utc": "2026-06-02T06:30:21Z"}},
            )
        )

    def test_artifact_state_only_requires_html_sent(self):
        class Result:
            release_date = "2026-05-31"
            content_hash = "new-hash"

        self.assertTrue(
            noagent.report_already_sent(
                Result(),
                {
                    "2026-05-31": {
                        "markets": ["hk", "jp"],
                        "artifacts": {
                            "html": {"sent_at_utc": "2026-06-02T06:30:21Z"},
                        },
                    }
                },
            )
        )

    def test_run_for_dates_persists_each_success_before_later_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            for date in ("2026-05-30", "2026-05-31"):
                for market in ("jp", "hk"):
                    (reports / f"{date}-{market}.json").write_text(
                        json.dumps(report_payload(market, date)),
                        encoding="utf-8",
                    )

            def fake_send(result):
                if result.release_date == "2026-05-31":
                    raise RuntimeError("send failed")
                return {"ok": True}

            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = fake_send
                text = noagent.run_for_dates(
                    ["2026-05-30", "2026-05-31"],
                    finalizations=[],
                )
            finally:
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

            state = json.loads((root / "html_notifications.json").read_text(encoding="utf-8"))

        self.assertIn("Chartoo HTML 日报推送失败", text)
        self.assertIn("2026-05-31", text)
        self.assertIn("2026-05-30", state)
        self.assertNotIn("2026-05-31", state)
        self.assertEqual(state["2026-05-30"]["markets"], ["hk", "jp"])
        self.assertEqual(set(state["2026-05-30"]["artifacts"]), {"html"})

    def test_main_surfaces_telegram_send_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            jp = reports / "2026-05-31-jp.json"
            hk = reports / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload("jp", "2026-05-31")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "2026-05-31")), encoding="utf-8")
            payload = {
                "action": "finalize",
                "finalizations": [
                    finalization("jp", "2026-05-31", jp),
                    finalization("hk", "2026-05-31", hk),
                ],
            }

            original_run_monitor = noagent.run_monitor
            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.run_monitor = lambda: json.dumps(payload, ensure_ascii=False)
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: (_ for _ in ()).throw(RuntimeError("send failed"))
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(noagent.main(), 0)
            finally:
                noagent.run_monitor = original_run_monitor
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

        self.assertIn("Chartoo HTML 日报推送失败", out.getvalue())
        self.assertIn("2026-05-31", out.getvalue())

    def test_main_scans_recent_two_complete_dates_when_monitor_is_silent(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            for date in ("2026-05-29", "2026-05-30", "2026-05-31"):
                for market in ("jp", "hk"):
                    (reports / f"{date}-{market}.json").write_text(
                        json.dumps(report_payload(market, date)),
                        encoding="utf-8",
                    )
            sent = []

            original_run_monitor = noagent.run_monitor
            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.run_monitor = lambda: '{"wakeAgent": false}'
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: sent.append(result.path.name)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(noagent.main(), 0)
            finally:
                noagent.run_monitor = original_run_monitor
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

        self.assertEqual(out.getvalue().strip(), '{"wakeAgent": false}')
        self.assertEqual(
            sent,
            [
                "2026-05-30-chartoo.html",
                "2026-05-31-chartoo.html",
            ],
        )

    def test_main_scan_skips_dates_missing_one_market(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            (reports / "2026-05-31-jp.json").write_text(
                json.dumps(report_payload("jp", "2026-05-31")),
                encoding="utf-8",
            )
            sent = []

            original_run_monitor = noagent.run_monitor
            original_send_html_report = noagent.send_html_report
            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            try:
                noagent.run_monitor = lambda: '{"wakeAgent": false}'
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.send_html_report = lambda result: sent.append(result.path.name)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(noagent.main(), 0)
            finally:
                noagent.run_monitor = original_run_monitor
                noagent.send_html_report = original_send_html_report
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state

        self.assertEqual(out.getvalue().strip(), '{"wakeAgent": false}')
        self.assertEqual(sent, [])

    def test_partial_boundary_is_immediate_and_candidate_requires_switch_metadata(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            date = "2026-06-01"
            report = reports / f"{date}-jp.json"
            report.write_text(
                json.dumps(report_payload_with_switch("jp", date, "2026-06-02T13:30:00Z")),
                encoding="utf-8",
            )

            original_data_root = noagent.DATA_ROOT
            original_now_utc = noagent.now_utc
            try:
                noagent.DATA_ROOT = root
                noagent.now_utc = lambda: "2026-06-02T13:29:59Z"
                before = dt.datetime(2026, 6, 2, 13, 29, 59, tzinfo=dt.timezone.utc)
                at_switch = dt.datetime(2026, 6, 2, 13, 30, 0, tzinfo=dt.timezone.utc)
                self.assertFalse(noagent._partial_report_ready([report], now=before))
                self.assertTrue(noagent._partial_report_ready([report], now=at_switch))
                self.assertEqual(noagent.candidate_dates(), [])
                noagent.now_utc = lambda: "2026-06-02T13:30:00Z"
                self.assertEqual(noagent.candidate_dates(), [date])
            finally:
                noagent.DATA_ROOT = original_data_root
                noagent.now_utc = original_now_utc

    def test_partial_requires_switch_metadata_and_does_not_invent_readiness(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            date = "2026-06-02"
            report = reports / f"{date}-jp.json"
            report.write_text(json.dumps(report_payload("jp", date)), encoding="utf-8")
            sent = []

            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            original_send_html_report = noagent.send_html_report
            original_now_utc = noagent.now_utc
            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.now_utc = lambda: "2026-06-03T00:00:00Z"
                noagent.send_html_report = lambda result: sent.append(result)
                self.assertEqual(noagent.candidate_dates(), [])
                self.assertEqual(
                    noagent.run_for_dates([date], finalizations=[], allow_partial=True),
                    '{"wakeAgent": false}',
                )
            finally:
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state
                noagent.send_html_report = original_send_html_report
                noagent.now_utc = original_now_utc

            self.assertEqual(sent, [])
            self.assertFalse((root / "html_notifications.json").exists())

    def test_partial_sends_once_across_state_reload_then_full_once_on_market_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            date = "2026-06-03"
            jp = reports / f"{date}-jp.json"
            hk = reports / f"{date}-hk.json"
            jp.write_text(
                json.dumps(report_payload_with_switch("jp", date, "2026-06-04T13:30:00Z")),
                encoding="utf-8",
            )
            sent = []

            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            original_send_html_report = noagent.send_html_report
            original_now_utc = noagent.now_utc
            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.now_utc = lambda: "2026-06-04T13:30:00Z"
                noagent.send_html_report = lambda result: sent.append(
                    (result.artifact_key, result.missing_markets, result.caption)
                )

                self.assertEqual(
                    noagent.run_for_dates([date], finalizations=[], allow_partial=True),
                    '{"wakeAgent": false}',
                )
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0][0:2], ("html-partial", ("hk",)))
                self.assertIn("HK 数据尚未到齐", sent[0][2])

                reloaded = load_script_module("itunes_daily_noagent_reloaded", "itunes_daily_noagent.py")
                reloaded.DATA_ROOT = root
                reloaded.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                reloaded.now_utc = lambda: "2026-06-04T13:30:00Z"
                reloaded.fetch_livedoor_entries = lambda release_date: []
                reloaded.send_html_report = lambda result: sent.append(
                    (result.artifact_key, result.missing_markets, result.caption)
                )
                self.assertEqual(
                    reloaded.run_for_dates([date], finalizations=[], allow_partial=True),
                    '{"wakeAgent": false}',
                )
                self.assertEqual(len(sent), 1)

                hk.write_text(
                    json.dumps(report_payload_with_switch("hk", date, "2026-06-04T13:35:00Z")),
                    encoding="utf-8",
                )
                self.assertEqual(
                    reloaded.run_for_dates([date], finalizations=[], allow_partial=True),
                    '{"wakeAgent": false}',
                )
                self.assertEqual(len(sent), 2)
                self.assertEqual(sent[1][0:2], ("html", ()))

                self.assertEqual(
                    reloaded.run_for_dates([date], finalizations=[], allow_partial=True),
                    '{"wakeAgent": false}',
                )
                self.assertEqual(len(sent), 2)
            finally:
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state
                noagent.send_html_report = original_send_html_report
                noagent.now_utc = original_now_utc

            state = json.loads((root / "html_notifications.json").read_text(encoding="utf-8"))

        self.assertEqual(set(state[date]["markets"]), {"jp", "hk"})
        self.assertEqual(state[date]["partial_markets"], ["jp"])
        self.assertEqual(set(state[date]["artifacts"]), {"html-partial", "html"})

    def test_existing_full_delivery_does_not_fall_back_to_partial(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            date = "2026-06-04"
            jp = reports / f"{date}-jp.json"
            jp.write_text(
                json.dumps(report_payload_with_switch("jp", date, "2026-06-05T13:30:00Z")),
                encoding="utf-8",
            )
            state_path = root / "html_notifications.json"
            state_path.write_text(
                json.dumps(
                    {
                        date: {
                            "markets": ["jp", "hk"],
                            "sent_at_utc": "2026-06-04T02:00:00Z",
                            "artifacts": {
                                "html": {
                                    "sent_at_utc": "2026-06-04T02:00:00Z",
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            sent = []

            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            original_send_html_report = noagent.send_html_report
            original_now_utc = noagent.now_utc
            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = state_path
                noagent.now_utc = lambda: "2026-06-05T13:30:00Z"
                noagent.send_html_report = lambda result: sent.append(result)
                self.assertEqual(
                    noagent.run_for_dates([date], finalizations=[], allow_partial=True),
                    '{"wakeAgent": false}',
                )
            finally:
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state
                noagent.send_html_report = original_send_html_report
                noagent.now_utc = original_now_utc

        self.assertEqual(sent, [])

    def test_outbox_stage_key_deduplicates_after_state_save_failure_and_content_change(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            date = "2026-06-05"
            jp = reports / f"{date}-jp.json"
            jp.write_text(
                json.dumps(report_payload_with_switch("jp", date, "2026-06-06T13:30:00Z")),
                encoding="utf-8",
            )
            hk = reports / f"{date}-hk.json"
            outbox_path = root / "outbox.sqlite3"
            sent = []

            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            original_send_html_report = noagent.send_html_report
            original_save_notified = noagent.save_notified
            original_now_utc = noagent.now_utc

            def fail_save(*args, **kwargs):
                raise OSError("injected state save failure")

            def document_count():
                with Outbox(outbox_path).connect() as db:
                    return db.execute("SELECT count(*) FROM events WHERE kind='document'").fetchone()[0]

            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.now_utc = lambda: "2026-06-06T13:30:00Z"
                noagent.send_html_report = original_send_html_report
                with mock.patch.dict(
                    os.environ,
                    {
                        "PYLON_OUTBOX": str(outbox_path),
                        "PYLON_PROFILE": "webops",
                        "PYLON_JOB_ID": "itunes-daily-test",
                    },
                    clear=False,
                ):
                    noagent.save_notified = fail_save
                    with self.assertRaisesRegex(OSError, "injected state save failure"):
                        noagent.run_for_dates([date], finalizations=[], allow_partial=True)
                    self.assertEqual(document_count(), 1)

                    jp_payload = json.loads(jp.read_text(encoding="utf-8"))
                    jp_payload["aggregation"]["tracks"][0]["title"] = "JP Song v2"
                    jp.write_text(json.dumps(jp_payload), encoding="utf-8")

                    noagent.save_notified = original_save_notified
                    self.assertEqual(
                        noagent.run_for_dates([date], finalizations=[], allow_partial=True),
                        '{"wakeAgent": false}',
                    )
                    self.assertEqual(document_count(), 1)

                    hk.write_text(
                        json.dumps(report_payload_with_switch("hk", date, "2026-06-06T13:35:00Z")),
                        encoding="utf-8",
                    )
                    noagent.save_notified = fail_save
                    with self.assertRaisesRegex(OSError, "injected state save failure"):
                        noagent.run_for_dates([date], finalizations=[], allow_partial=True)
                    self.assertEqual(document_count(), 2)

                    hk_payload = json.loads(hk.read_text(encoding="utf-8"))
                    hk_payload["aggregation"]["tracks"][0]["title"] = "HK Song v2"
                    hk.write_text(json.dumps(hk_payload), encoding="utf-8")

                    noagent.save_notified = original_save_notified
                    self.assertEqual(
                        noagent.run_for_dates([date], finalizations=[], allow_partial=True),
                        '{"wakeAgent": false}',
                    )
                    self.assertEqual(document_count(), 2)
            finally:
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state
                noagent.send_html_report = original_send_html_report
                noagent.save_notified = original_save_notified
                noagent.now_utc = original_now_utc

            state = json.loads((root / "html_notifications.json").read_text(encoding="utf-8"))

        self.assertEqual(set(state[date]["artifacts"]), {"html-partial", "html"})

    def test_old_partial_state_stays_a_candidate_until_market_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            reports.mkdir()
            date = "2026-05-01"
            jp = reports / f"{date}-jp.json"
            hk = reports / f"{date}-hk.json"
            jp.write_text(
                json.dumps(report_payload_with_switch("jp", date, "2026-05-02T13:30:00Z")),
                encoding="utf-8",
            )
            (root / "html_notifications.json").write_text(
                json.dumps(
                    {
                        date: {
                            "partial_markets": ["jp"],
                            "partial_missing_markets": ["hk"],
                            "partial_sent_at_utc": "2026-05-02T13:30:00Z",
                            "artifacts": {
                                "html-partial": {
                                    "sent_at_utc": "2026-05-02T13:30:00Z",
                                },
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            sent = []

            original_data_root = noagent.DATA_ROOT
            original_notify_state = noagent.DEFAULT_NOTIFY_STATE
            original_send_html_report = noagent.send_html_report
            original_now_utc = noagent.now_utc
            try:
                noagent.DATA_ROOT = root
                noagent.DEFAULT_NOTIFY_STATE = root / "html_notifications.json"
                noagent.now_utc = lambda: "2026-10-05T00:00:00Z"
                noagent.send_html_report = lambda result: sent.append(
                    (result.artifact_key, result.caption)
                )
                self.assertIn(date, noagent.candidate_dates())

                hk.write_text(
                    json.dumps(report_payload_with_switch("hk", date, "2026-10-05T00:00:00Z")),
                    encoding="utf-8",
                )
                self.assertEqual(
                    noagent.run_for_dates([date], finalizations=[], allow_partial=True),
                    '{"wakeAgent": false}',
                )
            finally:
                noagent.DATA_ROOT = original_data_root
                noagent.DEFAULT_NOTIFY_STATE = original_notify_state
                noagent.send_html_report = original_send_html_report
                noagent.now_utc = original_now_utc

            state = json.loads((root / "html_notifications.json").read_text(encoding="utf-8"))

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], "html")
        self.assertIn("补全", sent[0][1])
        self.assertEqual(set(state[date]["markets"]), {"jp", "hk"})


if __name__ == "__main__":
    unittest.main()
