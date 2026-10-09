from __future__ import annotations

from contextlib import contextmanager, nullcontext
import json
import stat
from unittest.mock import Mock

import pytest

from pylon import bot, commands
from pylon.outbox import Outbox
from pylon.tasks.linuxdo import following, following_import, helper_account


def snapshot(owner="fixture_owner", *, imported_at="2026-09-01T00:00:00Z", usernames=None):
    return {"version": 2, "complete": True, "owner": owner, "imported_at": imported_at,
            "usernames": ["fixture_actor"] if usernames is None else usernames}


def write_snapshot(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def private_following(tmp_path, monkeypatch):
    root = tmp_path / "following-private"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(following, "private_root", lambda: root)
    monkeypatch.setattr(helper_account, "private_root", lambda: root)
    return root


def bind_owner(root, *, imported_at="2026-09-01T00:00:00Z", usernames=None):
    path = write_snapshot(root / "following.json", snapshot(imported_at=imported_at, usernames=usernames))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    return path


def test_owner_v2_snapshot_is_normalized_and_persisted_with_its_bound_owner(private_following):
    root = private_following
    bind_owner(root)
    incoming = write_snapshot(root.parent / "incoming.json", snapshot(
        imported_at="2026-09-02T00:00:00Z", usernames=["FiXtUrE_Actor", "fixture_other"]))

    result = following.install(incoming)

    installed = following.load(root / "following.json")
    assert installed.owner == "fixture_owner"
    assert installed.usernames == frozenset({"fixture_actor", "fixture_other"})
    assert json.loads((root / "following.json").read_text())["version"] == 2
    assert result["count"] == 2 and result["added"] == 1 and result["removed"] == 0


@pytest.mark.parametrize("incoming", [
    snapshot(owner="fixture_other", imported_at="2026-09-02T00:00:00Z"),
    snapshot(owner="", imported_at="2026-09-02T00:00:00Z"),
    snapshot(imported_at="2026-08-31T23:59:59Z", usernames=["fixture_stale"]),
])
def test_telegram_text_import_cannot_rebind_owner_or_replace_newer_snapshot(private_following, incoming):
    root = private_following
    target = bind_owner(root)
    previous = target.read_bytes()

    result = following.import_text("webops", json.dumps(incoming))

    assert "未导入" in result or "不能覆盖" in result or "不匹配" in result
    assert target.read_bytes() == previous
    assert following.load(target).usernames == frozenset({"fixture_actor"})


def test_telegram_cannot_create_owner_binding_or_downgrade_bound_v2_snapshot(private_following):
    root = private_following
    payload = json.dumps(snapshot())

    assert following.import_text("webops", payload) == "请先在终端绑定本人的论坛账号"
    assert not (root / "following.json").exists()

    target = bind_owner(root)
    before = target.read_bytes()
    legacy = snapshot(imported_at="2026-09-02T00:00:00Z")
    legacy.pop("owner")
    legacy["version"] = 1

    assert following.import_text("webops", json.dumps(legacy)) == "请先在终端绑定本人的论坛账号"
    assert target.read_bytes() == before


def test_equal_timestamp_cannot_change_contents_and_identical_import_is_idempotent(private_following):
    root = private_following
    target = bind_owner(root)
    before = target.read_bytes()

    assert following.import_text("webops", json.dumps(snapshot())) == "关注名单未变化：1 人。"
    assert following.import_text("webops", json.dumps(snapshot(usernames=["fixture_other"]))) == "同一采集时间的名单内容不一致"
    assert target.read_bytes() == before


def test_text_command_preserves_json_and_profile_boundary(private_following, monkeypatch):
    root = private_following
    bind_owner(root)
    monkeypatch.setenv("PYLON_COMMAND_PROFILE", "fixture")
    payload = json.dumps(snapshot(imported_at="2026-09-02T00:00:00Z", usernames=["fixture_other"]))

    assert commands.handle("default", "/linuxdo_following_import " + payload) == "请将关注名单发送给负责 Linux.do 通知的机器人。"
    assert commands.handle("webops", "/linuxdo_following_import " + payload) == "关注名单已更新：1 人，新增 1 人，移除 1 人。"
    assert following.load(root / "following.json").usernames == frozenset({"fixture_other"})


def test_telegram_document_accepts_only_webops_owner_file_or_explicit_command():
    normal = {"document": {"file_name": following_import.FILE_NAME}}
    renamed = {"document": {"file_name": "fixture.json"}}
    commanded = renamed | {"caption": "/linuxdo_following_import fixture"}

    assert following_import.accepts("webops", normal)
    assert following_import.accepts("webops", commanded)
    assert not following_import.accepts("default", normal)
    assert not following_import.accepts("webops", renamed)
    assert not following_import.accepts("webops", {"caption": "/linuxdo_following_import"})


def test_telegram_document_imports_bounded_json_without_echoing_file_content(private_following, monkeypatch):
    root = private_following
    bind_owner(root)
    payload = json.dumps(snapshot(imported_at="2026-09-02T00:00:00Z",
                                  usernames=["fixture_actor", "fixture_other"])).encode()

    class Response:
        def raise_for_status(self):
            return None

        def iter_bytes(self):
            return [payload[:12], payload[12:]]

    class HTTP:
        @contextmanager
        def stream(self, method, url, *, follow_redirects):
            assert method == "GET"
            assert url == "https://api.telegram.org/file/botfixture-token/documents/fixture.json"
            assert follow_redirects is False
            yield Response()

    http = HTTP()
    monkeypatch.setattr("pylon.commands.authorized", lambda profile, message: True)
    monkeypatch.setattr(following_import, "profile_env", lambda profile: {"TELEGRAM_BOT_TOKEN": "fixture-token"})
    monkeypatch.setattr(following_import, "client", lambda: nullcontext(http))
    monkeypatch.setattr(following_import, "telegram_request", lambda *args, **kwargs: {
        "file_path": "documents/fixture.json"})
    message = {"document": {"file_name": following_import.FILE_NAME, "file_size": len(payload),
                             "file_id": "fixture-id"}}

    result = following_import.document("webops", message)

    assert result == "关注名单已更新：2 人，新增 1 人，移除 0 人。"
    assert following.load(root / "following.json").usernames == frozenset({"fixture_actor", "fixture_other"})
    assert "fixture_actor" not in result


@pytest.mark.parametrize("size", [None, True, -1, 0, following_import.LIMIT + 1])
def test_telegram_document_rejects_invalid_size_before_network_access(monkeypatch, size):
    monkeypatch.setattr("pylon.commands.authorized", lambda profile, message: True)
    client = Mock(side_effect=AssertionError("size must be checked before download"))
    monkeypatch.setattr(following_import, "client", client)
    message = {"document": {"file_name": following_import.FILE_NAME, "file_size": size,
                             "file_id": "fixture-id"}}

    assert following_import.document("webops", message) == "关注名单文件为空或过大，未导入。"
    client.assert_not_called()


def test_telegram_document_rejects_unauthorized_or_unsafe_download_path(private_following, monkeypatch):
    root = private_following
    bind_owner(root)
    monkeypatch.setattr("pylon.commands.authorized", lambda profile, message: False)
    client = Mock(side_effect=AssertionError("unauthorized input must not download"))
    monkeypatch.setattr(following_import, "client", client)
    message = {"document": {"file_name": following_import.FILE_NAME, "file_size": 64,
                             "file_id": "fixture-id"}}

    assert following_import.document("webops", message) == "未识别到本人发送的关注名单文件。"
    client.assert_not_called()

    monkeypatch.setattr("pylon.commands.authorized", lambda profile, message: True)
    monkeypatch.setattr(following_import, "profile_env", lambda profile: {"TELEGRAM_BOT_TOKEN": "fixture-token"})
    monkeypatch.setattr(following_import, "client", lambda: nullcontext(object()))
    monkeypatch.setattr(following_import, "telegram_request", lambda *args, **kwargs: {
        "file_path": "../fixture.json"})

    assert following_import.document("webops", message) == "关注名单下载地址无效，未导入。"
    assert following.load(root / "following.json").usernames == frozenset({"fixture_actor"})


def test_bot_queues_only_authorized_following_documents_and_processes_each_once(tmp_path, monkeypatch):
    box = Outbox(tmp_path / "bot-import.sqlite3")
    monkeypatch.setattr(bot, "authorized", lambda profile, message: message.get("fixture_owner") is True)
    document = Mock(return_value="关注名单未变化：1 人。")
    monkeypatch.setattr(following_import, "document", document)
    process = Mock(side_effect=AssertionError("document import must not spawn a command process"))
    monkeypatch.setattr(bot.subprocess, "run", process)
    owner_message = {"message_id": 101, "fixture_owner": True,
                     "document": {"file_name": following_import.FILE_NAME, "file_size": 100,
                                  "file_id": "fixture-id"}}
    updates = [
        {"update_id": 301, "message": owner_message},
        {"update_id": 302, "message": owner_message | {"message_id": 102, "fixture_owner": False}},
    ]

    bot.store_updates(box, "webops", updates)
    bot.store_updates(box, "webops", updates)
    assert bot.command_once(box)
    assert not bot.command_once(box)

    document.assert_called_once_with("webops", owner_message)
    process.assert_not_called()
    with box.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT update_id,kind,status FROM updates")] == [
            (301, "following_document", "done"),
        ]
        replies = [json.loads(row[0])["text"] for row in db.execute("SELECT payload FROM events")]
    assert replies == ["关注名单未变化：1 人。"]
