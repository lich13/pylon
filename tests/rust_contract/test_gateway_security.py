"""Focused security contracts for the Rust gateway RPC and broker boundary.

Offline cases use isolated fake Telegram fixtures. The production peer check
starts the listener without ``--offline`` and disables every fixture bot.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import signal
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time

import pytest

from test_gateway_contract import (
    BIN,
    PUBLIC_PORT,
    ROOT,
    FakeTelegram,
    Gateway as OfflineGateway,
    assert_rpc_rejected,
    open_context,
    rpc,
    rpc_value,
    seed_home,
    wait_for,
)


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory(prefix="pylon-rust-security-") as directory:
        yield Path(directory)


@pytest.fixture
def ordinary_peer_workdir():
    with tempfile.TemporaryDirectory(prefix="pylon-uds-peer-", dir="/tmp") as directory:
        path = Path(directory)
        if os.geteuid() == 0:
            path.chmod(0o755)
        yield path


def _seed_security_home(home: Path, telegram: FakeTelegram):
    return seed_home(
        home,
        telegram,
        [("bot_one", "101"), ("bot_two", "202"), ("bot_peer", "101")],
        [
            {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
            {"id": "beta", "topic": "topic_beta", "job_id": "job_beta"},
        ],
        {"topic_alpha": "bot_one", "topic_beta": "bot_two"},
    )


def _insert_receipt(
    home: Path,
    event_id: str,
    *,
    profile: str,
    module: str,
    receipt,
    callback_data: str,
):
    payload = {
        "text": "isolated callback fixture",
        "reply_markup": {
            "inline_keyboard": [[{"text": "fixture button", "callback_data": callback_data}]]
        },
    }
    now = time.time()
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        database.execute(
            """
            INSERT INTO events(
                id,profile,job_id,kind,payload,status,attempts,next_attempt,
                created_at,updated_at,receipt,module
            ) VALUES(?,?,?,?,?,'delivered',1,?,?,?,?,?)
            """,
            (
                event_id,
                profile,
                "job_alpha",
                "text",
                json.dumps(payload),
                now,
                now,
                now,
                json.dumps(receipt),
                module,
            ),
        )
        database.commit()


def _callback_query(message_id: int, data: str, *, sender="101", chat="101", chat_type="private", is_bot=False):
    return {
        "id": f"fixture-query-{message_id}",
        "from": {"id": int(sender), "is_bot": is_bot},
        "message": {
            "message_id": message_id,
            "chat": {"id": int(chat), "type": chat_type},
        },
        "data": data,
    }


def _auth_callback(home: Path, context: str, query: dict, bot: str | None = None):
    params = {"query": query}
    if bot is not None:
        params["bot"] = bot
    return rpc(home, "auth.callback", params, context)


def test_auth_callback_requires_private_owner_and_exact_original_keyboard_data(
    workdir, telegram
):
    home = workdir / "home"
    _seed_security_home(home, telegram)

    with OfflineGateway(home, telegram):
        _insert_receipt(
            home,
            "callback-object",
            profile="bot_one",
            module="alpha",
            receipt={"message_id": 4101, "chat_id": 101},
            callback_data="alpha|confirm",
        )
        context = open_context(home, "alpha", "job_alpha")["token"]

        accepted = _auth_callback(
            home, context, _callback_query(4101, "alpha|confirm")
        )
        assert accepted["ok"] is True and accepted["value"] is True

        altered = _auth_callback(
            home, context, _callback_query(4101, "alpha|confirm-extra")
        )
        assert altered["ok"] is True and altered["value"] is False

        non_owner_sender = _auth_callback(
            home,
            context,
            _callback_query(4101, "alpha|confirm", sender="909"),
        )
        assert non_owner_sender["ok"] is True and non_owner_sender["value"] is False

        group_chat = _auth_callback(
            home,
            context,
            _callback_query(4101, "alpha|confirm", chat="-101", chat_type="group"),
        )
        assert group_chat["ok"] is True and group_chat["value"] is False

        other_private_chat = _auth_callback(
            home,
            context,
            _callback_query(4101, "alpha|confirm", chat="202"),
        )
        assert other_private_chat["ok"] is True and other_private_chat["value"] is False

        bot_sender = _auth_callback(
            home,
            context,
            _callback_query(4101, "alpha|confirm", is_bot=True),
        )
        assert bot_sender["ok"] is True and bot_sender["value"] is False


def test_callback_lookup_accepts_receipt_object_or_array_last_entry_only(
    workdir, telegram
):
    home = workdir / "home"
    _seed_security_home(home, telegram)

    with OfflineGateway(home, telegram):
        _insert_receipt(
            home,
            "callback-object",
            profile="bot_one",
            module="alpha",
            receipt={"message_id": 4201, "chat_id": 101},
            callback_data="alpha|object",
        )
        _insert_receipt(
            home,
            "callback-array",
            profile="bot_one",
            module="alpha",
            receipt=[
                {"message_id": 4301, "chat_id": 101},
                {"message_id": 4302, "chat_id": 101},
            ],
            callback_data="alpha|last",
        )
        _insert_receipt(
            home,
            "callback-wrong-chat",
            profile="bot_one",
            module="alpha",
            receipt={"message_id": 4401, "chat_id": 303},
            callback_data="alpha|wrong-chat",
        )
        context = open_context(home, "alpha", "job_alpha")["token"]

        object_query = _callback_query(4201, "alpha|object")
        assert _auth_callback(home, context, object_query)["value"] is True

        last_query = _callback_query(4302, "alpha|last")
        assert _auth_callback(home, context, last_query)["value"] is True

        earlier_array_item = _callback_query(4301, "alpha|last")
        assert _auth_callback(home, context, earlier_array_item)["value"] is False

        mismatched_receipt_chat = _callback_query(4401, "alpha|wrong-chat")
        assert _auth_callback(home, context, mismatched_receipt_chat)["value"] is False

        object_lookup = rpc_value(home, "delivery.lookup", {"message_id": 4201}, context)
        last_lookup = rpc_value(home, "delivery.lookup", {"message_id": 4302}, context)
        earlier_lookup = rpc_value(home, "delivery.lookup", {"message_id": 4301}, context)
        assert [row["id"] for row in object_lookup] == ["callback-object"]
        assert [row["id"] for row in last_lookup] == ["callback-array"]
        assert earlier_lookup == []


def test_callback_and_lookup_are_scoped_to_the_context_bot_owner(workdir, telegram):
    home = workdir / "home"
    _seed_security_home(home, telegram)

    with OfflineGateway(home, telegram):
        _insert_receipt(
            home,
            "callback-bot-one",
            profile="bot_one",
            module="alpha",
            receipt={"message_id": 4501, "chat_id": 101},
            callback_data="alpha|owned",
        )
        alpha = open_context(home, "alpha", "job_alpha")["token"]
        beta = open_context(home, "beta", "job_beta")["token"]
        query = _callback_query(4501, "alpha|owned")

        same_owner_other_bot = _auth_callback(home, alpha, query, bot="bot_peer")
        assert same_owner_other_bot["ok"] is True
        assert same_owner_other_bot["value"] is False

        different_owner_bot = _auth_callback(home, alpha, query, bot="bot_two")
        assert_rpc_rejected(different_owner_bot)

        beta_lookup = rpc_value(home, "delivery.lookup", {"message_id": 4501}, beta)
        assert beta_lookup == []


def test_auth_sign_is_stable_and_isolated_by_bot_credential(workdir, telegram):
    home = workdir / "home"
    tokens = _seed_security_home(home, telegram)

    with OfflineGateway(home, telegram):
        context = open_context(home, "alpha", "job_alpha")["token"]
        params = {"action": "open", "target": "market"}
        first = rpc_value(home, "auth.sign", params, context)
        repeated = rpc_value(home, "auth.sign", params, context)
        peer_bot = rpc_value(
            home, "auth.sign", {**params, "bot": "bot_peer"}, context
        )

        expected_legacy = hmac.new(
            tokens["bot_one"].encode(),
            b"bot_one|open|market",
            hashlib.sha256,
        ).hexdigest()[:12]
        expected_peer_legacy = hmac.new(
            tokens["bot_peer"].encode(),
            b"bot_peer|open|market",
            hashlib.sha256,
        ).hexdigest()[:12]
        assert first == repeated
        assert re.fullmatch(r"[0-9a-f]{32}", first)
        assert re.fullmatch(r"[0-9a-f]{32}", peer_bot)
        assert first[:12] == expected_legacy
        assert peer_bot[:12] == expected_peer_legacy
        assert peer_bot != first

        different_owner = rpc(home, "auth.sign", {**params, "bot": "bot_two"}, context)
        assert_rpc_rejected(different_owner)


class _ProductionGateway(OfflineGateway):
    """Start the real listener mode while all fixture bots remain disabled."""

    def start(self):
        assert BIN.is_file(), f"Rust gateway binary not found at {BIN}; build it before these tests"
        if not self._port_checked:
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", PUBLIC_PORT))
                except OSError as error:
                    pytest.fail(
                        f"loopback port {PUBLIC_PORT} is occupied; gateway needs a test-port option: {error}"
                    )
            self._port_checked = True
        self.proc = subprocess.Popen(
            [str(BIN), "--home", str(self.home), "gateway"],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for(self._ready, timeout=8.0)
        return self


_RAW_CONTEXT_OPEN = r"""
import http.client
import socket
import sys

