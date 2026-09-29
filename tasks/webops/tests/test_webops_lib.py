import json
import os
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.webops import locks, output, paths, playwright_env, state_json


class WebOpsLibTests(unittest.TestCase):
    def test_paths_expose_current_profile_layout(self):
        self.assertEqual(paths.PROFILE_ROOT, Path(os.environ.get('PYLON_PROFILE_ROOT', '/srv/pylon/profiles/webops')))
        self.assertEqual(paths.SCRIPTS_DIR, paths.PROFILE_ROOT / 'scripts')
        self.assertEqual(paths.CHARTOO_DATA_ROOT, paths.PROFILE_ROOT / 'data' / 'chartoo_itunes_daily')
        self.assertEqual(paths.PLAYWRIGHT_BROWSERS_PATH, Path(os.environ.get('PYLON_HOME', '/srv/pylon')) / 'playwright-browsers')

    def test_output_helpers_preserve_cron_silent_contracts(self):
        self.assertEqual(output.wake_agent_false(), '{"wakeAgent": false}')
        self.assertEqual(output.silent(), '[SILENT]')
        self.assertEqual(
            output.json_dumps_compact({'b': 1, 'a': '中文'}),
            '{"b":1,"a":"中文"}',
        )

    def test_output_helpers_strip_invisible_prompt_controls(self):
        payload = {
            'title\u200d': 'A\u200dB',
            'items': ['C\u200bD', {'body': 'E\u2060F', 'bidi': 'G\u202eH'}],
        }
        dumped = output.json_dumps_compact(payload)
        for char in ['\u200b', '\u200c', '\u200d', '\u2060', '\ufeff', '\u202e']:
            self.assertNotIn(char, dumped)
        self.assertIn('"title":"AB"', dumped)
        self.assertIn('"body":"EF"', dumped)

    def test_state_json_loads_default_on_missing_or_corrupt_and_saves_utf8(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'state.json'
            self.assertEqual(state_json.load(path, default={'items': []}), {'items': []})
            path.write_text('{bad json', encoding='utf-8')
            self.assertEqual(state_json.load(path, default={'items': []}), {'items': []})
            state_json.save(path, {'items': ['中文']})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'items': ['中文']})

    def test_nonblocking_file_lock_reports_contention(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / 'task.lock'
            with locks.nonblocking_file_lock(lock_path) as first:
                self.assertTrue(first.acquired)
                with locks.nonblocking_file_lock(lock_path) as second:
                    self.assertFalse(second.acquired)
            with locks.nonblocking_file_lock(lock_path) as third:
                self.assertTrue(third.acquired)

    def test_nonblocking_file_lock_does_not_truncate_holder_pid_on_contention(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / 'task.lock'
            with locks.nonblocking_file_lock(lock_path) as first:
                self.assertTrue(first.acquired)
                holder_pid = lock_path.read_text(encoding='utf-8')
                self.assertEqual(holder_pid, str(os.getpid()))
                with locks.nonblocking_file_lock(lock_path) as second:
                    self.assertFalse(second.acquired)
                self.assertEqual(lock_path.read_text(encoding='utf-8'), holder_pid)

    def test_playwright_env_setdefault_does_not_override_existing_value(self):
        old = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
        try:
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/custom/path'
            self.assertEqual(playwright_env.ensure(), '/custom/path')
            del os.environ['PLAYWRIGHT_BROWSERS_PATH']
            self.assertEqual(playwright_env.ensure(), str(paths.PLAYWRIGHT_BROWSERS_PATH))
        finally:
            if old is None:
                os.environ.pop('PLAYWRIGHT_BROWSERS_PATH', None)
            else:
                os.environ['PLAYWRIGHT_BROWSERS_PATH'] = old

if __name__ == '__main__':
    unittest.main()
