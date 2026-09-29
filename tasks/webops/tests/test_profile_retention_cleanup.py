import gzip
import itertools
import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from webops_test_utils import load_script_module


_MODULE_COUNTER = itertools.count()


def load_cleanup_module():
    return load_script_module(
        f'profile_retention_cleanup_test_{next(_MODULE_COUNTER)}',
        'profile_retention_cleanup.py',
    )


class ProfileRetentionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name) / 'webops'
        self.root.mkdir()
        self.cleanup = load_cleanup_module()
        self.cleanup.PROFILE_ROOT = self.root

    def tearDown(self):
        self.tmpdir.cleanup()

    def make_profile(self):
        profile = self.root / 'data' / 'app' / 'browser-profile'
        profile.mkdir(parents=True)
        lock_path = self.root / 'data' / 'app' / 'browser.lock'
        return profile, lock_path

    def make_rule(self, profile, lock_path, cache_roots=None):
        return {
            'name': 'test_browser_profile',
            'path': profile,
            'lock_path': lock_path,
            'cache_roots': cache_roots or [
                'Default/Cache',
                'Default/Code Cache',
            ],
        }

    def write_file(self, path, payload=b'x'):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return path

    def iso_at(self, timestamp):
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()

    def make_linuxdo_data(self):
        data_root = self.root / 'data' / 'linuxdo_notice'
        data_root.mkdir(parents=True)
        self.cleanup.LINUXDO_DATA_ROOT = data_root
        self.cleanup.LINUXDO_DECISION_LOG_FILE = data_root / 'decisions.jsonl'
        self.cleanup.LINUXDO_DECISION_ARCHIVE_DIR = data_root / 'archives'
        return data_root


    def write_jsonl(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for row in rows:
            if isinstance(row, str):
                lines.append(row)
            else:
                lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def populate_profile(self, profile):
        cache_a = self.write_file(profile / 'Default' / 'Cache' / 'Cache_Data' / 'a.bin', b'a' * 7)
        cache_b = self.write_file(profile / 'Default' / 'Code Cache' / 'js' / 'b.bin', b'b' * 11)
        preserved = [
            self.write_file(profile / 'Default' / 'Cookies', b'cookie'),
            self.write_file(profile / 'Default' / 'Local Storage' / 'leveldb' / 'state.ldb', b'local'),
            self.write_file(profile / 'Default' / 'IndexedDB' / 'db' / 'data.ldb', b'indexed'),
            self.write_file(profile / 'Default' / 'Preferences', b'prefs'),
            self.write_file(profile / 'session_cookie.txt', b'cookie'),
        ]
        return [cache_a, cache_b], preserved

    def make_state_db(self):
        db = self.root / 'state.db'
        with sqlite3.connect(db) as conn:
            conn.execute(
                'CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL)'
            )
            conn.execute('CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL, content TEXT)')
        self.cleanup.STATE_DB = db
        self.cleanup.SESSIONS_DIR = self.root / 'sessions'
        self.cleanup.SESSIONS_DIR.mkdir()
        return db

    def add_session(self, db, session_id, source, started_at, ended_at):
        with sqlite3.connect(db) as conn:
            conn.execute(
                'INSERT INTO sessions (id, source, started_at, ended_at) VALUES (?, ?, ?, ?)',
                (session_id, source, started_at, ended_at),
            )
            conn.execute('INSERT INTO messages (session_id, content) VALUES (?, ?)', (session_id, f'message:{session_id}'))

    def write_session_files(self, session_id, payload=b'session'):
        paths = [
            self.cleanup.SESSIONS_DIR / f'{session_id}.jsonl',
            self.cleanup.SESSIONS_DIR / f'session_{session_id}.json',
            self.cleanup.SESSIONS_DIR / f'request_dump_{session_id}_20260501_010101_000001.json',
        ]
        for path in paths:
            self.write_file(path, payload)
        return paths

    def test_dry_run_reports_cache_deletions_without_removing_files(self):
        profile, lock_path = self.make_profile()
        cache_files, preserved_files = self.populate_profile(profile)

        result = self.cleanup.cleanup_browser_profile(self.make_rule(profile, lock_path), dry_run=True)

        self.assertFalse(result['skipped_locked'])
        self.assertEqual(result['deleted_files'], 2)
        self.assertEqual(result['deleted_bytes'], 18)
        self.assertEqual(result['before_bytes'], result['after_bytes'])
        for path in cache_files + preserved_files:
            self.assertTrue(path.exists(), path)

    def test_actual_cleanup_removes_only_cache_contents_and_keeps_cache_roots(self):
        profile, lock_path = self.make_profile()
        cache_files, preserved_files = self.populate_profile(profile)

        result = self.cleanup.cleanup_browser_profile(self.make_rule(profile, lock_path), dry_run=False)

        self.assertFalse(result['skipped_locked'])
        self.assertEqual(result['deleted_files'], 2)
        self.assertEqual(result['deleted_bytes'], 18)
        self.assertLess(result['after_bytes'], result['before_bytes'])
        self.assertTrue((profile / 'Default' / 'Cache').is_dir())
        self.assertTrue((profile / 'Default' / 'Code Cache').is_dir())
        for path in cache_files:
            self.assertFalse(path.exists(), path)
        for path in preserved_files:
            self.assertTrue(path.exists(), path)

    def test_locked_profile_is_skipped_without_deleting_cache(self):
        profile, lock_path = self.make_profile()
        cache_files, _ = self.populate_profile(profile)

        with self.cleanup.locks.nonblocking_file_lock(lock_path) as lock_result:
            self.assertTrue(lock_result.acquired)
            result = self.cleanup.cleanup_browser_profile(self.make_rule(profile, lock_path), dry_run=False)

        self.assertTrue(result['skipped_locked'])
        self.assertEqual(result['deleted_files'], 0)
        self.assertEqual(result['deleted_bytes'], 0)
        for path in cache_files:
            self.assertTrue(path.exists(), path)

    def test_profile_path_outside_profile_root_is_rejected(self):
        outside_profile = Path(self.tmpdir.name) / 'outside-profile'
        outside_profile.mkdir()
        rule = self.make_rule(outside_profile, self.root / 'outside.lock')

        with self.assertRaises(ValueError):
            self.cleanup.cleanup_browser_profile(rule, dry_run=True)

    def test_cache_root_outside_profile_is_rejected(self):
        profile, lock_path = self.make_profile()
        rule = self.make_rule(profile, lock_path, cache_roots=['../outside-cache'])

        with self.assertRaises(ValueError):
            self.cleanup.cleanup_browser_profile(rule, dry_run=True)

    def test_run_includes_browser_profile_cleanup_totals(self):
        profile, lock_path = self.make_profile()
        cache_files, _ = self.populate_profile(profile)
        self.cleanup.STATE_DB = self.root / 'state.db'
        self.cleanup.SESSIONS_DIR = self.root / 'sessions'
        self.cleanup.LOCK_PATH = self.root / 'retention.lock'
        self.cleanup.RULES = []
        self.cleanup.BROWSER_PROFILE_RULES = [self.make_rule(profile, lock_path)]

        result = self.cleanup.run(dry_run=True)

        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['total_deleted_files'], 2)
        self.assertEqual(result['total_deleted_bytes'], 18)
        self.assertIn('browser_profile_cleanup', result)
        self.assertEqual(result['browser_profile_cleanup'][0]['deleted_files'], 2)
        for path in cache_files:
            self.assertTrue(path.exists(), path)

    def test_default_rules_preserve_chartoo_raw_and_reports(self):
        rule_names = {rule['name'] for rule in self.cleanup.RULES}
        browser_rule_paths = {str(rule['path']) for rule in self.cleanup.BROWSER_PROFILE_RULES}

        self.assertNotIn('chartoo_raw', rule_names)
        self.assertNotIn('chartoo_reports', rule_names)
        self.assertTrue(all('chartoo_itunes_daily' not in path for path in browser_rule_paths))

    def make_chartoo_daily_data(self):
        root = self.root / 'data' / 'chartoo_itunes_daily'
        html = root / 'html'
        raw = root / 'raw'
        reports = root / 'reports'
        html.mkdir(parents=True)
        raw.mkdir()
        reports.mkdir()
        for date in ('2026-05-29', '2026-05-30', '2026-05-31'):
            self.write_file(html / f'{date}-chartoo.html', f'<html>{date}</html>'.encode())
            self.write_file(raw / f'{date}.json', b'raw')
            self.write_file(reports / f'{date}-jp.json', b'{}')
        self.write_file(html / 'not-a-chartoo-file.txt', b'keep')
        with sqlite3.connect(root / 'state.sqlite') as conn:
            conn.execute('CREATE TABLE observations (id INTEGER PRIMARY KEY, release_date TEXT NOT NULL, title TEXT)')
            conn.execute('CREATE TABLE finalizations (market TEXT NOT NULL, release_date TEXT NOT NULL, status TEXT NOT NULL)')
            for date in ('2026-05-29', '2026-05-30', '2026-05-31'):
                conn.execute('INSERT INTO observations(release_date,title) VALUES(?,?)', (date, f'obs {date}'))
                conn.execute('INSERT INTO finalizations(market,release_date,status) VALUES(?,?,?)', ('jp', date, 'finalized'))
        self.cleanup.CHARTOO_DATA_ROOT = root
        return root

    def test_chartoo_daily_cleanup_keeps_latest_two_release_dates_only(self):
        root = self.make_chartoo_daily_data()

        result = self.cleanup.cleanup_chartoo_daily_data(dry_run=False)

        self.assertEqual(result['kept_release_dates'], ['2026-05-30', '2026-05-31'])
        self.assertEqual(result['deleted_html_files'], 1)
        self.assertNotIn('deleted_tunemymusic_files', result)
        self.assertFalse((root / 'html' / '2026-05-29-chartoo.html').exists())
        self.assertTrue((root / 'html' / '2026-05-30-chartoo.html').exists())
        self.assertTrue((root / 'html' / '2026-05-31-chartoo.html').exists())
        self.assertTrue((root / 'html' / 'not-a-chartoo-file.txt').exists())
        self.assertTrue((root / 'raw' / '2026-05-29.json').exists())
        self.assertTrue((root / 'reports' / '2026-05-29-jp.json').exists())
        with sqlite3.connect(root / 'state.sqlite') as conn:
            self.assertEqual(
                [row[0] for row in conn.execute('SELECT DISTINCT release_date FROM observations ORDER BY release_date')],
                ['2026-05-30', '2026-05-31'],
            )
            self.assertEqual(
                [row[0] for row in conn.execute('SELECT DISTINCT release_date FROM finalizations ORDER BY release_date')],
                ['2026-05-30', '2026-05-31'],
            )

    def test_chartoo_daily_cleanup_dry_run_does_not_delete(self):
        root = self.make_chartoo_daily_data()

        result = self.cleanup.cleanup_chartoo_daily_data(dry_run=True)

        self.assertEqual(result['kept_release_dates'], ['2026-05-30', '2026-05-31'])
        self.assertEqual(result['deleted_html_files'], 1)
        self.assertNotIn('deleted_tunemymusic_files', result)
        self.assertTrue((root / 'html' / '2026-05-29-chartoo.html').exists())
        with sqlite3.connect(root / 'state.sqlite') as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM observations').fetchone()[0], 3)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM finalizations').fetchone()[0], 3)

    def test_prune_noncron_sessions_removes_old_ended_telegram_and_cli_only(self):
        db = self.make_state_db()
        now = time.time()
        old = now - 15 * 86400
        recent = now - 2 * 86400
        self.add_session(db, 'old_telegram', 'telegram', old, old + 60)
        self.add_session(db, 'old_cli', 'cli', old, old + 60)
        self.add_session(db, 'recent_telegram', 'telegram', recent, recent + 60)
        self.add_session(db, 'long_running_recently_ended', 'telegram', old, recent + 60)
        self.add_session(db, 'open_telegram', 'telegram', old, None)
        self.add_session(db, 'old_cron', 'cron', old, old + 60)
        removed_files = self.write_session_files('old_telegram')
        kept_files = self.write_session_files('recent_telegram')

        result = self.cleanup.prune_noncron_sessions(now, dry_run=False)

        self.assertEqual(result['candidate_sessions'], 2)
        self.assertEqual(result['deleted_db_sessions'], 2)
        with sqlite3.connect(db) as conn:
            remaining = {row[0] for row in conn.execute('SELECT id FROM sessions')}
            remaining_messages = {row[0] for row in conn.execute('SELECT session_id FROM messages')}
        self.assertNotIn('old_telegram', remaining)
        self.assertNotIn('old_cli', remaining)
        self.assertIn('recent_telegram', remaining)
        self.assertIn('long_running_recently_ended', remaining)
        self.assertIn('open_telegram', remaining)
        self.assertIn('old_cron', remaining)
        self.assertNotIn('old_telegram', remaining_messages)
        self.assertNotIn('old_cli', remaining_messages)
        for path in removed_files:
            self.assertFalse(path.exists(), path)
        for path in kept_files:
            self.assertTrue(path.exists(), path)

    def test_prune_noncron_sessions_dry_run_keeps_db_and_files(self):
        db = self.make_state_db()
        now = time.time()
        old = now - 15 * 86400
        self.add_session(db, 'old_telegram', 'telegram', old, old + 60)
        session_files = self.write_session_files('old_telegram')

        result = self.cleanup.prune_noncron_sessions(now, dry_run=True)

        self.assertEqual(result['candidate_sessions'], 1)
        self.assertEqual(result['deleted_db_sessions'], 0)
        with sqlite3.connect(db) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0], 1)
        for path in session_files:
            self.assertTrue(path.exists(), path)

    def test_prune_orphan_session_files_removes_only_old_unlinked_files(self):
        db = self.make_state_db()
        now = time.time()
        self.add_session(db, 'linked', 'telegram', now - 30 * 86400, now - 30 * 86400 + 60)
        linked = self.write_file(self.cleanup.SESSIONS_DIR / 'session_linked.json', b'linked')
        old_orphan = self.write_file(self.cleanup.SESSIONS_DIR / 'session_old_orphan.json', b'old')
        recent_orphan = self.write_file(self.cleanup.SESSIONS_DIR / 'session_recent_orphan.json', b'recent')
        old_mtime = now - 8 * 86400
        os.utime(linked, (old_mtime, old_mtime))
        os.utime(old_orphan, (old_mtime, old_mtime))

        result = self.cleanup.prune_orphan_session_files(now, dry_run=False)

        self.assertEqual(result['candidate_files'], 1)
        self.assertFalse(old_orphan.exists())
        self.assertTrue(recent_orphan.exists())
        self.assertTrue(linked.exists())

    def test_archive_linuxdo_decision_log_dry_run_keeps_source_and_archives_untouched(self):
        self.make_linuxdo_data()
        now = time.time()
        old_entry = {'logged_at': self.iso_at(now - 3 * 86400), 'topic_id': 'old'}
        recent_entry = {'logged_at': self.iso_at(now - 3600), 'topic_id': 'recent'}
        undated_entry = {'topic_id': 'undated'}
        malformed_line = '{bad json'
        self.write_jsonl(self.cleanup.LINUXDO_DECISION_LOG_FILE, [old_entry, recent_entry, undated_entry, malformed_line])
        original = self.cleanup.LINUXDO_DECISION_LOG_FILE.read_text(encoding='utf-8')

        result = self.cleanup.archive_linuxdo_decision_log(now, dry_run=True)

        self.assertEqual(result['archived_lines'], 1)
        self.assertEqual(result['retained_lines'], 3)
        self.assertEqual(result['malformed_lines'], 1)
        self.assertEqual(result['undated_lines'], 1)
        self.assertEqual(self.cleanup.LINUXDO_DECISION_LOG_FILE.read_text(encoding='utf-8'), original)
        self.assertFalse(self.cleanup.LINUXDO_DECISION_ARCHIVE_DIR.exists())

    def test_archive_linuxdo_decision_log_moves_old_entries_to_gzip_and_keeps_recent_lines(self):
        self.make_linuxdo_data()
        now = time.time()
        old_ts = self.iso_at(now - 3 * 86400)
        old_entry = {'logged_at': old_ts, 'topic_id': 'old'}
        recent_entry = {'logged_at': self.iso_at(now - 3600), 'topic_id': 'recent'}
        undated_entry = {'topic_id': 'undated'}
        malformed_line = '{bad json'
        self.write_jsonl(self.cleanup.LINUXDO_DECISION_LOG_FILE, [old_entry, recent_entry, undated_entry, malformed_line])

        result = self.cleanup.archive_linuxdo_decision_log(now, dry_run=False)

        archive_date = datetime.fromisoformat(old_ts).date().isoformat()
        archive_path = self.cleanup.LINUXDO_DECISION_ARCHIVE_DIR / f'decisions-{archive_date}.jsonl.gz'
        self.assertEqual(result['archived_lines'], 1)
        self.assertEqual(result['retained_lines'], 3)
        self.assertTrue(archive_path.exists())
        with gzip.open(archive_path, 'rt', encoding='utf-8') as handle:
            archived_lines = handle.read().splitlines()
        self.assertEqual(len(archived_lines), 1)
        self.assertEqual(json.loads(archived_lines[0])['topic_id'], 'old')
        active_lines = self.cleanup.LINUXDO_DECISION_LOG_FILE.read_text(encoding='utf-8').splitlines()
        self.assertEqual(json.loads(active_lines[0])['topic_id'], 'recent')
        self.assertEqual(json.loads(active_lines[1])['topic_id'], 'undated')
        self.assertEqual(active_lines[2], malformed_line)


    def test_run_includes_linuxdo_cleanup_totals(self):
        self.make_linuxdo_data()
        now = time.time()
        self.cleanup.STATE_DB = self.root / 'state.db'
        self.cleanup.SESSIONS_DIR = self.root / 'sessions'
        self.cleanup.LOCK_PATH = self.root / 'retention.lock'
        self.cleanup.RULES = []
        self.cleanup.BROWSER_PROFILE_RULES = []
        old_mtime = now - 3 * 86400
        self.write_jsonl(
            self.cleanup.LINUXDO_DECISION_LOG_FILE,
            [{'logged_at': self.iso_at(old_mtime), 'topic_id': 'old'}],
        )

        result = self.cleanup.run(dry_run=True)

        self.assertEqual(result['status'], 'ok')
        self.assertIn('linuxdo_decision_log_archive', result)
        self.assertNotIn('linuxdo_daily_reports_cleanup', result)
        self.assertNotIn('linuxdo_ocr_cache_prune', result)
        self.assertEqual(result['linuxdo_decision_log_archive']['archived_lines'], 1)
        self.assertGreater(result['total_deleted_bytes'], 0)



if __name__ == '__main__':
    unittest.main()