class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=3)
        self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)

connection = UnixHTTPConnection(sys.argv[1])
connection.request(
    "POST", "/rpc", body=sys.argv[2],
    headers={"Content-Type": "application/json"},
)
response = connection.getresponse()
print(response.read().decode("utf-8"))
connection.close()
"""


def _ordinary_identity() -> tuple[int, int]:
    if os.geteuid() != 0:
        return os.getuid(), os.getgid()
    for account in sorted(pwd.getpwall(), key=lambda item: item.pw_uid):
        if 0 < account.pw_uid <= 65535 and 0 < account.pw_gid <= 65535:
            return account.pw_uid, account.pw_gid
    pytest.skip("no ordinary UID/GID is available for the peer-credential fixture")


def _run_context_open_as(uid: int, gid: int, home: Path, method="context.open") -> dict:
    request = json.dumps(
        {
            "api_version": 2,
            "method": method,
            "params": {"module": "alpha", "subject": "job:job_alpha"},
            "context": "",
        },
        separators=(",", ":"),
    )

    def drop_privileges():
        os.setgroups([])
        os.setgid(gid)
        os.setuid(uid)

    result = subprocess.run(
        [sys.executable, "-c", _RAW_CONTEXT_OPEN, str(home / "run/gateway.sock"), request],
        cwd=home,
        env={"PATH": os.defpath, "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=5,
        preexec_fn=drop_privileges if os.geteuid() == 0 else None,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


@pytest.mark.parametrize("method", ["context.open", "context.retire_service"])
def test_production_context_open_rejects_an_ordinary_unix_peer(
    ordinary_peer_workdir, telegram, method
):
    home = ordinary_peer_workdir / "home"
    _seed_security_home(home, telegram)
    settings_path = home / "config/bots.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for bot in settings["bots"].values():
        bot["enabled"] = False
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    # On a root test run, let the ordinary peer connect through a fixture-only
    # shared group so this checks peer credentials after the filesystem ACL.
    ordinary_peer_workdir.chmod(0o755)
    uid, gid = _ordinary_identity()
    assert uid != 0

    with _ProductionGateway(home, telegram):
        socket_path = home / "run/gateway.sock"
        if os.geteuid() == 0:
            home.chmod(0o755)
            socket_path.parent.chmod(0o755)
            os.chown(socket_path, -1, gid)
            os.chmod(socket_path, 0o660)

        with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
            before = database.execute("SELECT COUNT(*) FROM contexts").fetchone()[0]

        response = _run_context_open_as(uid, gid, home, method)
        assert_rpc_rejected(response)

        with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
            after = database.execute("SELECT COUNT(*) FROM contexts").fetchone()[0]
        assert after == before


def _root_trusted_path(path: Path) -> bool:
    try:
        current = path.resolve(strict=True)
    except OSError:
        return False
    while True:
        metadata = current.stat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
            return False
        parent = current.parent
        if parent == current:
            return True
        current = parent


@pytest.fixture
def root_broker_workdir():
    if sys.platform != "linux":
        pytest.skip("Linux root broker permissions are not covered on macOS")
    if os.geteuid() != 0:
        pytest.skip("the broker privilege path requires a Linux root test process")

    base = Path("/run")
    if not _root_trusted_path(base):
        pytest.skip("/run is not a root-owned, non-writable trusted fixture parent")
    try:
        temporary = tempfile.TemporaryDirectory(prefix="pylon-rust-security-", dir=base)
    except OSError as error:
        pytest.skip(f"cannot create an isolated trusted fixture under /run: {error}")
    try:
        root = Path(temporary.name)
        root.chmod(0o755)
        yield root
    finally:
        temporary.cleanup()


def _unix_socket_accepts(path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.25)
            connection.connect(str(path))
        return True
    except OSError:
        return False


@contextmanager
def _running_root_broker(home: Path):
    process = subprocess.Popen(
        [str(BIN), "--home", str(home), "broker"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def ready():
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f"broker exited during startup: {stderr or stdout}")
        return _unix_socket_accepts(home / "run/broker.sock")

    try:
        wait_for(ready, timeout=6.0)
        yield process
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
                raise AssertionError("fixture broker did not stop after SIGTERM")
        process.communicate()


def test_root_broker_uses_trusted_helper_and_module_uid_cannot_write_protected_files(
    root_broker_workdir, telegram
):
    home = root_broker_workdir / "home"
    secret = home / "gateway/secrets/bot-bot_one"
    helper = home / "gateway/fixture-helper.sh"
    proof = home / "gateway/fixture-helper-ran"
    seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    for directory in (
        home,
        home / "config",
        home / "gateway",
        home / "gateway/secrets",
        home / "modules",
        home / "modules/alpha",
        home / "profiles",
        home / "profiles/default",
    ):
        directory.chmod(0o755)
    for source in (
        home / "modules/alpha/module.json",
        home / "modules/alpha/handler.sh",
        home / "modules/alpha/job.sh",
    ):
        source.chmod(0o644)
    uid, gid = _ordinary_identity()
    assert uid != 0 and gid != 0

    settings_path = home / "config/bots.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["runtime_users"] = {"alpha": {"uid": uid, "gid": gid}}
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    helper.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "cat >/dev/null\n"
        f"printf 'broker-ran\\n' >> {shlex.quote(str(proof))}\n"
        "printf '{\"ran\":true}\\n'\n",
        encoding="utf-8",
    )
    helper.chmod(0o755)
    proof.write_bytes(b"")
    proof.chmod(0o600)
    helper_before = helper.read_bytes()
    secret_before = secret.read_bytes()
    assert helper.stat().st_uid == 0 and secret.stat().st_uid == 0
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600

    manifest_path = home / "modules/alpha/module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = ["fixture.write"]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    script = (
        "#!/bin/sh\n"
        "set -eu\n"
        f"test \"$(id -u)\" = {uid}\n"
        f"if (printf 'tamper\\n' >> {shlex.quote(str(helper))}) >/dev/null 2>&1; then exit 71; fi\n"
        f"if (printf 'tamper\\n' >> {shlex.quote(str(secret))}) >/dev/null 2>&1; then exit 72; fi\n"
        "exit 0\n"
    )
    (home / "modules/alpha/job.sh").write_text(script, encoding="utf-8")
    (home / "modules/alpha/job.sh").chmod(0o644)

    grant = {
        "fixture.write": {
            "modules": ["alpha"],
            "origins": ["job:job_alpha"],
            "argv": ["/bin/sh", str(helper)],
            "trusted_paths": [str(helper)],
            "timeout": 5,
        }
    }
    capability_path = home / "config/capabilities.json"
    capability_path.write_text(json.dumps(grant, indent=2), encoding="utf-8")
    capability_path.chmod(0o600)

    with OfflineGateway(home, telegram):
        (home / "run").chmod(0o755)
        with _running_root_broker(home):
            context = open_context(home, "alpha", "job_alpha")["token"]
            privileged = rpc_value(
                home,
                "capability.invoke",
                {"capability": "fixture.write", "params": {}},
                context,
            )
            assert privileged == {"ran": True}
            assert proof.read_bytes() == b"broker-ran\n"

            offline_env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
            }
            offline_env["PYLON_TEST_TELEGRAM"] = telegram.url
            execution = subprocess.run(
                [str(BIN), "--home", str(home), "--offline", "execute", "job_alpha"],
                cwd=ROOT,
                env=offline_env,
                capture_output=True,
                text=True,
                timeout=8,
            )
            assert execution.returncode == 0, execution.stderr or execution.stdout
            assert json.loads(execution.stdout.strip()) == {
                "task": "job_alpha",
                "status": "ok",
            }

    assert helper.read_bytes() == helper_before
    assert secret.read_bytes() == secret_before


@pytest.mark.parametrize(
    "behavior",
    ["invalid_json", "nonzero_exit"],
    ids=["invalid-json", "nonzero-exit"],
)
def test_root_broker_redacts_helper_failures_and_runs_helper_once(
    root_broker_workdir, telegram, behavior
):
    home = root_broker_workdir / "home"
    helper = home / "gateway/fixture-failing-helper.sh"
    proof = home / "gateway/fixture-failing-helper-ran"
    sensitive_output = "fixture-root-helper-sensitive-output"
    seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )

    helper_output = (
        f'{{"detail":"{sensitive_output}",'
        if behavior == "invalid_json"
        else sensitive_output
    )
    helper_script = (
        "#!/bin/sh\n"
        "set -eu\n"
        "cat >/dev/null\n"
        f"printf 'broker-ran\\n' >> {shlex.quote(str(proof))}\n"
        f"printf '%s\\n' {shlex.quote(helper_output)}\n"
    )
    if behavior == "nonzero_exit":
        helper_script += "exit 23\n"
    helper.write_text(helper_script, encoding="utf-8")
    helper.chmod(0o755)
    proof.write_bytes(b"")
    proof.chmod(0o600)

    manifest_path = home / "modules/alpha/module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = ["fixture.write"]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    grant = {
        "fixture.write": {
            "modules": ["alpha"],
            "origins": ["job:job_alpha"],
            "argv": ["/bin/sh", str(helper)],
            "trusted_paths": [str(helper)],
            "timeout": 5,
        }
    }
    capability_path = home / "config/capabilities.json"
    capability_path.write_text(json.dumps(grant, indent=2), encoding="utf-8")
    capability_path.chmod(0o600)

    with OfflineGateway(home, telegram):
        (home / "run").chmod(0o755)
        with _running_root_broker(home):
            context = open_context(home, "alpha", "job_alpha")["token"]
            response = rpc(
                home,
                "capability.invoke",
                {"capability": "fixture.write", "params": {}},
                context,
            )

            assert response.get("ok") is False, response
            assert response.get("error_code") == "rejected", response
            assert response.get("retryable") is False, response
            expected_error = (
                "operation result uncertain; inspect retained state"
                if behavior == "invalid_json"
                else "request rejected"
            )
            assert response.get("error") == expected_error, response
            assert sensitive_output not in json.dumps(response)
            assert proof.read_text(encoding="utf-8").splitlines() == ["broker-ran"]
