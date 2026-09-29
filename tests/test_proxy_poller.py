import importlib.util
import json
import sys
import tempfile
import types
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class JsonState:
    @staticmethod
    def load(path, default=None):
        target = Path(path)
        if not target.exists():
            return default
        return json.loads(target.read_text())

    @staticmethod
    def save(path, value):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value))


fake_base = types.ModuleType("chiphell_fid26_monitor")
fake_base.paths = SimpleNamespace(CHIPHELL_DATA_ROOT=Path(tempfile.gettempdir()) / "chiphell-poller-tests")
fake_base.state_json = JsonState

spec = importlib.util.spec_from_file_location(
    "chiphell_fid26_proxy_poller_for_tests",
    ROOT / "src/pylon/tasks/chiphell/poller.py",
)
poller = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(poller)
poller.base = fake_base


class PollerFaultIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.log_patch = mock.patch.object(poller, "_log_event")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        root = Path(self.tempdir.name)
        poller.QUEUE_PATH = root / "delivery.sqlite3"
        poller.HEALTH_PATH = root / "health.json"
        self.state = {"proxy_runtime": {}}
        fake_base.load_state = lambda path: deepcopy(self.state)

        def save_state(path, value, max_ids):
            self.state = deepcopy(value)

        fake_base.save_state = save_state
        self.args = SimpleNamespace(
            state=root / "state.json",
            max_stored_ids=500,
            fid="328",
        )
        self.diagnostics = {
            "last_error_class": "TimeoutError",
            "last_error_code": "NAVIGATION_TIMEOUT",
            "proxy_probe_status": "OK",
            "proxy_probe_latency_ms": 250,
            "navigation_duration_ms": 80_000,
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def pending_count(self):
        return poller.DeliveryQueue(poller.QUEUE_PATH).pending_count()

    def test_upstream_alerts_only_on_second_persisted_round(self):
        result = ("UPSTREAM_UNAVAILABLE", [], [], self.diagnostics)
        with mock.patch.object(poller, "_collect_proxy_once", return_value=result):
            self.assertEqual(poller._run_cycle(self.args, observe=False), 300)
            self.assertEqual(self.pending_count(), 0)
            first_health = json.loads(poller.HEALTH_PATH.read_text())
            self.assertEqual(first_health["consecutive_failures"], 1)
            self.assertFalse(first_health["alert_sent"])

            self.assertEqual(poller._run_cycle(self.args, observe=False), 900)
            self.assertEqual(self.pending_count(), 1)
            second_health = json.loads(poller.HEALTH_PATH.read_text())
            self.assertEqual(second_health["consecutive_failures"], 2)
            self.assertTrue(second_health["alert_sent"])

    def test_silent_upstream_recovery_creates_no_recovery_notification(self):
        upstream = ("UPSTREAM_UNAVAILABLE", [], [], self.diagnostics)
        healthy = (
            "OK",
            [],
            [],
            {
                "last_error_class": None,
                "last_error_code": None,
                "proxy_probe_status": "NOT_NEEDED",
                "proxy_probe_latency_ms": 0,
                "navigation_duration_ms": 500,
            },
        )
        with mock.patch.object(poller, "_collect_proxy_once", side_effect=[upstream, healthy]), mock.patch.object(
            poller,
            "choose_interval",
            return_value=60,
        ):
            poller._run_cycle(self.args, observe=False)
            self.assertEqual(poller._run_cycle(self.args, observe=False), 60)
        self.assertEqual(self.pending_count(), 0)

    def test_apply_forum_target_overrides_parser_defaults(self):
        args = SimpleNamespace(url="https://www.chiphell.com/forum.php?mod=forumdisplay&fid=26&filter=author&orderby=dateline", fid="26")
        updated = poller.apply_forum_target(args)
        self.assertEqual(updated.fid, "328")
        self.assertEqual(
            updated.url,
            "https://www.chiphell.com/forum.php?mod=forumdisplay&fid=328&filter=author&orderby=dateline",
        )

    def test_confirmed_proxy_failure_alerts_on_first_round(self):
        diagnostics = dict(self.diagnostics)
        diagnostics.update({
            "proxy_probe_status": "LISTENER_DOWN",
            "proxy_probe_latency_ms": 1,
            "last_error_code": "net::ERR_PROXY_CONNECTION_FAILED",
        })
        result = ("PROXY_UNAVAILABLE", [], [], diagnostics)
        with mock.patch.object(poller, "_collect_proxy_once", return_value=result):
            self.assertEqual(poller._run_cycle(self.args, observe=False), 300)
        self.assertEqual(self.pending_count(), 1)
        health = json.loads(poller.HEALTH_PATH.read_text())
        self.assertEqual(health["status"], "PROXY_UNAVAILABLE")
        self.assertEqual(health["consecutive_failures"], 1)
        self.assertTrue(health["alert_sent"])


if __name__ == "__main__":
    unittest.main()
