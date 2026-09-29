import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCRIPT_DIR = ROOT.parent / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))


def load_module(name: str, filename: str):
    path = SCRIPT_DIR / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


alert = load_module("xianbao_alert_test", "xianbao_alert.py")


class XianbaoAlertTests(unittest.TestCase):
    def test_cron_job_tail_prefers_recent_output_and_agent_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            output_dir = profile / "cron" / "output" / "xianbao-hermes-poll"
            output_dir.mkdir(parents=True)
            (output_dir / "2026-06-16_19-29-25.md").write_text(
                "# Cron Job: xianbao-hermes-poll\n\n"
                "**Job ID:** xianbao-hermes-poll\n\n"
                "poll json source unavailable url=https://new.xianbao.fun/plus/json/push.json failure_type=URLError\n",
                encoding="utf-8",
            )
            logs_dir = profile / "logs"
            logs_dir.mkdir()
            (logs_dir / "agent.log").write_text(
                "2026-06-16 19:29:25 INFO cron.scheduler: Job 'xianbao-hermes-poll' (no_agent): empty stdout — silent run\n"
                "2026-06-16 19:29:25 INFO cron.scheduler: Job 'other-job': ignored\n",
                encoding="utf-8",
            )

            tail = alert.cron_job_tail(profile, "xianbao-hermes-poll", lines=12)

        self.assertIn("Recent cron output", tail)
        self.assertIn("poll json source unavailable", tail)
        self.assertIn("Recent scheduler log", tail)
        self.assertIn("xianbao-hermes-poll", tail)
        self.assertNotIn("other-job", tail)


if __name__ == "__main__":
    unittest.main()
