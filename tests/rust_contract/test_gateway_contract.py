"""Targeted black-box contracts for the Rust gateway CLI and RPC surface."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import select
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from contextlib import contextmanager

import pytest


ROOT = Path(__file__).resolve().parents[2]
BIN = Path(os.environ.get("PYLON_GATEWAY_BIN", ROOT / "target/debug/pylon")).resolve()
PUBLIC_PORT = 8793


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: Path, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def rpc(home: Path, method: str, params=None, context: str = "") -> dict:
    body = json.dumps(
        {
            "api_version": 2,
            "method": method,
            "params": params or {},
            "context": context,
        },
        separators=(",", ":"),
    )
    connection = _UnixHTTPConnection(home / "run/gateway.sock", timeout=2.0)
    try:
        connection.request(
            "POST",
            "/rpc",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        assert response.status == 200
        return json.loads(response.read())
    finally:
        connection.close()


def rpc_value(home: Path, method: str, params=None, context: str = ""):
    response = rpc(home, method, params, context)
    assert response.get("ok") is True, response.get("error", "gateway RPC failed")
    return response["value"]


def assert_rpc_rejected(response: dict) -> None:
    assert response.get("ok") is False, response
    assert response.get("error") == "request rejected", response
    assert response.get("error_code") == "rejected", response
    assert response.get("retryable") is False, response


def assert_rpc_unavailable(response: dict) -> None:
    assert response.get("ok") is False, response
    assert response.get("error") == "service temporarily unavailable", response
    assert response.get("error_code") == "unavailable", response
    assert response.get("retryable") is True, response


def wait_for(predicate, timeout: float = 6.0, interval: float = 0.05):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, sqlite3.Error, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(interval)
    raise AssertionError(f"condition was not met before timeout; last error: {last_error!r}")


def _fake_token(bot_id: str, owner: str) -> str:
    suffix = hashlib.sha256(bot_id.encode()).hexdigest()
    return f"{owner}:fixture_{suffix}"


def _module_manifest(
    module_id: str,
    topic: str,
    *,
    command_name: str | None = None,
    job_id: str | None = None,
    lock: str = "",
    model: bool = False,
) -> dict:
    manifest = {
        "api_version": 2,
        "id": module_id,
        "handler": ["bash", "handler.sh"],
        "workspace": "default",
        "commands": [],
        "jobs": {},
        "services": {},
    }
    if command_name:
        manifest["commands"].append(
            {
                "name": command_name,
                "description": f"{module_id} fixture command",
                "topic": topic,
                "menu": True,
                "order": 1,
            }
        )
    if job_id:
        manifest["jobs"][job_id] = {
            "name": f"{module_id} fixture job",
            "entry": ["bash", "job.sh"],
            "workspace": "default",
            "topic": topic,
            "schedule": "* * * * *",
            "timeout": 30,
            "lock": lock,
            "model": model,
        }
    return manifest


def write_module(
    module_dir: Path,
    module_id: str,
    topic: str,
    *,
    command_name: str | None = None,
    job_id: str | None = None,
    lock: str = "",
    model: bool = False,
) -> Path:
    module_dir.mkdir(parents=True, exist_ok=True)
    manifest = _module_manifest(
        module_id,
        topic,
        command_name=command_name,
        job_id=job_id,
        lock=lock,
        model=model,
    )
    (module_dir / "module.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    handler = "#!/bin/sh\ncat >/dev/null\nprintf '{\"api_version\":2,\"value\":null}\\n'\n"
    if job_id:
        handler = (
            '#!/bin/bash\ninput="$(cat)"\n'
            'if [[ "$input" =~ \\"action\\"[[:space:]]*:[[:space:]]*\\"runtime[.]complete\\" ]]; then\n'
            "  printf '{\"api_version\":2,\"value\":{\"status\":\"ok\"}}\\n'\n"
            "else\n"
            "  printf '{\"api_version\":2,\"value\":null}\\n'\n"
            "fi\n"
        )
    (module_dir / "handler.sh").write_text(handler, encoding="utf-8")
    (module_dir / "job.sh").write_text(
        "#!/bin/sh\nset -eu\n"
        "mkdir -p \"$PYLON_HOME/data\"\n"
        "printf 'started\\n' >> \"$PYLON_HOME/data/model-runs\"\n"
        "sleep 1\n",
        encoding="utf-8",
    )
    (module_dir / "service.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    return module_dir


def write_registry(modules_dir: Path, module_ids: list[str]) -> bytes:
    path = modules_dir / "registry.json"
    temporary = modules_dir / ".registry-contract.tmp"
    payload = json.dumps(
        {
            "api_version": 2,
            "modules": {module_id: module_id for module_id in module_ids},
        },
        indent=2,
    ).encode()
    temporary.write_bytes(payload)
    os.replace(temporary, path)
    return payload


def seed_home(
    home: Path,
    telegram: "FakeTelegram",
    bots: list[tuple[str, str]],
    modules: list[dict],
    routes: dict[str, str],
) -> dict[str, str]:
    (home / "config").mkdir(parents=True)
    (home / "gateway/secrets").mkdir(parents=True)
    (home / "modules").mkdir(parents=True)
    profile = home / "profiles/default"
    profile.mkdir(parents=True)
    (profile / ".env").write_text("", encoding="utf-8")

    tokens = {}
    settings_bots = {}
    for bot_id, owner in bots:
        token = _fake_token(bot_id, owner)
        tokens[bot_id] = token
        credential = f"bot-{bot_id}"
        secret = home / "gateway/secrets" / credential
        secret.write_text(token, encoding="utf-8")
        secret.chmod(0o600)
        settings_bots[bot_id] = {
            "owner": owner,
            "credential": credential,
            "username": bot_id,
            "enabled": True,
            "menus": [],
        }

    for spec in modules:
        write_module(
            home / "modules" / spec["id"],
            spec["id"],
            spec["topic"],
            command_name=spec.get("command_name"),
            job_id=spec.get("job_id"),
            lock=spec.get("lock", ""),
            model=spec.get("model", False),
        )
    write_registry(home / "modules", [spec["id"] for spec in modules])

    config = {
        "bots": settings_bots,
        "routes": routes,
        "workspaces": {
            "default": {"root": "profiles/default", "env": "profiles/default/.env"}
        },
    }
    (home / "config/bots.json").write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tokens


class FakeTelegram:
    def __init__(self):
        self._lock = threading.RLock()
        self._calls = []
        self._commands = {}
        self._buttons = {}
        self._updates = {}
        self._message_id = 5000
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw or b"{}")
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                token = parts[0][3:] if parts and parts[0].startswith("bot") else ""
                method = parts[1] if len(parts) > 1 else ""
                with owner._lock:
                    owner._calls.append((token, method, body))
                    result = owner._result(token, method, body)
                output = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                self.wfile.write(output)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _result(self, token: str, method: str, body: dict):
        if method == "getUpdates":
            offset = int(body.get("offset", 0))
            return [item for item in self._updates.get(token, []) if item["update_id"] >= offset]
        if method == "getMyCommands":
            key = json.dumps(
                [body.get("scope", {"type": "default"}), body.get("language_code", "")],
                sort_keys=True,
                separators=(",", ":"),
            )
            return self._commands.get((token, key), [])
        if method == "setMyCommands":
            key = json.dumps(
                [body.get("scope", {"type": "default"}), body.get("language_code", "")],
                sort_keys=True,
                separators=(",", ":"),
            )
            self._commands[(token, key)] = body.get("commands", [])
            return True
        if method == "getChatMenuButton":
            return self._buttons.get((token, str(body.get("chat_id", "default"))), {"type": "commands"})
        if method == "setChatMenuButton":
            self._buttons[(token, str(body.get("chat_id", "default")))] = body.get(
                "menu_button", {"type": "commands"}
            )
            return True
        if method == "sendMessage":
            self._message_id += 1
            chat_id = body.get("chat_id")
            if isinstance(chat_id, str) and chat_id.isdigit():
                chat_id = int(chat_id)
            return {"message_id": self._message_id, "chat": {"id": chat_id}}
        if method == "sendDocument":
            self._message_id += 1
            chat_id = body.get("chat_id")
            if isinstance(chat_id, str) and chat_id.isdigit():
                chat_id = int(chat_id)
            return {"message_id": self._message_id, "chat": {"id": chat_id}, "document": {}}
        if method == "getMe":
            return {"id": 1, "username": "fixture_bot"}
        return True

    def set_updates(self, token: str, updates: list[dict]):
        with self._lock:
            self._updates[token] = list(updates)

    def count(self, token: str, method: str) -> int:
        with self._lock:
            return sum(1 for call_token, call_method, _ in self._calls if call_token == token and call_method == method)

    def commands(self, token: str, scope: dict, language: str) -> list[dict]:
        key = json.dumps([scope, language], sort_keys=True, separators=(",", ":"))
        with self._lock:
            return list(self._commands.get((token, key), []))

    def sent_text(self, text: str) -> bool:
        with self._lock:
            return any(
                method == "sendMessage" and body.get("text") == text
                for _, method, body in self._calls
            )

    def send_count(self, text: str) -> int:
        with self._lock:
            return sum(
                1 for _, method, body in self._calls
                if method == "sendMessage" and body.get("text") == text
            )

    def method_counts(self) -> dict[str, int]:
        with self._lock:
            counts = {}
            for _, method, _ in self._calls:
                counts[method] = counts.get(method, 0) + 1
            return counts

    def command_summary(self) -> list[tuple[dict, str, list[str]]]:
        with self._lock:
            result = []
            for (_, key), commands in self._commands.items():
                scope, language = json.loads(key)
                result.append(
                    (scope, language, sorted(item.get("command", "") for item in commands))
                )
            return result

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


@pytest.fixture
def workdir():
    import tempfile

    with tempfile.TemporaryDirectory(prefix="pylon-rust-contract-") as directory:
        yield Path(directory)


class Gateway:
    def __init__(self, home: Path, telegram: FakeTelegram):
        self.home = home
        self.telegram = telegram
        self.env = os.environ.copy()
        self.env["PYLON_TEST_TELEGRAM"] = telegram.url
        self.proc: subprocess.Popen | None = None
        self._port_checked = False

    def start(self):
        assert BIN.is_file(), f"Rust gateway binary not found at {BIN}; build it before these tests"
        if not self._port_checked:
            with socket.socket() as probe:
                try:
                    probe.bind(("127.0.0.1", PUBLIC_PORT))
                except OSError as error:
                    pytest.fail(f"loopback port {PUBLIC_PORT} is occupied; gateway needs a test-port option: {error}")
            self._port_checked = True
        self.proc = subprocess.Popen(
            [str(BIN), "--home", str(self.home), "--offline", "gateway"],
            cwd=ROOT,
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for(self._ready, timeout=8.0)
        return self

    def _ready(self):
        assert self.proc is not None
        if self.proc.poll() is not None:
            stdout, stderr = self.proc.communicate()
            raise AssertionError(f"gateway exited during startup: {stderr or stdout}")
        result = rpc(self.home, "health")
        return result if result.get("ok") is True else None

    def stop(self):
        if self.proc is None or self.proc.poll() is not None:
            return
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3)
            raise AssertionError("test gateway did not stop after SIGTERM")
        self.proc.communicate()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()


def open_context(home: Path, module: str, job: str) -> dict:
    return rpc_value(home, "context.open", {"module": module, "subject": f"job:{job}"})


def event_row(home: Path, event_id: str) -> dict | None:
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        database.row_factory = sqlite3.Row
        row = database.execute(
            "SELECT id,profile,status,attempts,receipt,error,module FROM events WHERE id=?",
            (event_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["receipt"] = json.loads(result["receipt"]) if result["receipt"] else None
        return result


def event_status(home: Path, event_id: str, status: str):
    current = event_row(home, event_id)
    return current if current and current["status"] == status else None


def run_cli(home: Path, *args: str, input_text: str | None = None, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BIN), "--home", str(home), *args],
        cwd=ROOT,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=8,
    )


@contextmanager
def running_gateway(home: Path, telegram: FakeTelegram):
    gateway = Gateway(home, telegram)
    gateway.start()
    try:
        yield gateway
    finally:
        gateway.stop()


def test_delivery_receipts_are_isolated_between_modules(workdir, telegram):
    home = workdir / "home"
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101"), ("bot_two", "202")],
        [
            {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
            {"id": "beta", "topic": "topic_beta", "job_id": "job_beta"},
        ],
        {"topic_alpha": "bot_one", "topic_beta": "bot_two"},
    )

    with running_gateway(home, telegram):
        alpha = open_context(home, "alpha", "job_alpha")["token"]
        beta = open_context(home, "beta", "job_beta")["token"]
        message = "module-alpha receipt fixture"
        submitted = rpc_value(
            home,
            "delivery.submit",
            {
                "id": "alpha-event-1",
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "payload": {"text": message},
            },
            alpha,
        )
        assert submitted["module"] == "alpha"
        assert submitted["profile"] == "bot_one"
        delivered = wait_for(lambda: event_status(home, "alpha-event-1", "delivered"))
        assert delivered["receipt"] == [{"message_id": 5001, "chat_id": 101}]

        owned = rpc_value(home, "delivery.get", {"id": "alpha-event-1"}, alpha)
        assert owned["module"] == "alpha"
        assert owned["receipt"] == delivered["receipt"]
        rejected_get = rpc(home, "delivery.get", {"id": "alpha-event-1"}, beta)
        assert_rpc_rejected(rejected_get)
        rejected_duplicate = rpc(
            home,
            "delivery.submit",
            {
                "id": "alpha-event-1",
                "kind": "text",
                "job_id": "job_beta",
                "topic": "topic_beta",
                "payload": {"text": "other module cannot adopt this id"},
            },
            beta,
        )
        assert_rpc_rejected(rejected_duplicate)
        assert telegram.count(tokens["bot_one"], "sendMessage") == 1


def test_delivery_submit_and_cancel_are_idempotent_and_module_scoped(workdir, telegram):
    home = workdir / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101"), ("bot_two", "202")],
        [
            {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
            {"id": "beta", "topic": "topic_beta", "job_id": "job_beta"},
        ],
        {"topic_alpha": "bot_one", "topic_beta": "bot_two"},
    )
    params = {
        "id": "cancel-event-1",
        "kind": "text",
        "job_id": "job_alpha",
        "topic": "topic_alpha",
        "next_attempt": time.time() + 600,
        "payload": {"text": "cancelled event must not send"},
    }

    with running_gateway(home, telegram):
        alpha = open_context(home, "alpha", "job_alpha")["token"]
        beta = open_context(home, "beta", "job_beta")["token"]
        first = rpc_value(home, "delivery.submit", params, alpha)
        repeated = rpc_value(home, "delivery.submit", params, alpha)
        assert first["id"] == repeated["id"] == "cancel-event-1"

        changed = dict(params, payload={"text": "different content"})
        rejected = rpc(home, "delivery.submit", changed, alpha)
        assert_rpc_rejected(rejected)

        rejected_cancel = rpc(home, "delivery.cancel", {"id": "cancel-event-1"}, beta)
        assert_rpc_rejected(rejected_cancel)
        assert event_row(home, "cancel-event-1")["status"] == "pending"

        cancelled = rpc_value(home, "delivery.cancel", {"id": "cancel-event-1"}, alpha)
        repeated_cancel = rpc_value(home, "delivery.cancel", {"id": "cancel-event-1"}, alpha)
        assert cancelled["status"] == repeated_cancel["status"] == "suppressed"
        assert telegram.sent_text("cancelled event must not send") is False


def test_cancel_before_delayed_submit_creates_non_deliverable_tombstone(workdir, telegram):
    home = workdir / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101"), ("bot_two", "202")],
        [
            {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
            {"id": "beta", "topic": "topic_beta", "job_id": "job_beta"},
        ],
        {"topic_alpha": "bot_one", "topic_beta": "bot_two"},
    )

    with running_gateway(home, telegram):
        alpha = open_context(home, "alpha", "job_alpha")["token"]
        beta = open_context(home, "beta", "job_beta")["token"]
        cancellation = rpc_value(
            home,
            "delivery.cancel",
            {"id": "late-submit-event", "if_missing": True},
            alpha,
        )
        assert cancellation["status"] == "suppressed"
        assert event_row(home, "late-submit-event")["attempts"] == 0

        delayed = rpc_value(
            home,
            "delivery.submit",
            {
                "id": "late-submit-event",
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "payload": {"text": "cancelled before delayed submit"},
            },
            alpha,
        )
        assert delayed["status"] == "suppressed"
        assert delayed["module"] == "alpha"

        cross_module = rpc(
            home,
            "delivery.submit",
            {
                "id": "late-submit-event",
                "kind": "text",
                "job_id": "job_beta",
                "topic": "topic_beta",
                "payload": {"text": "another module cannot adopt tombstone"},
            },
            beta,
        )
        assert_rpc_rejected(cross_module)
        time.sleep(0.2)
        assert event_row(home, "late-submit-event")["status"] == "suppressed"
        assert event_row(home, "late-submit-event")["attempts"] == 0
        assert telegram.sent_text("cancelled before delayed submit") is False


def test_sending_event_recovers_as_uncertain_and_is_never_replayed(workdir, telegram):
    home = workdir / "home"
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    message = "restart uncertainty fixture"
    params = {
        "id": "restart-event-1",
        "kind": "text",
        "job_id": "job_alpha",
        "topic": "topic_alpha",
        "next_attempt": time.time() + 600,
        "payload": {"text": message},
    }

    gateway = Gateway(home, telegram)
    gateway.start()
    try:
        context = open_context(home, "alpha", "job_alpha")["token"]
        rpc_value(home, "delivery.submit", params, context)
        assert event_row(home, "restart-event-1")["status"] == "pending"
        assert telegram.sent_text(message) is False
        with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
            database.execute(
                "UPDATE events SET status='sending',attempts=1,receipt=?,updated_at=? WHERE id=?",
                (json.dumps([{"message_id": 77, "chat_id": 101}]), time.time(), "restart-event-1"),
            )
            database.commit()
        calls_before_restart = telegram.count(tokens["bot_one"], "getUpdates")
        gateway.stop()

        gateway.start()
        wait_for(lambda: telegram.count(tokens["bot_one"], "getUpdates") > calls_before_restart)
        recovered = wait_for(lambda: event_status(home, "restart-event-1", "uncertain"))
        assert recovered["receipt"] == [{"message_id": 77, "chat_id": 101}]
        current = rpc_value(home, "delivery.get", {"id": "restart-event-1"}, context)
        assert current["status"] == "uncertain"
        assert current["receipt"] == recovered["receipt"]
        time.sleep(0.7)
        assert telegram.send_count(message) == 0
        assert event_row(home, "restart-event-1")["status"] == "uncertain"
    finally:
        gateway.stop()


def test_owner_updates_and_bot_scoped_menus(workdir, telegram):
    home = workdir / "home"
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101"), ("bot_two", "202")],
        [
            {
                "id": "alpha",
                "topic": "topic_alpha",
                "command_name": "alpha_cmd",
                "job_id": "job_alpha",
            },
            {
                "id": "beta",
                "topic": "topic_beta",
                "command_name": "beta_cmd",
                "job_id": "job_beta",
            },
        ],
        {"topic_alpha": "bot_one", "topic_beta": "bot_two"},
    )
    now = int(time.time())
    telegram.set_updates(
        tokens["bot_one"],
        [
            {
                "update_id": 1,
                "message": {
                    "message_id": 11,
                    "date": now,
                    "chat": {"id": 101, "type": "private"},
                    "from": {"id": 909, "is_bot": False},
                    "text": "UNAUTHORIZED-SENDER",
                },
            },
            {
                "update_id": 2,
                "message": {
                    "message_id": 12,
                    "date": now,
                    "chat": {"id": -101, "type": "group"},
                    "from": {"id": 101, "is_bot": False},
                    "text": "GROUP-MESSAGE",
                },
            },
            {
                "update_id": 3,
                "message": {
                    "message_id": 13,
                    "date": now,
                    "chat": {"id": 101, "type": "private"},
                    "from": {"id": 101, "is_bot": False},
                    "text": "/help",
                },
            },
        ],
    )

    with running_gateway(home, telegram):
        scopes_by_bot = {
            "bot_one": [
                {"type": "default"},
                {"type": "all_private_chats"},
                {"type": "chat", "chat_id": "101"},
            ],
            "bot_two": [
                {"type": "default"},
                {"type": "all_private_chats"},
                {"type": "chat", "chat_id": "202"},
            ],
        }

        def missing_menus():
            missing = []
            for bot_id, command_name in (("bot_one", "alpha_cmd"), ("bot_two", "beta_cmd")):
                for scope in scopes_by_bot[bot_id]:
                    for language in ("", "zh", "en"):
                        commands = telegram.commands(tokens[bot_id], scope, language)
                        if command_name not in {item.get("command") for item in commands}:
                            missing.append((bot_id, scope, language))
            return missing

        try:
            wait_for(lambda: not missing_menus(), timeout=5.0)
        except AssertionError as error:
            raise AssertionError(
                f"{error}; missing={missing_menus()}; calls={telegram.method_counts()}; "
                f"menus={telegram.command_summary()}"
            ) from error

        def authorized_updates_ready():
            with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
                rows = database.execute(
                    "SELECT update_id,status FROM updates WHERE profile='bot_one' ORDER BY update_id"
                ).fetchall()
                offset = database.execute(
                    "SELECT value FROM kv WHERE key='offset:bot_one'"
                ).fetchone()
            return rows == [(3, "done")] and offset == ("4",)

        wait_for(authorized_updates_ready, timeout=5.0)
        for bot_id, expected_command, other_command in (
            ("bot_one", "alpha_cmd", "beta_cmd"),
            ("bot_two", "beta_cmd", "alpha_cmd"),
        ):
            for scope in scopes_by_bot[bot_id]:
                names = {
                    item["command"]
                    for item in telegram.commands(tokens[bot_id], scope, "zh")
                }
                assert expected_command in names and other_command not in names
        assert telegram.sent_text("UNAUTHORIZED-SENDER") is False
        assert telegram.sent_text("GROUP-MESSAGE") is False




def test_capability_rpc_requires_valid_context_and_fails_closed_without_broker(workdir, telegram):
    home = workdir / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )

    with running_gateway(home, telegram):
        invalid_context = rpc(
            home,
            "capability.invoke",
            {"capability": "fixture.write", "params": {}},
            "not-a-context-token",
        )
        assert_rpc_rejected(invalid_context)

        context = open_context(home, "alpha", "job_alpha")["token"]
        # No broker socket exists, so the request fails before the privileged
        # operation is received and remains safe to retry.
        assert not (home / "run/broker.sock").exists()
        denied = rpc(
            home,
            "capability.invoke",
            {"capability": "fixture.write", "params": {}},
            context,
        )
        assert_rpc_unavailable(denied)


def test_duplicate_bot_credentials_are_rejected_without_partial_registration(workdir, telegram):
    home = workdir / "home"
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [],
        {},
    )
    config_path = home / "config/bots.json"
    config_before = config_path.read_bytes()
    duplicate = run_cli(
        home,
        "bots",
        "add",
        "bot_two",
        "--owner",
        "202",
        "--username",
        "bot_two",
        input_text=tokens["bot_one"] + "\n",
    )
    assert duplicate.returncode != 0
    assert "duplicate bot credential" in duplicate.stderr
    assert tokens["bot_one"] not in duplicate.stdout + duplicate.stderr
    assert config_path.read_bytes() == config_before
    assert not (home / "gateway/secrets/bot-bot_two").exists()


def test_registry_conflict_check_does_not_rewrite_the_rejected_registry(workdir, telegram):
    home = workdir / "home"
    seed_home(home, telegram, [], [], {})
    modules = home / "conflicting-modules"
    modules.mkdir()
    for module_id in ("first", "second"):
        write_module(modules / module_id, module_id, "topic", command_name="same_command")
    registry = write_registry(modules, ["first", "second"])

    rejected = run_cli(home, "modules", "check", "--directory", str(modules))
    assert rejected.returncode != 0
    assert "duplicate or invalid command" in rejected.stderr
    assert (modules / "registry.json").read_bytes() == registry


def test_install_conflict_preserves_existing_registry(workdir, telegram):
    home = workdir / "home"
    seed_home(home, telegram, [], [], {})
    modules = home / "modules"
    write_module(
        modules / "installed",
        "installed",
        "topic_installed",
        command_name="shared_command",
    )
    registry = write_registry(modules, ["installed"])

    source = workdir / "candidate"
    write_module(
        source,
        "candidate",
        "topic_candidate",
        command_name="shared_command",
    )
    archive = workdir / "candidate.tar.gz"
    packed = run_cli(
        home,
        "modules",
        "pack",
        str(source),
        "--output",
        str(archive),
    )
    assert packed.returncode == 0, packed.stderr
    checksum = json.loads(packed.stdout)["sha256"]

    installed = run_cli(
        home,
        "modules",
        "install",
        str(archive),
        "--sha256",
        checksum,
    )
    assert installed.returncode != 0
    assert "duplicate or invalid command" in installed.stderr
    assert (modules / "registry.json").read_bytes() == registry
    assert not (modules / "candidate").exists()


def test_fourth_bot_and_module_reload_without_replacing_gateway_or_binary(workdir, telegram):
    home = workdir / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101"), ("bot_two", "202"), ("bot_three", "303")],
        [
            {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
            {"id": "beta", "topic": "topic_beta", "job_id": "job_beta"},
            {"id": "gamma", "topic": "topic_gamma", "job_id": "job_gamma"},
        ],
        {
            "topic_alpha": "bot_one",
            "topic_beta": "bot_two",
            "topic_gamma": "bot_three",
        },
    )
    binary_before = hashlib.sha256(BIN.read_bytes()).hexdigest()

    with running_gateway(home, telegram) as gateway:
        pid_before = gateway.proc.pid
        module_ids = ["alpha", "beta", "gamma", "fresh"]
        write_module(
            home / "modules/fresh",
            "fresh",
            "topic_fresh",
            command_name="fresh_cmd",
            job_id="job_fresh",
        )
        added = run_cli(
            home,
            "bots",
            "add",
            "bot_four",
            "--owner",
            "404",
            "--username",
            "bot_four",
            input_text=_fake_token("bot_four", "404") + "\n",
        )
        assert added.returncode == 0, added.stderr
        fourth_token = _fake_token("bot_four", "404")
        route = run_cli(home, "routes", "topic_fresh", "bot_four")
        assert route.returncode == 0, route.stderr
        write_registry(home / "modules", module_ids)
        reload_result = run_cli(home, "bots", "reload")
        assert reload_result.returncode == 0, reload_result.stderr

        def fresh_context():
            result = rpc(
                home,
                "context.open",
                {"module": "fresh", "subject": "job:job_fresh"},
            )
            return result["value"] if result.get("ok") is True else None

        live_context = wait_for(fresh_context, timeout=12.0)
        assert live_context["bot"] == "bot_four"
        try:
            wait_for(lambda: telegram.count(fourth_token, "getUpdates") > 0, timeout=2.0)
        except AssertionError as error:
            raise AssertionError(f"{error}; Telegram calls: {telegram.method_counts()}") from error

        scopes = [
            {"type": "default"},
            {"type": "all_private_chats"},
            {"type": "chat", "chat_id": "404"},
        ]
        wait_for(
            lambda: all(
                "fresh_cmd" in {
                    item.get("command")
                    for item in telegram.commands(fourth_token, scope, "zh")
                }
                for scope in scopes
            ),
            timeout=5.0,
        )

        catalog_hash = rpc_value(home, "health")["catalog"]
        write_module(
            home / "modules/intruder",
            "intruder",
            "topic_fresh",
            command_name="fresh_cmd",
            job_id="job_intruder",
        )
        invalid_registry = write_registry(
            home / "modules", module_ids + ["intruder"]
        )
        rejected = run_cli(home, "modules", "check", "--directory", str(home / "modules"))
        assert rejected.returncode != 0
        assert "duplicate or invalid command" in rejected.stderr
        assert (home / "modules/registry.json").read_bytes() == invalid_registry
        assert _wait_stderr_line(gateway.proc, "configuration rejected", timeout=8.0)

        assert rpc_value(home, "health")["catalog"] == catalog_hash
        assert rpc_value(
            home,
            "context.inspect",
            {},
            live_context["token"],
        )["module"] == "fresh"
        unavailable = rpc(
            home,
            "context.open",
            {"module": "intruder", "subject": "job:job_intruder"},
        )
        assert_rpc_rejected(unavailable)
        assert gateway.proc.pid == pid_before
        assert hashlib.sha256(BIN.read_bytes()).hexdigest() == binary_before


def _read_stderr_line(process: subprocess.Popen | None, fragment: str):
    if process is None or process.stderr is None:
        return None

    # select() observes the pipe fd, while TextIOWrapper.readline() can prefetch
    # several lines into its own buffer. Read from the fd directly so a buffered
    # line cannot become invisible to later select() calls.
    buffer = getattr(process, "_gateway_contract_stderr_buffer", None)
    if buffer is None:
        buffer = bytearray()
        process._gateway_contract_stderr_buffer = buffer

    def take_matching_line():
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                return None
            raw_line = bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
            line = raw_line.decode("utf-8", errors="replace")
            if fragment in line:
                return line

    line = take_matching_line()
    if line is not None:
        return line

    fd = process.stderr.fileno()
    ready, _, _ = select.select([fd], [], [], 0)
    if not ready:
        return None

    try:
        chunk = os.read(fd, 8192)
    except OSError:
        return None
    if chunk:
        buffer.extend(chunk)
        return take_matching_line()

    # A final diagnostic line may not end in a newline when the process exits.
    if buffer:
        line = bytes(buffer).decode("utf-8", errors="replace")
        buffer.clear()
        return line if fragment in line else None
    return None


def _wait_stderr_line(process: subprocess.Popen | None, fragment: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = _read_stderr_line(process, fragment)
        if line:
            return line
        time.sleep(0.05)
    return None


def test_model_job_lock_prevents_overlapping_runs(workdir, telegram):
    if os.geteuid() == 0:
        pytest.skip("the Rust runtime intentionally refuses to run business jobs as root")
    home = workdir / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [
            {
                "id": "modeler",
                "topic": "topic_modeler",
                "job_id": "model_job",
                "lock": "model_component",
                "model": True,
            }
        ],
        {"topic_modeler": "bot_one"},
    )

    with running_gateway(home, telegram):
        first_env = os.environ.copy()
        first_env["PYLON_TEST_TELEGRAM"] = telegram.url
        first_env["INVOCATION_ID"] = "contract-run-a"
        first = subprocess.Popen(
            [str(BIN), "--home", str(home), "--offline", "execute", "model_job"],
            cwd=ROOT,
            env=first_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            marker = home / "data/model-runs"
            wait_for(lambda: marker.is_file() and marker.read_text().count("started") == 1)

            second_env = os.environ.copy()
            second_env["PYLON_TEST_TELEGRAM"] = telegram.url
            second_env["INVOCATION_ID"] = "contract-run-b"
            second = subprocess.run(
                [str(BIN), "--home", str(home), "--offline", "execute", "model_job"],
                cwd=ROOT,
                env=second_env,
                capture_output=True,
                text=True,
                timeout=5,
            )
            assert second.returncode == 0, second.stderr
            assert marker.read_text().count("started") == 1
            stdout, stderr = first.communicate(timeout=6)
            assert first.returncode == 0, stderr or stdout
            assert json.loads(stdout.strip()) == {"task": "model_job", "status": "ok"}
            assert marker.read_text().count("started") == 1
            with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
                runs = database.execute(
                    "SELECT status FROM runs WHERE job_id='model_job'"
                ).fetchall()
            assert runs == [("ok",)]
        finally:
            if first.poll() is None:
                first.terminate()
                try:
                    first.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.wait(timeout=3)
