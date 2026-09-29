import json, sqlite3, tempfile, unittest
from pathlib import Path

from tests.helpers import load_script_module

monitor = load_script_module('monitor', 'itunes_daily_monitor.py')
class MonitorUnitTests(unittest.TestCase):
    def row(self, market, date, rank, apple, title, seen):
        return {'market':market,'release_date':date,'rank':rank,'apple_track_id':apple,'title':title,'artist':'A','album':'X','duration_seconds':180,'duration_label':'3:00','source_url':'u','data_url':'d','observed_at_utc':seen,'data_updated_at_utc':'u','source_quality':'chartoo_full'}
    def test_duration_to_seconds(self):
        self.assertEqual(monitor.duration_to_seconds('3:50'),230); self.assertEqual(monitor.duration_to_seconds('1:02:03'),3723); self.assertEqual(monitor.duration_to_seconds('30000'),30); self.assertIsNone(monitor.duration_to_seconds('bad'))
    def test_chartoo_audio_track_and_artwork_urls(self):
        self.assertEqual(
            monitor.preview_url_from_qm('itunes-assets/AudioPreview221/v4/x/y.m4a'),
            'https://audio-ssl.itunes.apple.com/itunes-assets/AudioPreview221/v4/x/y.m4a'
        )
        self.assertEqual(
            monitor.apple_track_url('jp','1889132072','1889132070'),
            'https://music.apple.com/jp/album/1889132070?i=1889132072'
        )
        self.assertEqual(
            monitor.artwork_url_from_gp('Music221/v4/56/9a/fa/cover.png'),
            'https://is1-ssl.mzstatic.com/image/thumb/Music221/v4/56/9a/fa/cover.png/1000x1000bb.jpg'
        )
    def test_chartoo_page_routes_are_current_canonical_urls(self):
        self.assertEqual(monitor.CHARTOO_PAGE['jp'], 'https://jp.chartoo.com/itunes/topsongs')
        self.assertEqual(monitor.CHARTOO_PAGE['hk'], 'https://hk.chartoo.com/itunes/topsongs')
        self.assertNotIn('/zh-Hans/', monitor.CHARTOO_PAGE['jp'])
        self.assertNotIn('/zh-Hans/', monitor.CHARTOO_PAGE['hk'])
    def test_resolve_chartoo_topsongs_url_uses_base_json_version_map(self):
        calls = []
        original_fetch_chartoo_json = monitor.fetch_chartoo_json
        original_fetch_chartoo_bytes = monitor.fetch_chartoo_bytes
        try:
            def fake_fetch_chartoo_json(url, timeout=30):
                calls.append(url)
                if url.endswith('/json/init/jp/itunes.json') or url.endswith('/json/country/jp/itunes.json'):
                    return {'mN': {}}
                if url.endswith('/json/init/jp/base-1234567890-abcd.json'):
                    return {'v': {'yB': '1779197466-bsv7'}}
                if url.endswith('/json/country/jp/base-1234567890-abcd.json'):
                    return {'lo': [{'df': 'apple-music', 'rb': 'TX'}, {'df': 'itunes', 'rb': 'yB'}]}
                if url.endswith('/json/init/jp/itunes-1779197466-bsv7.json'):
                    return {'mN': {'jp/topsongs': '1779249843-30ao'}}
                raise AssertionError(f'unexpected chartoo json url {url}')

            def fake_fetch_chartoo_bytes(url, timeout=30):
                self.assertEqual(url, 'https://jp.chartoo.com/itunes/topsongs')
                return b'<html><head><script defer src="/script/base-1234567890-abcd.js"></script></head></html>'

            monitor.fetch_chartoo_json = fake_fetch_chartoo_json
            monitor.fetch_chartoo_bytes = fake_fetch_chartoo_bytes
            url, info = monitor.resolve_chartoo_topsongs_url('jp')
        finally:
            monitor.fetch_chartoo_json = original_fetch_chartoo_json
            monitor.fetch_chartoo_bytes = original_fetch_chartoo_bytes

        self.assertEqual(url, 'https://itunes.chartoo.com/jp/topsongs-1779249843-30ao.json')
        self.assertEqual(info['pointer'], '1779249843-30ao')
        self.assertEqual(info['init_url'], 'https://jp.chartoo.com/json/init/jp/itunes-1779197466-bsv7.json')
        self.assertIn('https://jp.chartoo.com/json/country/jp/base-1234567890-abcd.json', calls)
        self.assertIn('https://jp.chartoo.com/json/init/jp/base-1234567890-abcd.json', calls)
    def test_rss_fallback_enriches_missing_duration_and_preview_from_lookup(self):
        calls = []
        original_fetch_json = monitor.fetch_json
        try:
            def fake_fetch_json(url, timeout=30):
                calls.append(url)
                if '/rss/topsongs/' in url:
                    return {'feed': {'updated': {'label': '2026-05-20T04:00:00Z'}, 'entry': [{
                        'im:name': {'label': '金剛不壞'},
                        'im:artist': {'label': 'Jer 柳應廷'},
                        'im:collection': {'im:name': {'label': '金剛不壞 - Single'}},
                        'im:releaseDate': {'attributes': {'label': '2026年5月19日'}},
                        'id': {'attributes': {'im:id': '6770577272'}},
                        'link': {'attributes': {'href': 'https://music.apple.com/hk/album/x?i=6770577272'}},
                    }]}}
                if '/lookup?' in url:
                    return {'resultCount': 1, 'results': [{'trackTimeMillis': 252000, 'previewUrl': 'https://audio.example/preview.m4a'}]}
                raise AssertionError(f'unexpected generic fetch for {url}')

            monitor.fetch_json = fake_fetch_json
            rows, meta = monitor.fetch_rss_rows('hk', '2026-05-20T04:10:00Z')
        finally:
            monitor.fetch_json = original_fetch_json

        self.assertEqual(meta['source'], 'itunes_rss')
        self.assertEqual(rows[0]['duration_seconds'], 252)
        self.assertEqual(rows[0]['duration_label'], '4:12')
        self.assertEqual(rows[0]['preview_url'], 'https://audio.example/preview.m4a')
        self.assertTrue(any('lookup?id=6770577272&country=hk' in url for url in calls))
    def test_rss_lookup_failure_does_not_block_rows(self):
        original_fetch_json = monitor.fetch_json
        try:
            def fake_fetch_json(url, timeout=30):
                if '/rss/topsongs/' in url:
                    return {'feed': {'entry': [{
                        'im:name': {'label': 'KMK'},
                        'im:artist': {'label': 'NEWS'},
                        'im:releaseDate': {'attributes': {'label': '2026年5月18日'}},
                        'id': {'attributes': {'im:id': '6763231265'}},
                    }]}}
                if '/lookup?' in url:
                    raise TimeoutError('lookup timeout')
                raise AssertionError(f'unexpected generic fetch for {url}')

            monitor.fetch_json = fake_fetch_json
            rows, meta = monitor.fetch_rss_rows('jp', '2026-05-20T04:10:00Z')
        finally:
            monitor.fetch_json = original_fetch_json

        self.assertEqual(meta['source'], 'itunes_rss')
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]['duration_seconds'])
        self.assertIsNone(rows[0]['preview_url'])
    def test_chartoo_probe_timeouts_are_bounded_before_rss_fallback(self):
        calls = []
        original_fetch_chartoo_json = monitor.fetch_chartoo_json
        original_fetch_chartoo_bytes = monitor.fetch_chartoo_bytes
        original_fetch_json = monitor.fetch_json
        try:
            def fake_fetch_chartoo_json(url, timeout=30):
                calls.append((url, timeout))
                raise TimeoutError('chartoo timeout')

            def fake_fetch_chartoo_bytes(url, timeout=30):
                calls.append((url, timeout))
                raise TimeoutError('chartoo page timeout')

            def fake_fetch_json(url, timeout=30):
                if 'itunes.apple.com' in url:
                    return {'feed': {'entry': []}}
                raise AssertionError(f'unexpected generic fetch for {url}')

            monitor.fetch_chartoo_json = fake_fetch_chartoo_json
            monitor.fetch_chartoo_bytes = fake_fetch_chartoo_bytes
            monitor.fetch_json = fake_fetch_json
            rows, meta = monitor.fetch_market_rows('jp', '2026-05-20T00:33:00Z')
        finally:
            monitor.fetch_chartoo_json = original_fetch_chartoo_json
            monitor.fetch_chartoo_bytes = original_fetch_chartoo_bytes
            monitor.fetch_json = original_fetch_json

        self.assertEqual(rows, [])
        self.assertEqual(meta['source'], 'itunes_rss')
        self.assertTrue(calls)
        self.assertLessEqual(max(timeout for _, timeout in calls), 8)
    def test_filter_rule(self):
        with tempfile.TemporaryDirectory() as td:
            conn=sqlite3.connect(Path(td)/'s.sqlite'); monitor.ensure_db(conn)
            rows=[self.row('jp','2026-04-28',51,'a51','Drop Me','2026-04-29T00:30:00Z'), self.row('jp','2026-04-28',50,'a50','Keep 50','2026-04-29T00:30:00Z'), self.row('jp','2026-04-28',80,'a80','Keep Twice','2026-04-29T00:30:00Z'), self.row('jp','2026-04-28',85,'a80','Keep Twice','2026-04-29T01:30:00Z')]
            monitor.record_observations(conn,'r1',rows); agg=monitor.aggregate_market_date(conn,'jp','2026-04-28'); titles=[t['title'] for t in agg['tracks']]
            self.assertNotIn('Drop Me',titles); self.assertIn('Keep 50',titles); self.assertIn('Keep Twice',titles); self.assertEqual(agg['dropped_count'],1); self.assertEqual(next(t for t in agg['tracks'] if t['title']=='Keep Twice')['occurrence_count'],2)
    def test_sorting_total_points_before_best_rank(self):
        with tempfile.TemporaryDirectory() as td:
            conn=sqlite3.connect(Path(td)/'s.sqlite'); monitor.ensure_db(conn)
            rows=[self.row('hk','2026-04-28',1,'one','One Shot Rank 1','2026-04-29T00:30:00Z'), self.row('hk','2026-04-28',20,'many','Many Rank 20','2026-04-29T00:30:00Z'), self.row('hk','2026-04-28',20,'many','Many Rank 20','2026-04-29T01:30:00Z')]
            monitor.record_observations(conn,'r1',rows); agg=monitor.aggregate_market_date(conn,'hk','2026-04-28')
            self.assertEqual(agg['tracks'][0]['title'],'Many Rank 20'); self.assertGreater(agg['tracks'][0]['total_points'],agg['tracks'][1]['total_points'])
    def test_fixed_switch_time_bj_uses_next_day_2130(self):
        self.assertEqual(monitor.fixed_switch_time_bj('2026-04-29'), '2026-04-30 21:30 北京时间')
    def test_should_switch_date_uses_fixed_2130_bj_cutoff(self):
        self.assertFalse(monitor.should_switch_date('2026-04-30', '2026-05-01T07:30:00Z'))  # 15:30 BJ
        self.assertFalse(monitor.should_switch_date('2026-04-30', '2026-05-01T13:29:59Z'))
        self.assertTrue(monitor.should_switch_date('2026-04-30', '2026-05-01T13:30:00Z'))
    def test_choose_initial_active_date_keeps_previous_before_2130(self):
        rows=[self.row('hk','2026-04-30',5,'a','Old Active','2026-05-01T06:30:00Z'), self.row('hk','2026-05-01',64,'b','Visible New','2026-05-01T06:30:00Z')]
        self.assertEqual(monitor.choose_initial_active_date(rows,'2026-05-01','2026-05-01T07:30:00Z'), '2026-04-30')
        self.assertEqual(monitor.choose_initial_active_date(rows,'2026-05-01','2026-05-01T13:30:00Z'), '2026-05-01')
    def test_choose_initial_active_date_forces_next_day_at_cutoff_even_if_latest_lags(self):
        rows=[self.row('hk','2026-04-30',5,'a','Old Active','2026-05-01T13:30:00Z')]
        self.assertEqual(monitor.choose_initial_active_date(rows,'2026-04-30','2026-05-01T13:30:00Z'), '2026-05-01')
    def test_run_once_defers_visible_newer_date_until_2130(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'root'; db=Path(td)/'s.sqlite'
            conn=sqlite3.connect(db); monitor.ensure_db(conn); monitor.set_meta(conn,'hk.active_date','2026-04-30'); conn.close()
            orig_markets=monitor.MARKETS; orig_fetch=monitor.fetch_market_rows; orig_iso_now=monitor.iso_now
            try:
                monitor.MARKETS=('hk',)
                def fake_fetch(market, observed):
                    return [self.row('hk','2026-04-30',5,'old','Old Active',observed), self.row('hk','2026-05-01',64,'new','Visible New',observed)], {'market':'hk','status':'ok','source':'test','row_count':2}
                monitor.fetch_market_rows=fake_fetch
                monitor.iso_now=lambda: '2026-05-01T07:30:00Z'
                args=type('Args', (), {'root': str(root), 'db': str(db)})()
                self.assertEqual(monitor.run_once(args), '{"wakeAgent": false}')
            finally:
                monitor.MARKETS=orig_markets; monitor.fetch_market_rows=orig_fetch; monitor.iso_now=orig_iso_now
            with sqlite3.connect(db) as conn:
                self.assertEqual(monitor.get_meta(conn,'hk.active_date'), '2026-04-30')
                self.assertEqual(conn.execute('select count(*) from finalizations').fetchone()[0], 0)
    def test_run_once_switches_visible_newer_date_at_2130(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'root'; db=Path(td)/'s.sqlite'
            conn=sqlite3.connect(db); monitor.ensure_db(conn); monitor.set_meta(conn,'hk.active_date','2026-04-30'); monitor.record_observations(conn,'seed',[self.row('hk','2026-04-30',5,'old','Old Active','2026-05-01T12:30:00Z')]); conn.close()
            orig_markets=monitor.MARKETS; orig_fetch=monitor.fetch_market_rows; orig_iso_now=monitor.iso_now
            try:
                monitor.MARKETS=('hk',)
                def fake_fetch(market, observed):
                    return [self.row('hk','2026-04-30',5,'old','Old Active',observed), self.row('hk','2026-05-01',64,'new','Visible New',observed)], {'market':'hk','status':'ok','source':'test','row_count':2}
                monitor.fetch_market_rows=fake_fetch
                monitor.iso_now=lambda: '2026-05-01T13:30:00Z'
                args=type('Args', (), {'root': str(root), 'db': str(db)})()
                out=monitor.run_once(args)
            finally:
                monitor.MARKETS=orig_markets; monitor.fetch_market_rows=orig_fetch; monitor.iso_now=orig_iso_now
            self.assertIn('"action":"finalize"', out)
            with sqlite3.connect(db) as conn:
                self.assertEqual(monitor.get_meta(conn,'hk.active_date'), '2026-05-01')
                self.assertEqual(conn.execute('select count(*) from finalizations where market="hk" and release_date="2026-04-30"').fetchone()[0], 1)
    def test_run_once_switches_at_2130_even_when_chartoo_has_not_visible_next_date(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'root'; db=Path(td)/'s.sqlite'
            conn=sqlite3.connect(db); monitor.ensure_db(conn); monitor.set_meta(conn,'hk.active_date','2026-05-01'); monitor.record_observations(conn,'seed',[self.row('hk','2026-05-01',5,'old','Old Active','2026-05-02T12:30:00Z')]); conn.close()
            orig_markets=monitor.MARKETS; orig_fetch=monitor.fetch_market_rows; orig_iso_now=monitor.iso_now
            try:
                monitor.MARKETS=('hk',)
                def fake_fetch(market, observed):
                    return [self.row('hk','2026-05-01',5,'old','Old Active',observed)], {'market':'hk','status':'ok','source':'test','row_count':1}
                monitor.fetch_market_rows=fake_fetch
                monitor.iso_now=lambda: '2026-05-02T13:30:00Z'
                args=type('Args', (), {'root': str(root), 'db': str(db)})()
                out=monitor.run_once(args)
            finally:
                monitor.MARKETS=orig_markets; monitor.fetch_market_rows=orig_fetch; monitor.iso_now=orig_iso_now
            self.assertIn('"action":"finalize"', out)
            with sqlite3.connect(db) as conn:
                self.assertEqual(monitor.get_meta(conn,'hk.active_date'), '2026-05-02')
                fin=conn.execute('select switch_to_date from finalizations where market="hk" and release_date="2026-05-01"').fetchone()
                self.assertIsNotNone(fin)
                self.assertEqual(fin[0], '2026-05-02')
    def test_run_once_stays_silent_when_forced_active_date_waits_for_chartoo_lag(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'root'; db=Path(td)/'s.sqlite'
            conn=sqlite3.connect(db); monitor.ensure_db(conn); monitor.set_meta(conn,'hk.active_date','2026-05-03'); conn.close()
            orig_markets=monitor.MARKETS; orig_fetch=monitor.fetch_market_rows; orig_iso_now=monitor.iso_now
            try:
                monitor.MARKETS=('hk',)
                def fake_fetch(market, observed):
                    return [self.row('hk','2026-05-02',5,'lag','Lagging Latest',observed)], {'market':'hk','status':'ok','source':'test','row_count':1}
                monitor.fetch_market_rows=fake_fetch
                monitor.iso_now=lambda: '2026-05-03T14:30:00Z'
                args=type('Args', (), {'root': str(root), 'db': str(db)})()
                out=monitor.run_once(args)
            finally:
                monitor.MARKETS=orig_markets; monitor.fetch_market_rows=orig_fetch; monitor.iso_now=orig_iso_now
            self.assertEqual(out, '{"wakeAgent": false}')
            with sqlite3.connect(db) as conn:
                self.assertEqual(monitor.get_meta(conn,'hk.active_date'), '2026-05-03')
                self.assertEqual(conn.execute('select count(*) from finalizations').fetchone()[0], 0)
    def test_run_once_stays_silent_when_source_regresses_after_finalized_transition(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'root'; db=Path(td)/'s.sqlite'
            conn=sqlite3.connect(db); monitor.ensure_db(conn); monitor.set_meta(conn,'hk.active_date','2026-05-11')
            conn.execute('''INSERT INTO finalizations(market,release_date,finalized_at_utc,switch_to_date,status) VALUES(?,?,?,?,?)''',('hk','2026-05-10','2026-05-11T13:30:15Z','2026-05-11','finalized')); conn.commit(); conn.close()
            orig_markets=monitor.MARKETS; orig_fetch=monitor.fetch_market_rows; orig_iso_now=monitor.iso_now
            try:
                monitor.MARKETS=('hk',)
                def fake_fetch(market, observed):
                    return [self.row('hk','2026-05-09',5,'regressed','Regressed Latest',observed)], {'market':'hk','status':'ok','source':'test','row_count':1}
                monitor.fetch_market_rows=fake_fetch
                monitor.iso_now=lambda: '2026-05-11T18:30:00Z'
                args=type('Args', (), {'root': str(root), 'db': str(db)})()
                out=monitor.run_once(args)
            finally:
                monitor.MARKETS=orig_markets; monitor.fetch_market_rows=orig_fetch; monitor.iso_now=orig_iso_now
            self.assertEqual(out, '{"wakeAgent": false}')
            with sqlite3.connect(db) as conn:
                self.assertEqual(monitor.get_meta(conn,'hk.active_date'), '2026-05-11')
                self.assertEqual(conn.execute('select count(*) from finalizations where market="hk" and release_date="2026-05-11"').fetchone()[0], 0)
                payload=json.loads(conn.execute('select payload_json from runs order by started_at_utc desc limit 1').fetchone()[0])
                self.assertEqual(payload['markets']['hk']['event'], 'waiting_for_chartoo_source_catchup')
                self.assertEqual(payload['markets']['hk']['source_lag_days'], 2)
    def test_run_once_does_not_skip_calendar_day_when_chartoo_jumps_ahead(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'root'; db=Path(td)/'s.sqlite'
            conn=sqlite3.connect(db); monitor.ensure_db(conn); monitor.set_meta(conn,'hk.active_date','2026-04-30'); monitor.record_observations(conn,'seed',[self.row('hk','2026-04-30',5,'old','Old Active','2026-05-01T12:30:00Z')]); conn.close()
            orig_markets=monitor.MARKETS; orig_fetch=monitor.fetch_market_rows; orig_iso_now=monitor.iso_now
            try:
                monitor.MARKETS=('hk',)
                def fake_fetch(market, observed):
                    return [
                        self.row('hk','2026-04-30',5,'old','Old Active',observed),
                        self.row('hk','2026-05-02',8,'jump','Jumped Visible Latest',observed),
                    ], {'market':'hk','status':'ok','source':'test','row_count':2}
                monitor.fetch_market_rows=fake_fetch
                monitor.iso_now=lambda: '2026-05-01T13:30:00Z'
                args=type('Args', (), {'root': str(root), 'db': str(db)})()
                out=monitor.run_once(args)
            finally:
                monitor.MARKETS=orig_markets; monitor.fetch_market_rows=orig_fetch; monitor.iso_now=orig_iso_now
            self.assertIn('"action":"finalize"', out)
            with sqlite3.connect(db) as conn:
                self.assertEqual(monitor.get_meta(conn,'hk.active_date'), '2026-05-01')
                fin=conn.execute('select switch_to_date from finalizations where market="hk" and release_date="2026-04-30"').fetchone()
                self.assertIsNotNone(fin)
                self.assertEqual(fin[0], '2026-05-01')
    def test_finalize_exposes_fixed_beijing_switch_time(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'root'; conn=sqlite3.connect(Path(td)/'s.sqlite'); monitor.ensure_db(conn)
            rows=[self.row('hk','2026-04-28',6,'a6','Be a star','2026-04-29T10:19:46Z'), self.row('hk','2026-04-28',8,'a6','Be a star','2026-04-29T12:30:47Z')]
            monitor.record_observations(conn,'r1',rows)
            fin=monitor.finalize_market_date(conn,root,'hk','2026-04-28','2026-04-29','2026-04-29T13:03:51Z')
            self.assertEqual(fin['switch_info']['switch_window_utc'],'2026-04-29T12:30:47Z → 2026-04-29T13:03:51Z')
            self.assertEqual(fin['switch_info']['observed_switch_window_bj'],'2026-04-29 20:30:47 → 2026-04-29 21:03:51 北京时间')
            self.assertEqual(fin['switch_info']['switch_window_bj'],'2026-04-29 21:30 北京时间')
            md=Path(fin['reports']['report_md']).read_text(encoding='utf-8')
            self.assertIn('switch_window_bj: 2026-04-29 21:30 北京时间', md)
    def test_prompt_slim_track_strips_invisible_unicode(self):
        track={'final_rank':1,'title':'Song\u200dName','artist':'A\u2060rtist','best_rank':1,'occurrence_count':2,'source_track_url':'https://example.test/a\u200bb'}
        slim=monitor.slim_track_for_prompt(track)
        dumped=monitor.output.json_dumps_compact(slim)
        self.assertNotIn('\u200d', dumped)
        self.assertNotIn('\u2060', dumped)
        self.assertNotIn('\u200b', dumped)
        self.assertIn('SongName', dumped)
if __name__ == '__main__': unittest.main()
