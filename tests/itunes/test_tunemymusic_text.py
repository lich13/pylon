import json
import tempfile
import unittest
from pathlib import Path

from pylon.tasks.itunes import tunemymusic_text


def report_payload(market, title, *, apple_track_id, points=100, best_rank=1, artist="Artist & Co"):
    return {
        "aggregation": {
            "market": market,
            "release_date": "2026-06-01",
            "unique_tracks_before_filter": 1,
            "tracks_after_filter": 1,
            "dropped_count": 0,
            "tracks": [
                {
                    "final_rank": 1,
                    "title": title,
                    "artist": artist,
                    "album": f"{title} - Single",
                    "duration_label": "3:21",
                    "duration_seconds": 201,
                    "total_points": points,
                    "best_rank": best_rank,
                    "occurrence_count": 3,
                    "apple_track_id": apple_track_id,
                    "canonical_key": f"apple:{apple_track_id}",
                    "source_track_url": f"https://music.apple.com/{market}/album/example?i={apple_track_id}",
                    "preview_url": f"https://audio.example/{apple_track_id}.m4a",
                    "artwork_url": f"https://img.example/{apple_track_id}.jpg",
                }
            ],
        }
    }


class TuneMyMusicTextTests(unittest.TestCase):
    def test_render_text_contains_direct_input_lines_and_unicode(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-06-01-jp.json"
            hk = root / "2026-06-01-hk.json"
            jp.write_text(
                json.dumps(report_payload("jp", "完全無欠", apple_track_id="6771626456", artist="B'z")),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(report_payload("hk", "是日戀愛", apple_track_id="6774103269", artist="Frankie 陳瑞輝")),
                encoding="utf-8",
            )

            result = tunemymusic_text.render_text([jp, hk])

        self.assertEqual(result.release_date, "2026-06-01")
        self.assertEqual(result.line_count, 2)
        self.assertRegex(result.content_hash, r"^[0-9a-f]{16}$")
        self.assertEqual(result.text, "B'z – 完全無欠\nFrankie 陳瑞輝 – 是日戀愛")
        self.assertNotIn("Track name", result.text)
        self.assertNotIn("<html", result.text.lower())
        self.assertNotIn(",", result.text)

    def test_cross_market_duplicates_are_folded_like_html(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            jp = root / "2026-06-01-jp.json"
            hk = root / "2026-06-01-hk.json"
            jp.write_text(
                json.dumps(report_payload("jp", "Shared Song", apple_track_id="same", points=100)),
                encoding="utf-8",
            )
            hk.write_text(
                json.dumps(report_payload("hk", "Shared Song", apple_track_id="same", points=300)),
                encoding="utf-8",
            )

            result = tunemymusic_text.render_text([jp, hk])

        self.assertEqual(result.line_count, 1)
        self.assertEqual(result.text, "Artist & Co – Shared Song")

    def test_caption_keeps_whole_lines_and_points_to_html_when_truncated(self):
        text = "\n".join(f"Artist {idx} – Song {idx}" for idx in range(1, 20))

        caption = tunemymusic_text.caption_for_text(text, limit=90)

        self.assertLessEqual(len(caption), 90)
        self.assertTrue(caption.startswith("TuneMyMusic:\nArtist 1 – Song 1\n"))
        self.assertIn("完整列表见 HTML 内 TuneMyMusic 区块", caption)
        self.assertNotIn("Artist 19", caption)
        self.assertNotIn("Artist 3", caption.rsplit("\n", 1)[0])


if __name__ == "__main__":
    unittest.main()
