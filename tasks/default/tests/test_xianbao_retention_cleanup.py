import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "xianbao_retention_cleanup.py"
JOBS_PATH = Path("/srv/hermes/cron/jobs.json")


def load_cleanup_module(testcase):
    testcase.assertTrue(SCRIPT_PATH.exists(), "cleanup script missing")
    spec = importlib.util.spec_from_file_location("xianbao_retention_cleanup", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def touch_file(path, *, age_days=0, age_hours=0, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    age_seconds = age_days * 86400 + age_hours * 3600
    if age_seconds:
        old_time = time.time() - age_seconds
        os.utime(path, (old_time, old_time))
    return path


class XianbaoRetentionCleanupTests(unittest.TestCase):
    def setUp(self):
        self.cleanup = load_cleanup_module(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.profile_root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


    def test_silent_output_cleanup_requires_age_and_silent_status(self):
        output_dir = self.profile_root / "cron" / "output" / "xianbao-hermes-poll"
        old_silent = touch_file(
            output_dir / "old-silent.md",
            age_hours=25,
            text=(
                "# Cron Job: xianbao-hermes-poll\n\n"
                "**Job ID:** xianbao-hermes-poll\n"
                "**Status:** silent (empty output)\n"
            ),
        )
        old_wake_silent = touch_file(
            output_dir / "old-wake-silent.md",
            age_hours=25,
            text="**Status:** silent (wakeAgent=false)\n",
        )
        recent_silent = touch_file(
            output_dir / "recent-silent.md",
            age_hours=2,
            text="**Status:** silent (empty output)\n",
        )
        old_failure = touch_file(
            output_dir / "old-failure.md",
            age_days=10,
            text="**Status:** failed\nTraceback\n",
        )
        old_other_job = touch_file(
            self.profile_root / "cron" / "output" / "other-job" / "old-silent.md",
            age_days=10,
            text="**Status:** silent (empty output)\n",
        )

        result = self.cleanup.run_cleanup(self.profile_root, silent_output_retention_days=1, apply=True)

        self.assertEqual(result["silent_output_candidates"], 2)
        self.assertEqual(result["deleted_files"], 2)
        self.assertFalse(old_silent.exists())
        self.assertFalse(old_wake_silent.exists())
        self.assertTrue(recent_silent.exists())
        self.assertTrue(old_failure.exists())
        self.assertTrue(old_other_job.exists())

    def test_tmp_cleanup_deletes_only_old_poll_out_err_files(self):
        data_dir = self.profile_root / "data" / "xianbao_filter"
        old_out = touch_file(data_dir / "xianbao-poll-out.abcdef", age_hours=26)
        old_err = touch_file(data_dir / "xianbao-poll-err.ghijkl", age_hours=26)
        recent_out = touch_file(data_dir / "xianbao-poll-out.recent", age_hours=2)
        unrelated = touch_file(data_dir / "state.sqlite", age_days=30)

        result = self.cleanup.run_cleanup(self.profile_root, tmp_retention_days=1, apply=True)

        self.assertEqual(result["tmp_candidates"], 2)
        self.assertEqual(result["deleted_files"], 2)
        self.assertFalse(old_out.exists())
        self.assertFalse(old_err.exists())
        self.assertTrue(recent_out.exists())
        self.assertTrue(unrelated.exists())

    def test_symlinks_are_never_deleted(self):
        target = touch_file(self.profile_root / "outside-target.txt", age_days=10)
        link = self.profile_root / "data" / "xianbao_filter" / "xianbao-poll-out.linked"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        old_time = time.time() - 10 * 86400
        os.utime(link, (old_time, old_time), follow_symlinks=False)

        result = self.cleanup.run_cleanup(self.profile_root, apply=True)

        self.assertEqual(result["tmp_candidates"], 0)
        self.assertEqual(result["deleted_files"], 0)
        self.assertTrue(link.exists())
        self.assertTrue(target.exists())

    def test_cli_defaults_to_dry_run(self):
        old_tmp = touch_file(
            self.profile_root / "data" / "xianbao_filter" / "xianbao-poll-out.old",
            age_days=3,
        )

        code = self.cleanup.main(["--profile-root", str(self.profile_root), "--quiet"])

        self.assertEqual(code, 0)
        self.assertTrue(old_tmp.exists())

    def test_xianbao_retention_cron_job_contract(self):
        payload = json.loads(JOBS_PATH.read_text(encoding="utf-8"))
        jobs = {job["id"]: job for job in payload["jobs"]}

        job = jobs["xianbao-retention-cleanup"]
        self.assertTrue(job["enabled"])
        self.assertTrue(job["no_agent"])
        self.assertFalse(job["wrap_response"])
        self.assertTrue(job["compact_failure_delivery"])
        self.assertEqual(job["deliver"], "local")
        self.assertEqual(job["script"], "xianbao_retention_cleanup.sh")
        self.assertEqual(job["schedule"]["kind"], "cron")
        self.assertEqual(job["schedule"]["expr"], "15 4 * * *")


if __name__ == "__main__":
    unittest.main()
