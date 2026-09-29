import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_PATH = ROOT / "plugins" / "software-update-monitor-reply" / "__init__.py"


def load_plugin():
    if not PLUGIN_PATH.exists():
        raise AssertionError(f"missing plugin module: {PLUGIN_PATH}")
    spec = importlib.util.spec_from_file_location("software_update_monitor_reply_test", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class SoftwareUpdateMonitorReplyPluginTests(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.pending_path = self.root / "pending.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def telegram_source(self, chat_id="chat"):
        return SimpleNamespace(
            platform=SimpleNamespace(value="telegram"),
            chat_id=chat_id,
            user_id="user",
            chat_type="dm",
            thread_id=None,
        )

    def gateway(self, authorized=True):
        return SimpleNamespace(_is_user_authorized=lambda _source: authorized)

    def test_parse_strict_commands(self):
        add = self.plugin.parse_command("增加监控 https://github.com/openai/codex")
        self.assertEqual(add.kind, "add")
        self.assertEqual(add.value, "https://github.com/openai/codex")

        add_short = self.plugin.parse_command("加监控 owner/repo")
        self.assertEqual(add_short.kind, "add")
        self.assertEqual(add_short.value, "owner/repo")

        add_site = self.plugin.parse_command("监控这个网站 https://example.com/feed.xml")
        self.assertEqual(add_site.kind, "add")
        self.assertEqual(add_site.value, "https://example.com/feed.xml")

        remove = self.plugin.parse_command("移除监控")
        self.assertEqual(remove.kind, "remove_list")
        self.assertIsNone(remove.value)

        numeric = self.plugin.parse_command("  3  ", has_pending=True)
        self.assertEqual(numeric.kind, "remove_number")
        self.assertEqual(numeric.value, "3")

        self.assertIsNone(self.plugin.parse_command("3", has_pending=False))
        self.assertIsNone(self.plugin.parse_command("增加监控"))
        self.assertIsNone(self.plugin.parse_command("帮我增加监控 https://github.com/openai/codex"))

    def test_non_telegram_or_unauthorized_sources_are_ignored(self):
        sent = []
        event = SimpleNamespace(
            text="增加监控 owner/repo",
            source=SimpleNamespace(platform=SimpleNamespace(value="slack"), chat_id="chat"),
        )

        with mock.patch.object(self.plugin, "send_reply", lambda *_args: sent.append(_args[-1])):
            self.assertIsNone(self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway()))

        source = self.telegram_source()
        event = SimpleNamespace(text="增加监控 owner/repo", source=source)
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            self.plugin, "send_reply", lambda *_args: sent.append(_args[-1])
        ):
            self.assertIsNone(self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway(False)))
        self.assertEqual(sent, [])

    def test_home_channel_is_authorized_when_gateway_denies(self):
        sent = []
        source = self.telegram_source(chat_id="home-chat")
        event = SimpleNamespace(text="增加监控 owner/repo", source=source)

        with mock.patch.dict("os.environ", {"TELEGRAM_HOME_CHANNEL": "home-chat"}), mock.patch.object(
            self.plugin, "PENDING_PATH", self.pending_path
        ), mock.patch.object(
            self.plugin,
            "run_monitor_json",
            side_effect=[
                self.plugin.CommandResult(
                    returncode=0,
                    data={"ok": True, "added": ["github_owner_repo_releases"], "sources": [{"name": "owner/repo Releases"}]},
                    output="{}",
                ),
                self.plugin.CommandResult(returncode=0, data={"ok": True}, output="{}"),
            ],
        ), mock.patch.object(self.plugin, "send_reply", lambda _gateway, _source, message: sent.append(message)):
            result = self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway(False))

        self.assertEqual(result, {"action": "skip", "reason": "software-update-monitor-reply handled"})
        self.assertIn("已添加", sent[0])

    def test_add_success_validates_and_replies(self):
        sent = []
        event = SimpleNamespace(text="增加监控 owner/repo", source=self.telegram_source())
        calls = []

        def fake_run(args):
            calls.append(args)
            if args[:2] == ["--add-monitor-url", "owner/repo"]:
                return self.plugin.CommandResult(
                    returncode=0,
                    data={"ok": True, "added": ["github_owner_repo_releases"], "sources": [{"name": "owner/repo Releases"}]},
                    output="{}",
                )
            if args == ["--validate-config", "--json"]:
                return self.plugin.CommandResult(returncode=0, data={"ok": True}, output="{}")
            raise AssertionError(args)

        with mock.patch.object(self.plugin, "PENDING_PATH", self.pending_path), mock.patch.object(
            self.plugin, "run_monitor_json", fake_run
        ), mock.patch.object(self.plugin, "send_reply", lambda _gateway, _source, message: sent.append(message)):
            result = self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway())

        self.assertEqual(result, {"action": "skip", "reason": "software-update-monitor-reply handled"})
        self.assertEqual(
            calls,
            [
                ["--add-monitor-url", "owner/repo", "--json"],
                ["--validate-config", "--json"],
            ],
        )
        self.assertIn("已添加", sent[0])
        self.assertIn("validate: OK", sent[0])

    def test_add_existing_and_unsupported_replies(self):
        existing = self.plugin.format_add_reply(
            self.plugin.CommandResult(
                returncode=0,
                data={"ok": True, "existing": ["github_owner_repo_releases"], "sources": [{"name": "owner/repo Releases"}]},
                output="{}",
            ),
            {"ok": True},
        )
        self.assertIn("已存在", existing)
        self.assertIn("validate: OK", existing)

        unsupported = self.plugin.format_add_reply(
            self.plugin.CommandResult(
                returncode=2,
                data={"ok": False, "unsupported": True, "error": "unsupported source URL"},
                output="{}",
            ),
            None,
        )
        self.assertIn("不支持，未添加", unsupported)

    def test_add_validate_failure_is_reported(self):
        reply = self.plugin.format_add_reply(
            self.plugin.CommandResult(
                returncode=0,
                data={"ok": True, "added": ["source"], "sources": [{"name": "source"}]},
                output="{}",
            ),
            {"ok": False, "errors": ["bad config"]},
        )

        self.assertIn("已添加", reply)
        self.assertIn("validate: FAIL", reply)

    def test_remove_monitor_lists_projects_and_writes_pending(self):
        sent = []
        event = SimpleNamespace(text="移除监控", source=self.telegram_source())
        projects = [
            {"number": 1, "id": "one", "name": "One"},
            {"number": 2, "id": "two", "name": "Two"},
        ]

        with mock.patch.object(self.plugin, "PENDING_PATH", self.pending_path), mock.patch.object(
            self.plugin,
            "run_monitor_json",
            return_value=self.plugin.CommandResult(returncode=0, data={"projects": projects}, output="{}"),
        ), mock.patch.object(self.plugin, "send_reply", lambda _gateway, _source, message: sent.append(message)):
            result = self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway())

        self.assertEqual(result, {"action": "skip", "reason": "software-update-monitor-reply handled"})
        self.assertIn("1. One", sent[0])
        self.assertIn("2. Two", sent[0])
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertIn("telegram:chat", pending)
        self.assertEqual(pending["telegram:chat"]["projects"], projects)

    def test_numeric_remove_uses_pending_then_clears_it_after_success(self):
        sent = []
        source = self.telegram_source()
        self.plugin.save_pending(
            {
                self.plugin.source_key(source): {
                    "projects": [
                        {"number": 1, "id": "one", "name": "One"},
                        {"number": 2, "id": "two", "name": "Two"},
                    ]
                }
            },
            self.pending_path,
        )
        event = SimpleNamespace(text="2", source=source)
        calls = []

        def fake_run(args):
            calls.append(args)
            if args == ["--remove-project", "two", "--json"]:
                return self.plugin.CommandResult(
                    returncode=0,
                    data={
                        "ok": True,
                        "project": {"name": "Two"},
                        "removed_source_ids": ["two_releases"],
                        "disabled_source_ids": [],
                    },
                    output="{}",
                )
            if args == ["--validate-config", "--json"]:
                return self.plugin.CommandResult(returncode=0, data={"ok": True}, output="{}")
            raise AssertionError(args)

        with mock.patch.object(self.plugin, "PENDING_PATH", self.pending_path), mock.patch.object(
            self.plugin, "run_monitor_json", fake_run
        ), mock.patch.object(self.plugin, "send_reply", lambda _gateway, _source, message: sent.append(message)):
            result = self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway())

        self.assertEqual(result, {"action": "skip", "reason": "software-update-monitor-reply handled"})
        self.assertEqual(calls, [["--remove-project", "two", "--json"], ["--validate-config", "--json"]])
        self.assertIn("已移除监控: Two", sent[0])
        self.assertIn("two_releases", sent[0])
        self.assertIn("validate: OK", sent[0])
        self.assertEqual(json.loads(self.pending_path.read_text(encoding="utf-8")), {})

    def test_numeric_without_pending_is_ignored_and_invalid_number_keeps_pending(self):
        source = self.telegram_source()
        event = SimpleNamespace(text="9", source=source)
        sent = []

        with mock.patch.object(self.plugin, "PENDING_PATH", self.pending_path):
            self.assertIsNone(self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway()))

        self.plugin.save_pending(
            {self.plugin.source_key(source): {"projects": [{"number": 1, "id": "one", "name": "One"}]}},
            self.pending_path,
        )
        with mock.patch.object(self.plugin, "PENDING_PATH", self.pending_path), mock.patch.object(
            self.plugin, "send_reply", lambda _gateway, _source, message: sent.append(message)
        ):
            result = self.plugin.handle_pre_gateway_dispatch(event=event, gateway=self.gateway())

        self.assertEqual(result, {"action": "skip", "reason": "software-update-monitor-reply handled"})
        self.assertIn("编号无效", sent[0])
        pending = json.loads(self.pending_path.read_text(encoding="utf-8"))
        self.assertIn(self.plugin.source_key(source), pending)

    def test_run_monitor_json_uses_fixed_python_and_script(self):
        completed = SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with mock.patch.object(self.plugin.subprocess, "run", return_value=completed) as run:
            result = self.plugin.run_monitor_json(["--validate-config", "--json"])

        self.assertTrue(result.data["ok"])
        command = run.call_args.args[0]
        self.assertEqual(command[0], str(self.plugin.PYTHON))
        self.assertEqual(command[1], str(self.plugin.MONITOR_SCRIPT))
        self.assertEqual(command[2:], ["--validate-config", "--json"])


if __name__ == "__main__":
    unittest.main()
