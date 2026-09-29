import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]

from pylon.admin import chiphell as control


def write_marker(directory: Path, value: str) -> None:
    directory.mkdir(parents=True)
    (directory / "marker").write_text(value)


class CookieImportTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.paths = {
            "DATA_ROOT": self.root,
            "PROFILE_DIR": self.root / "browser-profile",
            "CANDIDATE_PROFILE_DIR": self.root / ".browser-profile.import",
            "ROLLBACK_PROFILE_DIR": self.root / ".browser-profile.rollback",
            "IMPORT_LOCK_PATH": self.root / ".cookie-import.lock",
            "IMPORT_TRANSACTION_PATH": self.root / ".cookie-import-transaction.json",
            "IMPORT_TRANSACTION_TMP_PATH": self.root / ".cookie-import-transaction.json.tmp",
            "HEALTH_PATH": self.root / "proxy-health.json",
        }
        self.path_patch = mock.patch.multiple(control, **self.paths)
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_reconcile_reads_current_timer_without_changing_it(self):
        with mock.patch.object(control, 'run', return_value=SimpleNamespace(returncode=0)) as run, \
                mock.patch.object(control, 'service_active', return_value=False), \
                mock.patch.object(control, 'ensure_resume_ready'), \
                mock.patch.object(control, 'cron') as cron:
            control.command_reconcile()
        cron.assert_not_called()
        self.assertEqual(run.call_args_list[-1].args[0], ['systemctl', 'start', control.SERVICE])

    def test_reconcile_does_not_interrupt_cookie_import(self):
        with control.cookie_import_lock(), mock.patch.object(control, 'run') as run:
            self.assertEqual(control.command_reconcile(), 0)
        run.assert_not_called()

    def test_resume_sets_desired_timer_before_starting_poller(self):
        calls = []
        with mock.patch.object(control, 'run', side_effect=lambda args: calls.append(args)), \
                mock.patch.object(control, 'ensure_resume_ready'), \
                mock.patch.object(control, 'cron', side_effect=lambda action: calls.append(action)):
            control.command_resume()
        self.assertEqual(calls, ['resume', ['systemctl', 'start', control.SERVICE]])

    def test_transaction_state_is_mode_0600_and_contains_no_cookie_payload(self):
        control.write_import_transaction(was_active=True, stage="PREPARE", started_at=123)
        transaction = self.paths["IMPORT_TRANSACTION_PATH"]
        self.assertEqual(transaction.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            json.loads(transaction.read_text()),
            {
                "poller_was_active": True,
                "stage": "PREPARE",
                "started_at": 123,
                "version": 1,
            },
        )

    def test_profile_candidate_can_be_restored_or_committed(self):
        profile = self.paths["PROFILE_DIR"]
        candidate = self.paths["CANDIDATE_PROFILE_DIR"]
        rollback = self.paths["ROLLBACK_PROFILE_DIR"]
        write_marker(profile, "old")
        write_marker(candidate, "new")

        control.install_candidate_profile()
        self.assertEqual((profile / "marker").read_text(), "new")
        self.assertEqual((rollback / "marker").read_text(), "old")

        control.restore_previous_profile()
        self.assertEqual((profile / "marker").read_text(), "old")
        self.assertFalse(candidate.exists())
        self.assertFalse(rollback.exists())

        write_marker(candidate, "new")
        control.install_candidate_profile()
        control.commit_profile_import()
        self.assertEqual((profile / "marker").read_text(), "new")
        self.assertFalse(candidate.exists())
        self.assertFalse(rollback.exists())

    def test_interrupted_swapped_transaction_restores_profile_and_desired_service_state(self):
        profile = self.paths["PROFILE_DIR"]
        rollback = self.paths["ROLLBACK_PROFILE_DIR"]
        write_marker(profile, "new")
        write_marker(rollback, "old")
        control.write_import_transaction(was_active=True, stage="SWAPPED", started_at=123)

        with mock.patch.object(control, "service_active", return_value=False), mock.patch.object(control, "run") as run:
            desired_active = control.recover_interrupted_import()

        self.assertTrue(desired_active)
        self.assertEqual((profile / "marker").read_text(), "old")
        self.assertFalse(rollback.exists())
        self.assertFalse(self.paths["IMPORT_TRANSACTION_PATH"].exists())
        run.assert_called_once_with(["systemctl", "stop", control.SERVICE], check=False)

    def test_interrupted_committing_transaction_keeps_validated_new_profile(self):
        profile = self.paths["PROFILE_DIR"]
        rollback = self.paths["ROLLBACK_PROFILE_DIR"]
        write_marker(profile, "new")
        write_marker(rollback, "old")
        control.write_import_transaction(was_active=False, stage="COMMITTING", started_at=123)

        with mock.patch.object(control, "service_active", return_value=True), mock.patch.object(control, "run"):
            desired_active = control.recover_interrupted_import()

        self.assertFalse(desired_active)
        self.assertEqual((profile / "marker").read_text(), "new")
        self.assertFalse(rollback.exists())

    def test_health_result_must_have_a_new_file_signature(self):
        health = self.paths["HEALTH_PATH"]
        health.write_text(json.dumps({"status": "LOGIN_REQUIRED", "last_poll_at": 99}))
        old_signature = control.health_signature()
        replacement = self.root / "health.new"
        replacement.write_text(json.dumps({"status": "OK", "last_poll_at": 100}))
        os.replace(replacement, health)
        self.assertEqual(
            control.wait_for_fresh_health(100, old_signature, timeout_seconds=1)["status"],
            "OK",
        )


