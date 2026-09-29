import json
import tempfile
import unittest
from pathlib import Path

from chartoo.lib import html_report, livedoor_reference


def report_payload(market, title, *, apple_track_id, points=100, best_rank=1, occurrences=3):
    return {
        "aggregation": {
            "market": market,
            "release_date": "2026-05-31",
            "unique_tracks_before_filter": 1,
            "tracks_after_filter": 1,
            "dropped_count": 0,
            "tracks": [
                {
                    "final_rank": 1,
                    "title": title,
                    "artist": "Artist & Co",
                    "album": f"{title} - Single",
                    "duration_label": "3:21",
                    "duration_seconds": 201,
                    "total_points": points,
                    "best_rank": best_rank,
                    "occurrence_count": occurrences,
                    "apple_track_id": apple_track_id,
                    "canonical_key": f"apple:{apple_track_id}",
                    "source_track_url": f"https://music.apple.com/{market}/album/example?i={apple_track_id}",
                    "preview_url": f"https://audio.example/{apple_track_id}.m4a",
                    "artwork_url": f"https://img.example/{apple_track_id}.jpg",
                }
            ],
        },
        "switch_info": {
            "switch_window_bj": "2026-06-01 20:30:00 → 2026-06-01 21:30:00 北京时间",
            "switch_to_date": "2026-06-01",
        },
    }


def apple_artwork(size="1000"):
    return (
        "https://is1-ssl.mzstatic.com/image/thumb/Music211/v4/9b/34/03/"
        f"9b340351-9e3d-2189-8439-aecb94e5de83/4550756592923_cover.png/{size}x{size}bb.jpg"
    )


def report_payload_with_tracks(market, date, tracks):
    first_track = tracks[0] if tracks else {"title": "Empty", "apple_track_id": "empty"}
    payload = report_payload(market, first_track["title"], apple_track_id=first_track["apple_track_id"])
    payload["aggregation"]["release_date"] = date
    payload["aggregation"]["unique_tracks_before_filter"] = len(tracks)
    payload["aggregation"]["tracks_after_filter"] = len(tracks)
    payload["aggregation"]["tracks"] = []
    for idx, track in enumerate(tracks, 1):
        apple_track_id = track["apple_track_id"]
        payload["aggregation"]["tracks"].append(
            {
                "final_rank": idx,
                "title": track["title"],
                "artist": track.get("artist", "Artist & Co"),
                "album": f'{track["title"]} - Single',
                "duration_label": "3:21",
                "duration_seconds": 201,
                "total_points": 100 - idx,
                "best_rank": idx,
                "occurrence_count": 3,
                "apple_track_id": apple_track_id,
                "canonical_key": f"apple:{apple_track_id}",
                "source_track_url": f"https://music.apple.com/{market}/album/example?i={apple_track_id}",
                "preview_url": f"https://audio.example/{apple_track_id}.m4a",
                "artwork_url": f"https://img.example/{apple_track_id}.jpg",
            }
        )
    return payload


def report_payload_with_album_id(market, date, title, *, apple_track_id, album_id):
    payload = report_payload(market, title, apple_track_id=apple_track_id)
    payload["aggregation"]["release_date"] = date
    payload["aggregation"]["tracks"][0]["source_track_url"] = (
        f"https://music.apple.com/{market}/album/{album_id}?i={apple_track_id}"
    )
    return payload


