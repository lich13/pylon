"""Offline regression contracts for gateway reloads, delivery outcomes, and listeners."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time

import pytest

from test_gateway_contract import (
    BIN,
    FakeTelegram,
    ROOT,
    assert_rpc_rejected,
    event_row,
    event_status,
    open_context,
    rpc,
    rpc_value,
    seed_home,
    wait_for,
)
from test_runtime_reliability import JOB as RUNTIME_JOB, RuntimeCase, _held_quota


@pytest.fixture
def short_tmp_path():
    with tempfile.TemporaryDirectory(prefix="pylon-stab-", dir="/tmp") as directory:
        yield Path(directory)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _set_web_port(home: Path, port: int) -> None:
    path = home / "config/bots.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["web_port"] = port
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_settings(home: Path, settings: dict) -> None:
    (home / "config/bots.json").write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _tcp_connects(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


def _listener_lines(pid: int, port: int) -> list[str]:
    process = Path(f"/proc/{pid}")
    if process.is_dir():
        inodes = set()
        for descriptor in (process / "fd").iterdir():
            try:
                target = os.readlink(descriptor)
            except FileNotFoundError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
        listeners = []
        for name, family in (("tcp", socket.AF_INET), ("tcp6", socket.AF_INET6)):
            table = process / "net" / name
            if not table.exists():
                continue
            for line in table.read_text().splitlines()[1:]:
                fields = line.split()
                address, raw_port = fields[1].split(":")
                if fields[3] != "0A" or fields[9] not in inodes or int(raw_port, 16) != port:
                    continue
                raw = bytes.fromhex(address)
                raw = b"".join(raw[i:i+4][::-1] for i in range(0, len(raw), 4))
                listeners.append(f"{socket.inet_ntop(family, raw)}:{port}")
        return listeners
    if not shutil.which("lsof"):
        pytest.skip("listener ownership inspection requires /proc or lsof")
    result = subprocess.run(
        [
            "lsof",
            "-nP",
            "-a",
            "-p",
            str(pid),
            f"-iTCP:{port}",
            "-sTCP:LISTEN",
        ],
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.splitlines()[1:]


class _GatewayProcess:
    """Launch one isolated gateway without assuming the default web port is free."""

    def __init__(self, home: Path, telegram: "_LocalTelegram"):
        self.home = home
        self.telegram = telegram
        self.proc: subprocess.Popen | None = None
        self.env = os.environ.copy()
        self.env["PYLON_TEST_TELEGRAM"] = telegram.url

    def start(self):
        assert BIN.is_file(), f"Rust gateway binary not found at {BIN}; build it before these tests"
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


class _LocalTelegram:
    """Small loopback-only Telegram API fixture with optional HTML send failures."""

    def __init__(self, send_http_status: int | None = None):
        self._lock = threading.RLock()
        self._calls: list[tuple[str, str, dict]] = []
        self._commands: dict[tuple[str, str], list[dict]] = {}
        self._buttons: dict[tuple[str, str], dict] = {}
        self._message_id = 7000
        self._send_http_status = send_http_status
        self._closed = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                token = parts[0][3:] if parts and parts[0].startswith("bot") else ""
                method = parts[1] if len(parts) > 1 else ""
                with owner._lock:
                    owner._calls.append((token, method, request))
                    if method == "sendMessage" and owner._send_http_status is not None:
                        status = owner._send_http_status
                        content_type = "text/html; charset=utf-8"
                        output = b"<html><body>fixture upstream failure</body></html>"
                    else:
                        status = 200
                        content_type = "application/json"
                        output = json.dumps(
                            {"ok": True, "result": owner._result(token, method, request)}
                        ).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                self.wfile.write(output)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _result(self, token: str, method: str, request: dict):
        if method == "getUpdates":
            return []
        if method == "getMyCommands":
            key = json.dumps(
                [request.get("scope", {"type": "default"}), request.get("language_code", "")],
                sort_keys=True,
                separators=(",", ":"),
            )
            return self._commands.get((token, key), [])
        if method == "setMyCommands":
            key = json.dumps(
                [request.get("scope", {"type": "default"}), request.get("language_code", "")],
                sort_keys=True,
                separators=(",", ":"),
            )
            self._commands[(token, key)] = request.get("commands", [])
            return True
        if method == "getChatMenuButton":
            return self._buttons.get((token, str(request.get("chat_id", "default"))), {"type": "commands"})
        if method == "setChatMenuButton":
            self._buttons[(token, str(request.get("chat_id", "default")))] = request.get(
                "menu_button", {"type": "commands"}
            )
            return True
        if method == "sendMessage":
            self._message_id += 1
            chat_id = request.get("chat_id")
            if isinstance(chat_id, str) and chat_id.isdigit():
                chat_id = int(chat_id)
            return {"message_id": self._message_id, "chat": {"id": chat_id}}
        if method == "sendDocument":
            self._message_id += 1
            chat_id = request.get("chat_id")
            if isinstance(chat_id, str) and chat_id.isdigit():
                chat_id = int(chat_id)
            return {
                "message_id": self._message_id,
                "chat": {"id": chat_id},
                "document": {},
            }
        if method == "getMe":
            return {"id": 1, "username": "fixture_bot"}
        return True

    def count(self, token: str, method: str) -> int:
        with self._lock:
            return sum(
                1 for call_token, call_method, _ in self._calls
                if call_token == token and call_method == method
            )

    def send_count(self, text: str) -> int:
        with self._lock:
            return sum(
                1 for _, method, request in self._calls
                if method == "sendMessage" and request.get("text") == text
            )

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _seed_one_bot(home: Path, telegram: _LocalTelegram) -> dict[str, str]:
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    _set_web_port(home, _free_loopback_port())
    return tokens


def _delivery_params(event_id: str, text: str, *, created_at: float | None = None) -> dict:
    params = {
        "id": event_id,
        "kind": "text",
        "job_id": "job_alpha",
        "topic": "topic_alpha",
        "payload": {"text": text},
    }
    if created_at is not None:
        params["created_at"] = created_at
    return params


def _recovery_notices(home: Path) -> list[dict]:
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute(
            "SELECT id,profile,status,payload,created_at FROM events "
            "WHERE module='gateway' AND job_id='system' ORDER BY profile"
        ).fetchall()
    result = [dict(row) for row in rows]
    for row in result:
        row["payload"] = json.loads(row["payload"])
    return result


@pytest.mark.parametrize("http_status", [502, 503])
def test_html_upstream_failure_is_uncertain_and_never_replayed(short_tmp_path, http_status):
    telegram = _LocalTelegram(send_http_status=http_status)
    try:
        home = short_tmp_path / "home"
        tokens = _seed_one_bot(home, telegram)
        with _GatewayProcess(home, telegram):
            context = open_context(home, "alpha", "job_alpha")["token"]
            event_id = f"html-{http_status}-event"
            message = f"html upstream failure fixture {http_status}"
            rpc_value(home, "delivery.submit", _delivery_params(event_id, message), context)

            uncertain = wait_for(lambda: event_status(home, event_id, "uncertain"))
            assert uncertain["attempts"] == 1
            assert telegram.count(tokens["bot_one"], "sendMessage") == 1
            time.sleep(0.8)
            assert event_row(home, event_id)["status"] == "uncertain"
            assert event_row(home, event_id)["attempts"] == 1
            assert telegram.count(tokens["bot_one"], "sendMessage") == 1
    finally:
        telegram.close()


def test_connect_failure_leaves_delivery_pending(short_tmp_path):
    telegram = _LocalTelegram()
    home = short_tmp_path / "home"
    tokens = _seed_one_bot(home, telegram)
    try:
        with _GatewayProcess(home, telegram):
            context = open_context(home, "alpha", "job_alpha")["token"]
            telegram.close()
            event_id = "connect-failure-pending"
            rpc_value(
                home,
                "delivery.submit",
                _delivery_params(event_id, "connect failure stays pending"),
                context,
            )

            pending = wait_for(
                lambda: (
                    row
                    if (row := event_row(home, event_id))
                    and row["status"] == "pending"
                    and row["attempts"] >= 1
                    else None
                ),
                timeout=8.0,
            )
            assert pending["attempts"] == 1
            assert pending["status"] == "pending"
            assert telegram.count(tokens["bot_one"], "sendMessage") == 0
    finally:
        telegram.close()


def test_removed_bot_old_context_submission_is_a_controlled_error_after_reload(short_tmp_path):
    telegram = _LocalTelegram()
    try:
        home = short_tmp_path / "home"
        tokens = seed_home(
            home,
            telegram,
            [("bot_one", "101"), ("bot_two", "202")],
            [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
            {"topic_alpha": "bot_one"},
        )
        _set_web_port(home, _free_loopback_port())
        with _GatewayProcess(home, telegram):
            context = open_context(home, "alpha", "job_alpha")["token"]
            settings_path = home / "config/bots.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["bots"].pop("bot_one")
            settings["routes"]["topic_alpha"] = "bot_two"
            _write_settings(home, settings)

            assert rpc_value(home, "admin.reload") == {"ok": True}
            rejected = rpc(
                home,
                "delivery.submit",
                _delivery_params("removed-bot-context-event", "must not be queued"),
                context,
            )
            assert_rpc_rejected(rejected)
            assert rpc_value(home, "health")["api_version"] == 2
            assert event_row(home, "removed-bot-context-event") is None
            assert telegram.count(tokens["bot_one"], "sendMessage") == 0
    finally:
        telegram.close()


def test_recovery_notice_ttl_coalesces_per_bot_without_expiring_business_events(short_tmp_path):
    telegram = _LocalTelegram()
    home = short_tmp_path / "home"
    tokens = seed_home(
        home,
        telegram,
        [("bot_one", "101"), ("bot_two", "202")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    _set_web_port(home, _free_loopback_port())
    try:
        # Initialize the database schema, then simulate interrupted bot updates.
        with _GatewayProcess(home, telegram):
            pass

        database_path = home / "gateway/state.sqlite3"
        with sqlite3.connect(database_path, timeout=3) as database:
            database.executemany(
                "INSERT INTO updates(profile,update_id,payload,status) VALUES(?,?,?,'running')",
                [
                    ("bot_one", 11, json.dumps({"fixture": "interrupted one"})),
                    ("bot_one", 12, json.dumps({"fixture": "interrupted two"})),
                    ("bot_two", 21, json.dumps({"fixture": "interrupted three"})),
                ],
            )
            database.commit()

        freeze = home / "run/freeze"
        freeze.touch()
        gateway = _GatewayProcess(home, telegram)
        with gateway:
            notices = wait_for(
                lambda: rows if len(rows := _recovery_notices(home)) == 2 else None,
                timeout=8.0,
            )
            by_bot = {row["profile"]: row for row in notices}
            assert set(by_bot) == {"bot_one", "bot_two"}
            assert sum(row["profile"] == "bot_one" for row in notices) == 1
            for row in notices:
                expires_at = row["payload"].get("expires_at")
                assert isinstance(expires_at, (int, float)), row
                assert 590 <= expires_at - row["created_at"] <= 610, row

            expired = by_bot["bot_one"]
            expired["payload"]["expires_at"] = time.time() - 1
            fresh = by_bot["bot_two"]
            with sqlite3.connect(database_path, timeout=3) as database:
                database.execute(
                    "UPDATE events SET payload=? WHERE id=?",
                    (json.dumps(expired["payload"]), expired["id"]),
                )
                database.commit()

            context = open_context(home, "alpha", "job_alpha")["token"]
            business_text = "old business event has no implicit TTL"
            business_id = "old-business-event"
            rpc_value(
                home,
                "delivery.submit",
                _delivery_params(
                    business_id,
                    business_text,
                    created_at=time.time() - 24 * 60 * 60,
                ),
                context,
            )
            with sqlite3.connect(database_path, timeout=3) as database:
                raw_business = database.execute(
                    "SELECT status,payload,created_at FROM events WHERE id=?",
                    (business_id,),
                ).fetchone()
            assert raw_business is not None
            assert raw_business[0] == "pending"
            assert "expires_at" not in json.loads(raw_business[1])
            assert raw_business[2] < time.time() - 23 * 60 * 60
            assert fresh["payload"]["expires_at"] > time.time()

            freeze.unlink()
            wait_for(
                lambda: (
                    event_status(home, expired["id"], "suppressed")
                    and event_status(home, fresh["id"], "delivered")
                    and event_status(home, business_id, "delivered")
                ),
                timeout=8.0,
            )
            assert event_row(home, expired["id"])["status"] == "suppressed"
            assert event_row(home, fresh["id"])["status"] == "delivered"
            assert event_row(home, business_id)["status"] == "delivered"
            assert telegram.send_count(business_text + "\n") == 1
    finally:
        telegram.close()


def test_missing_web_port_defaults_to_loopback_8793(short_tmp_path):
    if _tcp_connects(8793):
        pytest.skip("default loopback web port 8793 is already in use")
    telegram = _LocalTelegram()
    try:
        home = short_tmp_path / "home"
        seed_home(
            home,
            telegram,
            [("bot_one", "101")],
            [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
            {"topic_alpha": "bot_one"},
        )
        settings = json.loads((home / "config/bots.json").read_text(encoding="utf-8"))
        assert "web_port" not in settings

        with _GatewayProcess(home, telegram) as gateway:
            assert gateway.proc is not None
            wait_for(lambda: _tcp_connects(8793), timeout=3.0)
            listeners = _listener_lines(gateway.proc.pid, 8793)
            assert listeners, "gateway must own the default web listener"
            assert all("127.0.0.1:8793" in line for line in listeners), listeners
    finally:
        telegram.close()


@pytest.mark.parametrize("replacement", [0, "different-valid-port"])
def test_web_port_reload_rejection_preserves_old_snapshot_and_listener(short_tmp_path, replacement):
    telegram = _LocalTelegram()
    try:
        home = short_tmp_path / "home"
        seed_home(
            home,
            telegram,
            [("bot_one", "101"), ("bot_two", "202")],
            [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
            {"topic_alpha": "bot_one"},
        )
        old_port = _free_loopback_port()
        new_port = _free_loopback_port()
        while new_port == old_port:
            new_port = _free_loopback_port()
        _set_web_port(home, old_port)

        with _GatewayProcess(home, telegram) as gateway:
            assert gateway.proc is not None
            wait_for(lambda: _tcp_connects(old_port), timeout=3.0)
            old_catalog = rpc_value(home, "health")["catalog"]

            settings_path = home / "config/bots.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            settings["routes"]["topic_alpha"] = "bot_two"
            settings["web_port"] = new_port if replacement == "different-valid-port" else replacement
            _write_settings(home, settings)

            rejected = rpc(home, "admin.reload")
            assert_rpc_rejected(rejected)
            assert rpc_value(home, "health")["catalog"] == old_catalog
            context = open_context(home, "alpha", "job_alpha")
            assert context["bot"] == "bot_one"
            wait_for(lambda: _tcp_connects(old_port), timeout=3.0)
            if replacement == "different-valid-port":
                assert not _tcp_connects(new_port)
            listeners = _listener_lines(gateway.proc.pid, old_port)
            assert listeners
            assert all(f"127.0.0.1:{old_port}" in line for line in listeners), listeners
    finally:
        telegram.close()


def test_health_remains_available_during_sqlite_busy_then_delivery_succeeds(short_tmp_path):
    telegram = _LocalTelegram()
    try:
        home = short_tmp_path / "home"
        tokens = _seed_one_bot(home, telegram)
        with _GatewayProcess(home, telegram):
            context = open_context(home, "alpha", "job_alpha")["token"]
            database = sqlite3.connect(
                home / "gateway/state.sqlite3", timeout=0.1, isolation_level=None
            )
            try:
                database.execute("BEGIN IMMEDIATE")
                started = time.monotonic()
                health = rpc_value(home, "health")
                assert health["api_version"] == 2
                assert time.monotonic() - started < 1.5
            finally:
                database.rollback()
                database.close()

            event_id = "sqlite-busy-released-event"
            message = "sqlite busy released fixture delivery"
            rpc_value(
                home,
                "delivery.submit",
                _delivery_params(event_id, message),
                context,
            )
            delivered = wait_for(
                lambda: event_status(home, event_id, "delivered"), timeout=8.0
            )
            assert delivered["status"] == "delivered"
            assert telegram.count(tokens["bot_one"], "sendMessage") == 1
            assert telegram.send_count(message + "\n") == 1
    finally:
        telegram.close()


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="offline runtime fixtures execute business processes as the current non-root user",
)
def test_two_executor_processes_dispatch_business_request_once(short_tmp_path):
    telegram = FakeTelegram()
    case = RuntimeCase(short_tmp_path / "runtime-home", telegram)
    try:
        case.write_job(
            'while [ ! -e "$PYLON_HOME/data/release-business" ]; do sleep 0.05; done\n'
        )
        case.gateway.start()
        first_executor = case.start_executor()
        first_socket = case.home / "run/executor.sock"
        isolated_socket = case.home / "run/executor-first.sock"
        first_socket.rename(isolated_socket)
        second_executor = case.start_executor()
        assert first_executor.poll() is None
        assert second_executor.poll() is None

        with _held_quota(case, model=False):
            queued = case.execute("two-executor-once")
            assert queued.returncode == 0, queued.stderr or queued.stdout
            assert json.loads(queued.stdout)["status"] == "queued"
            assert len(case.requests_in("queued")) == 1
            assert len(case.requests()) == 1
            assert case.count("business-count") == 0

        wait_for(
            lambda: len(case.requests_in("running")) == 1
            and case.count("business-count") == 1,
            timeout=8.0,
        )

        # Keep the request running long enough for both live dispatchers to contend.
        time.sleep(0.3)
        assert len(case.requests_in("running")) == 1
        assert len(case.requests()) == 1
        assert case.count("business-count") == 1

        (case.data / "release-business").touch()
        wait_for(lambda: len(case.requests_in("completed")) == 1, timeout=10.0)
        assert len(case.requests()) == 1
        assert len(case.runs()) == 1
        assert case.count("business-count") == case.count("completion-count") == 1
    finally:
        (case.data / "release-business").touch(exist_ok=True)
        case.close()
        telegram.close()


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="offline runtime fixtures execute business processes as the current non-root user",
)
def test_timeout_retains_only_bounded_redacted_process_tail(short_tmp_path):
    telegram = FakeTelegram()
    case = RuntimeCase(short_tmp_path / "runtime-home", telegram)
    fixture_secret = "fixture-runtime-output-secret"
    try:
        case.configure_job(timeout=1)
        case.write_job(
            f'printf "%s\\n" "{fixture_secret}"\n'
            "sleep 30\n"
        )
        case.gateway.start()

        result = case.execute("timeout-tail-artifact", timeout=8.0)
        assert result.returncode == 1, result.stderr or result.stdout
        assert json.loads(result.stdout) == {"task": RUNTIME_JOB, "status": "uncertain"}
        assert len(case.runs()) == 1
        assert case.runs()[0]["status"] == "uncertain"

        artifact_path = case.home / "supervisor/process-last-alpha.json"
        wait_for(lambda: artifact_path.is_file(), timeout=3.0)
        artifact_text = artifact_path.read_text(encoding="utf-8")
        assert fixture_secret not in artifact_text
        artifact = json.loads(artifact_text)
        for stream_name in ("stdout", "stderr"):
            stream = artifact[stream_name]
            assert stream["captured_bytes"] <= 65536
            assert stream["fingerprint"]
            assert stream["tail"] == "[untrusted output redacted]"
    finally:
        case.close()
        telegram.close()
