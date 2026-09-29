import json
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from pylon.tasks.itunes import livedoor_reference


class LivedoorReferenceTests(unittest.TestCase):
    def test_date_url_uses_release_date_archive_not_homepage(self):
        self.assertEqual(
            livedoor_reference.date_url_for("2026-07-08"),
            "http://blog.livedoor.jp/m_79sc/archives/2026-07-08.html",
        )
        self.assertEqual(
            livedoor_reference.jina_date_url_for("2026-07-08"),
            "https://r.jina.ai/http://blog.livedoor.jp/m_79sc/archives/2026-07-08.html",
        )
        self.assertNotEqual(
            livedoor_reference.jina_date_url_for("2026-07-08"),
            "https://r.jina.ai/http://blog.livedoor.jp/m_79sc/",
        )

    def test_extract_album_id_from_music_and_itunes_urls(self):
        self.assertEqual(
            livedoor_reference.extract_album_id_from_url("https://music.apple.com/jp/album/6787976235?i=6787976426"),
            "6787976235",
        )
        self.assertEqual(
            livedoor_reference.extract_album_id_from_url(
                "https://music.apple.com/jp/album/example-title/6787976235?i=6787976426"
            ),
            "6787976235",
        )
        self.assertEqual(
            livedoor_reference.extract_album_id_from_url(
                "https://itunes.apple.com/jp/album/example-title/id6787976235?i=6787976426"
            ),
            "6787976235",
        )
        self.assertEqual(livedoor_reference.extract_album_id_from_url("https://music.apple.com/jp/song/6787976426"), "")

    def test_extract_track_id_from_slugged_song_url(self):
        self.assertEqual(
            livedoor_reference.extract_track_id_from_url("https://music.apple.com/jp/song/tilt/6780835897"),
            "6780835897",
        )

    def test_song_id_segment_is_not_mistaken_for_an_album_id(self):
        url = "https://music.apple.com/jp/song/tilt/id6780835897"

        self.assertEqual(livedoor_reference.extract_album_id_from_url(url), "")
        self.assertEqual(livedoor_reference.extract_track_id_from_url(url), "6780835897")

    def test_parse_html_entries_uses_double_breaks_as_groups_and_shares_prefix(self):
        source = """
        <header>livedoor Blog ブログを書く 記事検索</header>
        <div class="article-body entry-content"><div class="article-body-inner">
          アニメ『片田舎のおっさん、剣聖になるⅡ』OP&amp;ED<br>
          ユニコーン「クレナイノハ - アニメば～じょん」<br>
          <a href="https://music.apple.com/jp/album/6784646951">https://music.apple.com/jp/album/6784646951</a><br>
          BLUE ENCOUNT「未完成」<br>
          <a href="https://music.apple.com/jp/album/6784147985">https://music.apple.com/jp/album/6784147985</a><br><br>
          アニメ『正反対な君と僕(第2期)』ED<br>
          Mega Shinnosuke「運命の君」<br>
          <a href="https://music.apple.com/jp/album/6783704597">https://music.apple.com/jp/album/6783704597</a>
        </div></div>
        <aside>コメント 月別アーカイブ</aside>
        """

        entries = livedoor_reference.parse_html_entries(source)

        self.assertEqual([entry.album_id for entry in entries], ["6784646951", "6784147985", "6783704597"])
        self.assertEqual(
            entries[0].body_text,
            "アニメ『片田舎のおっさん、剣聖になるⅡ』OP&ED\nユニコーン「クレナイノハ - アニメば～じょん」",
        )
        self.assertEqual(
            entries[1].body_text,
            "アニメ『片田舎のおっさん、剣聖になるⅡ』OP&ED\nBLUE ENCOUNT「未完成」",
        )
        self.assertEqual(
            entries[2].body_text,
            "アニメ『正反対な君と僕(第2期)』ED\nMega Shinnosuke「運命の君」",
        )
        self.assertNotIn("livedoor Blog", "\n".join(entry.body_text for entry in entries))
        self.assertNotIn("月別アーカイブ", "\n".join(entry.body_text for entry in entries))

    def test_parse_html_entries_keeps_song_url_out_of_description(self):
        source = """
        <div class="article-body entry-content">
          アニメ『転校先の清楚可憐な美少女が、昔男子と思って一緒に遊んだ幼馴染だった件』OP&amp;ED<br>
          DIALOGUE＋「夏に重ねて」<br>
          <a href="https://music.apple.com/jp/album/6781111844">album</a><br>
          harmoe「Tilt」<br>
          <a href="https://music.apple.com/jp/song/6780835897">song</a>
        </div>
        """

        entries = livedoor_reference.parse_html_entries(source)

        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1].track_id, "6780835897")
        self.assertEqual(entries[1].album_id, "")
        self.assertEqual(
            entries[1].body_text,
            "アニメ『転校先の清楚可憐な美少女が、昔男子と思って一緒に遊んだ幼馴染だった件』OP&ED\nharmoe「Tilt」",
        )
        self.assertNotIn("https://music.apple.com/jp/song/6780835897", entries[1].body_text)

    def test_lookup_songs_uses_one_official_request_and_normalizes_song_url(self):
        calls = []
        payload = {
            "resultCount": 2,
            "results": [
                {
                    "kind": "song",
                    "trackId": 6780835897,
                    "collectionId": 6780835589,
                    "trackViewUrl": "https://music.apple.com/jp/album/tilt/6780835589?i=6780835897&uo=4",
                },
                {
                    "kind": "song",
                    "trackId": 6775411897,
                    "collectionId": 6775411895,
                    "trackViewUrl": "https://music.apple.com/jp/album/x/6775411895?i=6775411897&uo=4",
                },
            ],
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        def fake_urlopen(request, timeout):
            calls.append((request.full_url, timeout))
            return FakeResponse()

        with mock.patch.object(livedoor_reference.urllib.request, "urlopen", side_effect=fake_urlopen):
            songs = livedoor_reference.lookup_songs(["6780835897", "6775411897"], country="jp", timeout=12)

        self.assertEqual(len(calls), 1)
        query = parse_qs(urlsplit(calls[0][0]).query)
        self.assertEqual(set(query["id"][0].split(",")), {"6780835897", "6775411897"})
        self.assertEqual(query["country"], ["jp"])
        self.assertEqual(query["entity"], ["song"])
        self.assertEqual(songs["6780835897"].album_id, "6780835589")
        self.assertEqual(songs["6780835897"].url, "https://music.apple.com/jp/album/tilt/6780835589?i=6780835897")

    def test_song_lookup_batches_entries_by_storefront(self):
        entries = [
            livedoor_reference.LivedoorEntry(
                album_id="",
                track_id="1111111111",
                title="JP Song",
                url="https://music.apple.com/jp/song/1111111111",
                body_text="JP Song",
            ),
            livedoor_reference.LivedoorEntry(
                album_id="",
                track_id="1111111111",
                title="HK Song",
                url="https://music.apple.com/hk/song/1111111111",
                body_text="HK Song",
            ),
        ]
        calls = []

        def fake_lookup(track_ids, country, timeout):
            ids = list(track_ids)
            calls.append((ids, country, timeout))
            return {
                track_id: livedoor_reference.AppleSong(
                    track_id=track_id,
                    album_id=f"9{track_id}",
                    url=f"https://music.apple.com/{country}/album/9{track_id}?i={track_id}",
                )
                for track_id in ids
            }

        with mock.patch.object(livedoor_reference, "lookup_songs", side_effect=fake_lookup):
            resolved = livedoor_reference._resolve_song_entries(entries, timeout=9)

        self.assertEqual(calls, [(["1111111111"], "hk", 9), (["1111111111"], "jp", 9)])
        self.assertEqual(resolved[0].album_id, "91111111111")
        self.assertEqual(resolved[1].album_id, "91111111111")
        self.assertIn("/jp/", resolved[0].url)
        self.assertIn("/hk/", resolved[1].url)

    def test_song_entry_matches_track_id_when_lookup_is_unavailable(self):
        tracks = {
            "jp": [
                {
                    "apple_track_id": "6780835897",
                    "source_track_url": "https://music.apple.com/jp/album/twice-upon-a-time/6780835589?i=6780835897",
                },
                {
                    "apple_track_id": "6780835900",
                    "source_track_url": "https://music.apple.com/jp/album/twice-upon-a-time/6780835589?i=6780835900",
                },
            ],
            "hk": [],
        }
        entry = livedoor_reference.LivedoorEntry(
            album_id="",
            track_id="6780835897",
            title="harmoe「Tilt」",
            url="https://music.apple.com/jp/song/6780835897",
            body_text="アニメ『Example』OP&ED\nharmoe「Tilt」",
        )

        matched, unmatched = livedoor_reference.match_entries_by_track(tracks, [entry])

        self.assertEqual(matched["jp:6780835897"], [entry])
        self.assertEqual(matched["jp:6780835900"], [])
        self.assertEqual(unmatched, [])

    def test_track_specific_entry_does_not_fall_back_to_a_different_song_on_the_same_album(self):
        tracks = {
            "jp": [
                {
                    "apple_track_id": "6780835900",
                    "source_track_url": "https://music.apple.com/jp/album/twice-upon-a-time/6780835589?i=6780835900",
                }
            ],
            "hk": [],
        }
        entry = livedoor_reference.LivedoorEntry(
            album_id="6780835589",
            track_id="6780835897",
            title="harmoe「Tilt」",
            url="https://music.apple.com/jp/album/tilt/6780835589?i=6780835897",
            body_text="アニメ『Example』OP&ED\nharmoe「Tilt」",
        )

        matched, unmatched = livedoor_reference.match_entries_by_track(tracks, [entry])

        self.assertEqual(matched["jp:6780835900"], [])
        self.assertEqual(unmatched, [entry])

    def test_track_specific_entry_matches_track_id_from_chartoo_source_url(self):
        tracks = {
            "jp": [
                {
                    "canonical_key": "tilt",
                    "source_track_url": "https://music.apple.com/jp/album/tilt/6780835589?i=6780835897",
                }
            ],
            "hk": [],
        }
        entry = livedoor_reference.LivedoorEntry(
            album_id="6780835589",
            track_id="6780835897",
            title="harmoe「Tilt」",
            url="https://music.apple.com/jp/album/tilt/6780835589?i=6780835897",
            body_text="アニメ『Example』OP&ED\nharmoe「Tilt」",
        )

        matched, unmatched = livedoor_reference.match_entries_by_track(tracks, [entry])

        self.assertEqual(matched["jp:tilt"], [entry])
        self.assertEqual(unmatched, [])

    def test_album_only_entry_falls_back_to_album_id(self):
        tracks = {
            "jp": [
                {
                    "apple_track_id": "track-1",
                    "source_track_url": "https://music.apple.com/jp/album/6787976235?i=6787976426",
                }
            ],
            "hk": [
                {
                    "apple_track_id": "track-2",
                    "source_track_url": "https://music.apple.com/hk/album/2222222222?i=2222222223",
                }
            ],
        }
        entries = [
            livedoor_reference.LivedoorEntry(
                album_id="6787976235",
                title="Blue",
                url="https://music.apple.com/jp/album/6787976235",
                body_text="Artist「Blue」",
            ),
            livedoor_reference.LivedoorEntry(
                album_id="9999999999",
                title="Unmatched",
                url="https://music.apple.com/jp/album/9999999999",
                body_text="Unmatched body",
            ),
        ]

        matched, unmatched = livedoor_reference.match_entries_by_track(tracks, entries)

        self.assertEqual(matched["jp:track-1"], [entries[0]])
        self.assertEqual(matched["hk:track-2"], [])
        self.assertEqual(unmatched, [entries[1]])

    def test_suppress_unmatched_entries_uses_exact_track_id(self):
        entry = livedoor_reference.LivedoorEntry(
            album_id="6780835589",
            track_id="6780835897",
            title="harmoe「Tilt」",
            url="https://music.apple.com/jp/album/6780835589?i=6780835897",
            body_text="harmoe「Tilt」",
        )
        historical_tracks = [
            {
                "apple_track_id": "6780835897",
                "source_track_url": "https://music.apple.com/hk/album/6780835589?i=6780835897",
            }
        ]

        kept, suppressed = livedoor_reference.suppress_unmatched_entries([entry], historical_tracks)

        self.assertEqual(kept, [])
        self.assertEqual(suppressed, [entry])

    def test_suppress_unmatched_entries_uses_album_id_for_album_only_entry(self):
        entry = livedoor_reference.LivedoorEntry(
            album_id="6787976235",
            title="Blue",
            url="https://music.apple.com/jp/album/6787976235",
            body_text="Artist「Blue」",
        )
        historical_tracks = [
            {
                "apple_track_id": "6787976426",
                "source_track_url": "https://music.apple.com/jp/album/6787976235?i=6787976426",
            }
        ]

        kept, suppressed = livedoor_reference.suppress_unmatched_entries([entry], historical_tracks)

        self.assertEqual(kept, [])
        self.assertEqual(suppressed, [entry])

    def test_suppress_unmatched_track_entry_does_not_fall_back_to_album_id(self):
        entry = livedoor_reference.LivedoorEntry(
            album_id="6780835589",
            track_id="6780835897",
            title="harmoe「Tilt」",
            url="https://music.apple.com/jp/album/6780835589?i=6780835897",
            body_text="harmoe「Tilt」",
        )
        historical_tracks = [
            {
                "apple_track_id": "6780835900",
                "source_track_url": "https://music.apple.com/jp/album/6780835589?i=6780835900",
            }
        ]

        kept, suppressed = livedoor_reference.suppress_unmatched_entries([entry], historical_tracks)

        self.assertEqual(kept, [entry])
        self.assertEqual(suppressed, [])


if __name__ == "__main__":
    unittest.main()