class HtmlReportTests(unittest.TestCase):
    def test_render_requires_both_markets(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            jp.write_text(json.dumps(report_payload("jp", "JP Song", apple_track_id="1")), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "both JP and HK"):
                html_report.render_daily_report([jp])

    def test_render_daily_report_contains_music_fields_and_audio(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload("jp", "JP <Hit>", apple_track_id="1")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "HK Hit", apple_track_id="2")), encoding="utf-8")

            result = html_report.render_daily_report([jp, hk])

        self.assertEqual(result.release_date, "2026-05-31")
        self.assertIn("Chartoo iTunes Daily", result.html)
        self.assertIn("JP", result.html)
        self.assertIn("HK", result.html)
        self.assertIn("JP &lt;Hit&gt;", result.html)
        self.assertIn("Artist &amp; Co", result.html)
        self.assertIn("Apple Music", result.html)
        self.assertIn("<audio", result.html)
        self.assertIn("https://audio.example/1.m4a", result.html)
        self.assertIn("最高 #1", result.html)
        self.assertIn("出现 3 次", result.html)
        self.assertIn("100 pts", result.html)
        self.assertEqual(result.tunemymusic_text, "Artist & Co – JP <Hit>\nArtist & Co – HK Hit")
        self.assertNotIn("TuneMyMusic", result.html)
        self.assertNotIn("导入文本", result.html)
        self.assertNotIn("Artist &amp; Co – JP &lt;Hit&gt;", result.html)
        self.assertNotIn("Artist &amp; Co – HK Hit", result.html)

    def test_render_daily_report_adds_page_order_audio_queue(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(
                json.dumps(
                    report_payload_with_tracks(
                        "jp",
                        "2026-05-31",
                        [
                            {"title": "JP First", "apple_track_id": "jp-1"},
                            {"title": "JP Second", "apple_track_id": "jp-2"},
                        ],
                    )
                ),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(
                    report_payload_with_tracks(
                        "hk",
                        "2026-05-31",
                        [{"title": "HK First", "apple_track_id": "hk-1"}],
                    )
                ),
                encoding="utf-8",
            )

            result = html_report.render_daily_report([jp, hk])

        self.assertEqual(result.html.count('data-chartoo-audio-index="'), 3)
        self.assertLess(
            result.html.index('data-chartoo-audio-index="0"'),
            result.html.index('data-chartoo-audio-index="1"'),
        )
        self.assertLess(
            result.html.index('data-chartoo-audio-index="1"'),
            result.html.index('data-chartoo-audio-index="2"'),
        )
        self.assertIn("chartooAudioQueue", result.html)
        self.assertIn("addEventListener('ended'", result.html)
        self.assertIn("next.play()", result.html)
        self.assertIn("stopQueue()", result.html)
        self.assertEqual(result.visible_track_count, 3)
        self.assertEqual(result.audio_track_count, 3)
        self.assertNotIn("DOMContentLoaded", result.html)

    def test_audio_queue_registers_media_session_handlers(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(
                json.dumps(
                    report_payload_with_tracks(
                        "jp",
                        "2026-05-31",
                        [
                            {"title": "JP First", "apple_track_id": "jp-1"},
                            {"title": "JP Second", "apple_track_id": "jp-2"},
                        ],
                    )
                ),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(
                    report_payload_with_tracks(
                        "hk",
                        "2026-05-31",
                        [{"title": "HK First", "apple_track_id": "hk-1"}],
                    )
                ),
                encoding="utf-8",
            )

            result = html_report.render_daily_report([jp, hk])

        self.assertIn("navigator.mediaSession", result.html)
        self.assertIn('setActionHandler("play"', result.html)
        self.assertIn('setActionHandler("pause"', result.html)
        self.assertIn('setActionHandler("nexttrack"', result.html)
        self.assertIn('setActionHandler("previoustrack"', result.html)
        self.assertIn("new MediaMetadata", result.html)

    def test_audio_queue_skips_tracks_without_preview_url(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp_payload = report_payload_with_tracks(
                "jp",
                "2026-05-31",
                [
                    {"title": "JP With Preview", "apple_track_id": "jp-1"},
                    {"title": "JP No Preview", "apple_track_id": "jp-2"},
                ],
            )
            jp_payload["aggregation"]["tracks"][1]["preview_url"] = ""
            jp.write_text(json.dumps(jp_payload), encoding="utf-8")
            hk.write_text(
                json.dumps(
                    report_payload_with_tracks(
                        "hk",
                        "2026-05-31",
                        [{"title": "HK With Preview", "apple_track_id": "hk-1"}],
                    )
                ),
                encoding="utf-8",
            )

            result = html_report.render_daily_report([jp, hk])

        self.assertEqual(result.html.count("<audio"), 2)
        self.assertEqual(result.html.count('data-chartoo-audio-index="'), 2)
        self.assertEqual(result.visible_track_count, 3)
        self.assertEqual(result.audio_track_count, 2)
        self.assertIn('data-chartoo-audio-index="0"', result.html)
        self.assertIn('data-chartoo-audio-index="1"', result.html)

    def test_empty_reports_expose_zero_visible_and_audio_counts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload_with_tracks("jp", "2026-05-31", [])), encoding="utf-8")
            hk.write_text(json.dumps(report_payload_with_tracks("hk", "2026-05-31", [])), encoding="utf-8")

            result = html_report.render_daily_report([jp, hk])

        self.assertEqual(result.visible_track_count, 0)
        self.assertEqual(result.audio_track_count, 0)
        self.assertEqual(result.html.count("<audio"), 0)

    def test_livedoor_album_id_matches_render_under_track_and_unmatched_articles_render_at_bottom(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(
                json.dumps(
                    report_payload_with_album_id(
                        "jp",
                        "2026-05-31",
                        "Blue",
                        apple_track_id="track-blue",
                        album_id="6787976235",
                    )
                ),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(
                    report_payload_with_album_id(
                        "hk",
                        "2026-05-31",
                        "HK Song",
                        apple_track_id="track-hk",
                        album_id="2222222222",
                    )
                ),
                encoding="utf-8",
            )
            entries = [
                livedoor_reference.LivedoorEntry(
                    album_id="6787976235",
                    title="Artist「Blue」",
                    url="https://music.apple.com/jp/album/6787976235",
                    body_text="TVアニメ『Example』OP\nArtist「Blue」",
                ),
                livedoor_reference.LivedoorEntry(
                    album_id="9999999999",
                    title="Unmatched",
                    url="https://music.apple.com/jp/album/9999999999",
                    body_text="Unmatched original text",
                ),
            ]

            result = html_report.render_daily_report([jp, hk], livedoor_entries=entries)

        self.assertIn("Livedoor", result.html)
        self.assertIn("TVアニメ『Example』OP", result.html)
        self.assertIn("Artist「Blue」", result.html)
        self.assertIn("未匹配原文", result.html)
        self.assertIn("Unmatched original text", result.html)
        self.assertEqual(len(result.livedoor_matches["jp:track-blue"]), 1)
        self.assertEqual(result.livedoor_unmatched, [entries[1]])

    def test_history_suppression_keeps_current_match_on_card_and_removes_only_unmatched_entry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-07-11-jp.json"
            hk = root / "2026-07-11-hk.json"
            history_hk = root / "2026-07-10-hk.json"
            jp.write_text(
                json.dumps(
                    report_payload_with_album_id(
                        "jp", "2026-07-11", "愛のFIRE", apple_track_id="6783620461", album_id="6783620460"
                    )
                ),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(report_payload_with_album_id("hk", "2026-07-11", "HK Song", apple_track_id="2", album_id="200")),
                encoding="utf-8",
            )
            history_hk.write_text(
                json.dumps(
                    report_payload_with_album_id(
                        "hk", "2026-07-10", "Power of Love", apple_track_id="6786146087", album_id="6786146086"
                    )
                ),
                encoding="utf-8",
            )
            current_entry = livedoor_reference.LivedoorEntry(
                album_id="6783620460",
                title="新浜レオン「愛のFIRE」",
                url="https://music.apple.com/jp/album/6783620460",
                body_text="アニメOP\n新浜レオン「愛のFIRE」",
            )
            historical_entry = livedoor_reference.LivedoorEntry(
                album_id="6786146086",
                title="西野カナ「Power of Love」",
                url="https://music.apple.com/jp/album/6786146086",
                body_text="アニメOP&ED\n西野カナ「Power of Love」",
            )

            result = html_report.render_daily_report(
                [jp, hk],
                livedoor_entries=[current_entry, historical_entry],
                livedoor_history_report_paths=[history_hk],
            )

        self.assertEqual(result.livedoor_matches["jp:6783620461"], [current_entry])
        self.assertEqual(result.livedoor_unmatched, [])
        self.assertEqual(result.livedoor_suppressed, [historical_entry])
        self.assertIn("新浜レオン「愛のFIRE」", result.html)
        self.assertNotIn("西野カナ「Power of Love」", result.html)
        self.assertNotIn("未匹配原文", result.html)

    def test_history_suppression_skips_missing_and_malformed_reports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-07-11-jp.json"
            hk = root / "2026-07-11-hk.json"
            malformed = root / "2026-07-10-jp.json"
            jp.write_text(json.dumps(report_payload("jp", "JP Song", apple_track_id="1")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "HK Song", apple_track_id="2")), encoding="utf-8")
            malformed.write_text("{", encoding="utf-8")
            entry = livedoor_reference.LivedoorEntry(
                album_id="9999999999",
                title="Unmatched",
                url="https://music.apple.com/jp/album/9999999999",
                body_text="Unmatched body",
            )

            result = html_report.render_daily_report(
                [jp, hk],
                livedoor_entries=[entry],
                livedoor_history_report_paths=[root / "missing.json", malformed],
            )

        self.assertEqual(result.livedoor_unmatched, [entry])
        self.assertEqual(result.livedoor_suppressed, [])
        self.assertIn("Unmatched body", result.html)

    def test_cross_market_duplicates_are_folded_to_stronger_market(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(
                json.dumps(report_payload("jp", "Shared Song", apple_track_id="same", points=100, best_rank=2)),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(report_payload("hk", "Shared Song", apple_track_id="same", points=300, best_rank=5)),
                encoding="utf-8",
            )

            result = html_report.render_daily_report([jp, hk])

        self.assertEqual(len(result.tracks_by_market["hk"]), 1)
        self.assertEqual(len(result.tracks_by_market["jp"]), 0)
        self.assertEqual(len(result.folded_duplicates), 1)
        folded = result.folded_duplicates[0]
        self.assertEqual(folded["keep_market"], "hk")
        self.assertEqual(folded["folded_market"], "jp")
        self.assertNotIn("跨市场重复已折叠", result.html)
        self.assertNotIn("保留 HK", result.html)
        self.assertNotIn("折叠 JP", result.html)
        self.assertIn("Artist &amp; Co", result.html)
        self.assertEqual(result.html.count("Shared Song"), 4)

    def test_cross_market_duplicates_fold_by_title_artist_when_apple_ids_differ(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(
                json.dumps(report_payload("jp", "Shared Song", apple_track_id="jp-local", points=120)),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(report_payload("hk", "Shared Song", apple_track_id="hk-local", points=90)),
                encoding="utf-8",
            )

            result = html_report.render_daily_report([jp, hk])

        self.assertEqual(len(result.tracks_by_market["jp"]), 1)
        self.assertEqual(len(result.tracks_by_market["hk"]), 0)
        self.assertEqual(result.folded_duplicates[0]["keep_market"], "jp")
        self.assertEqual(result.folded_duplicates[0]["folded_market"], "hk")

    def test_write_daily_report_uses_stable_path_and_content_hash(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            reports = root / "reports"
            out_dir = root / "html"
            reports.mkdir()
            jp = reports / "2026-05-31-jp.json"
            hk = reports / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload("jp", "JP Song", apple_track_id="1")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "HK Song", apple_track_id="2")), encoding="utf-8")

            result = html_report.write_daily_report([jp, hk], out_dir)

            self.assertEqual(result.path, out_dir / "2026-05-31-chartoo.html")
            self.assertTrue(result.path.exists())
            self.assertRegex(result.content_hash, r"^[0-9a-f]{16}$")
            self.assertEqual(result.path.read_text(encoding="utf-8"), result.html)

    def test_apple_artwork_uses_remote_srcset_without_inline_images(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp_payload = report_payload("jp", "JP Song", apple_track_id="1")
            hk_payload = report_payload("hk", "HK Song", apple_track_id="2")
            jp_payload["aggregation"]["tracks"][0]["artwork_url"] = apple_artwork("1000")
            hk_payload["aggregation"]["tracks"][0]["artwork_url"] = apple_artwork("1000")
            jp.write_text(json.dumps(jp_payload), encoding="utf-8")
            hk.write_text(json.dumps(hk_payload), encoding="utf-8")

            result = html_report.render_daily_report([jp, hk])

        self.assertNotIn("data:image", result.html)
        self.assertIn(f'src="{apple_artwork("300")}"', result.html)
        self.assertIn(
            f'srcset="{apple_artwork("300")} 300w, {apple_artwork("600")} 600w, {apple_artwork("1000")} 1000w"',
            result.html,
        )
        self.assertIn('sizes="(max-width: 760px) 58px, 72px"', result.html)
        self.assertIn('referrerpolicy="no-referrer"', result.html)
        self.assertIn('loading="lazy"', result.html)
        self.assertIn('decoding="async"', result.html)
        self.assertIn("chartooCoverFallback", result.html)

    def test_non_apple_artwork_keeps_original_remote_url_without_srcset(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-05-31-jp.json"
            hk = root / "2026-05-31-hk.json"
            jp.write_text(json.dumps(report_payload("jp", "JP Song", apple_track_id="1")), encoding="utf-8")
            hk.write_text(json.dumps(report_payload("hk", "HK Song", apple_track_id="2")), encoding="utf-8")

            result = html_report.render_daily_report([jp, hk])

        self.assertIn('src="https://img.example/1.jpg"', result.html)
        self.assertNotIn('https://img.example/1.jpg 300w', result.html)
        self.assertNotIn("data:image", result.html)


if __name__ == "__main__":
    unittest.main()