class CookieImportCommandTests(unittest.TestCase):
    def valid_payload(self) -> bytes:
        return json.dumps({
            "cookies": [{
                "name": "secret-name",
                "value": "secret-value",
                "domain": ".chiphell.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }],
        }).encode()

    def run_import(self, health_status: str):
        validation = {
            "count": 1,
            "home": "OK",
            "home_http": 200,
            "fid": "OK",
            "fid_http": 200,
            "rows": 42,
        }
        stdout = io.StringIO()
        stderr = io.StringIO()
        run = mock.Mock()
        restore = mock.Mock()
        commit = mock.Mock()
        with mock.patch.object(control.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(self.valid_payload()))), \
                mock.patch.object(control, "cookie_import_lock", return_value=contextlib.nullcontext()), \
                mock.patch.object(control, "recover_interrupted_import", return_value=None), \
                mock.patch.object(control, "service_active", return_value=True), \
                mock.patch.object(control, "write_import_transaction"), \
                mock.patch.object(control, "clear_import_transaction"), \
                mock.patch.object(control, "validate_candidate_profile", return_value=validation), \
                mock.patch.object(control, "install_candidate_profile"), \
                mock.patch.object(control, "restore_previous_profile", restore), \
                mock.patch.object(control, "commit_profile_import", commit), \
                mock.patch.object(control, "health_signature", return_value=(1, 2, 3)), \
                mock.patch.object(control, "wait_for_fresh_health", return_value={"status": health_status}), \
                mock.patch.object(control, "_restore_service"), \
                mock.patch.object(control, "run", run), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = control.command_import_cookies()
        return result, stdout.getvalue(), stderr.getvalue(), run, restore, commit

    def test_ok_health_commits_candidate_without_exposing_cookie_fields(self):
        result, stdout, stderr, run, restore, commit = self.run_import("OK")
        self.assertEqual(result, 0)
        self.assertIn("COOKIE_IMPORT_OK count=1 home=OK home_http=200 fid328=OK fid328_http=200 rows=42 poller=OK", stdout)
        self.assertNotIn("secret-name", stdout + stderr)
        self.assertNotIn("secret-value", stdout + stderr)
        restore.assert_not_called()
        commit.assert_called_once()
        self.assertIn(mock.call(["systemctl", "start", control.SERVICE]), run.mock_calls)

    def test_proxy_failure_keeps_real_page_validated_candidate(self):
        result, stdout, _, _, restore, commit = self.run_import("PROXY_UNAVAILABLE")
        self.assertEqual(result, 0)
        self.assertIn("COOKIE_IMPORT_OK_RECOVERY_PENDING", stdout)
        restore.assert_not_called()
        commit.assert_called_once()

    def test_login_required_after_swap_restores_old_profile(self):
        result, _, stderr, _, restore, commit = self.run_import("LOGIN_REQUIRED")
        self.assertEqual(result, 1)
        self.assertIn("REMOTE_LOGIN_REQUIRED stage=POST_SWAP status=LOGIN_REQUIRED", stderr)
        restore.assert_called_once()
        commit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
