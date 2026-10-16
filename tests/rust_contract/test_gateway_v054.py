"""Offline black-box regressions for v0.5.4 gateway concurrency and lifecycle fixes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time

import pytest

from test_gateway_contract import (
    BIN,
    ROOT,
    _UnixHTTPConnection,
    assert_rpc_unavailable,
    _fake_token,
    event_row,
    event_status,
    open_context,
    rpc_value,
    seed_home,
    wait_for,
)
from test_runtime_reliability import JOB, TOPIC, RuntimeCase


class _LargeBacklogHTTPServer(ThreadingHTTPServer):
    request_queue_size = 128


@pytest.fixture
def isolated_root():
    with tempfile.TemporaryDirectory(prefix="pylon-v054-", dir="/tmp") as directory:
        yield Path(directory)


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _set_random_web_port(home: Path) -> int:
    path = home / "config/bots.json"
    settings = json.loads(path.read_text(encoding="utf-8"))
    settings["web_port"] = _free_loopback_port()
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings["web_port"]


def _seed_home(
    home: Path,
    telegram: "_LocalTelegram",
    bots: list[tuple[str, str]],
    modules: list[dict],
    routes: dict[str, str],
) -> dict[str, str]:
    tokens = seed_home(home, telegram, bots, modules, routes)
    _set_random_web_port(home)
    return tokens


def _rpc(
    home: Path,
    method: str,
    params=None,
    context: str = "",
    *,
    timeout: float = 3.0,
) -> dict:
    body = json.dumps(
        {
            "api_version": 2,
            "method": method,
            "params": params or {},
            "context": context,
        },
        separators=(",", ":"),
    )
    connection = _UnixHTTPConnection(home / "run/gateway.sock", timeout=timeout)
    try:
        connection.request(
            "POST",
            "/rpc",
            body=body,
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        payload = response.read()
        assert response.status == 200, payload.decode("utf-8", errors="replace")
        return json.loads(payload)
    finally:
        connection.close()


def _rpc_value(
    home: Path,
    method: str,
    params=None,
    context: str = "",
    *,
    timeout: float = 3.0,
):
    response = _rpc(home, method, params, context, timeout=timeout)
    assert response.get("ok") is True, response
    return response["value"]


class _GatewayProcess:
    """Start an isolated gateway whose web port was selected for this test."""

    def __init__(self, home: Path, telegram: "_LocalTelegram"):
        self.home = home
        self.telegram = telegram
        self.env = os.environ.copy()
        self.env["PYLON_TEST_TELEGRAM"] = telegram.url
        self.proc: subprocess.Popen | None = None

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
        try:
            wait_for(self._ready, timeout=8.0)
        except BaseException:
            self.stop()
            raise
        return self

    def _ready(self):
        assert self.proc is not None
        if self.proc.poll() is not None:
            stdout, stderr = self.proc.communicate()
            raise AssertionError(f"gateway exited during startup: {stderr or stdout}")
        result = _rpc(self.home, "health", timeout=0.35)
        return result if result.get("ok") is True else None

    def stop(self, timeout: float = 8.0) -> float:
        if self.proc is None:
            return 0.0
        if self.proc.poll() is not None:
            self.proc.communicate()
            return 0.0
        started = time.monotonic()
        self.proc.send_signal(signal.SIGTERM)
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=3.0)
            stdout, stderr = self.proc.communicate()
            raise AssertionError(
                f"gateway did not stop after SIGTERM within {timeout}s: {stderr or stdout}"
            )
        elapsed = time.monotonic() - started
        self.proc.communicate()
        return elapsed

    def __enter__(self):
        return self.start()

    def __exit__(self, *_exc):
        self.stop()


class _LocalTelegram:
    """Loopback-only Telegram fixture with controllable slow and malformed replies."""

    def __init__(
        self,
        *,
        slow_method: str | None = None,
        empty_send_result: bool = False,
        block_send_message_response: bool = False,
        block_getupdates_at_or_after: int | None = None,
    ):
        self._lock = threading.RLock()
        self._calls: list[tuple[str, str, dict]] = []
        self._commands: dict[tuple[str, str], list[dict]] = {}
        self._buttons: dict[tuple[str, str], dict] = {}
        self._updates: dict[str, list[dict]] = {}
        self._message_id = 7000
        self._slow_method = slow_method
        self._empty_send_result = empty_send_result
        self._block_send_message_response = block_send_message_response
        self._block_getupdates_at_or_after = block_getupdates_at_or_after
        self._slow_release = threading.Event()
        self.send_message_entered = threading.Event()
        self.send_message_response_sent = threading.Event()
        self._send_message_release = threading.Event()
        if not block_send_message_response:
            self._send_message_release.set()
        self.longpoll_entered = threading.Event()
        self._longpoll_release = threading.Event()
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

                if method == owner._slow_method:
                    owner._slow_release.wait(timeout=30.0)
                if method == "sendMessage":
                    owner.send_message_entered.set()
                    owner._send_message_release.wait(timeout=30.0)
                if method == "getUpdates":
                    offset = int(request.get("offset", 0))
                    threshold = owner._block_getupdates_at_or_after
                    if threshold is not None and offset >= threshold:
                        owner.longpoll_entered.set()
                        owner._longpoll_release.wait(timeout=30.0)

                with owner._lock:
                    result = owner._result(token, method, request)
                output = json.dumps({"ok": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                try:
                    self.wfile.write(output)
                    self.wfile.flush()
                except OSError:
                    pass
                finally:
                    if method == "sendMessage":
                        owner.send_message_response_sent.set()

        self.server = _LargeBacklogHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _result(self, token: str, method: str, request: dict):
        if method == "getUpdates":
            offset = int(request.get("offset", 0))
            return [item for item in self._updates.get(token, []) if item["update_id"] >= offset]
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
            return self._buttons.get(
                (token, str(request.get("chat_id", "default"))), {"type": "commands"}
            )
        if method == "setChatMenuButton":
            self._buttons[(token, str(request.get("chat_id", "default")))] = request.get(
                "menu_button", {"type": "commands"}
            )
            return True
        if method == "sendMessage":
            if self._empty_send_result:
                return {}
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
        if method == "getChat":
            return {"id": request.get("chat_id")}
        return True

    def set_updates(self, token: str, updates: list[dict]) -> None:
        with self._lock:
            self._updates[token] = list(updates)

    def method_count(self, method: str, *, text: str | None = None) -> int:
        with self._lock:
            return sum(
                1
                for _, call_method, request in self._calls
                if call_method == method and (text is None or request.get("text") == text)
            )

    def count(self, token: str, method: str) -> int:
        with self._lock:
            return sum(
                1 for call_token, call_method, _ in self._calls
                if call_token == token and call_method == method
            )

    def send_fragment_count(self, token: str, fragment: str) -> int:
        with self._lock:
            return sum(
                1
                for call_token, method, request in self._calls
                if call_token == token
                and method == "sendMessage"
                and fragment in str(request.get("text", ""))
            )

    def release_slow_calls(self) -> None:
        self._slow_release.set()

    def release_longpoll(self) -> None:
        self._longpoll_release.set()

    def release_send_message_response(self) -> None:
        self._send_message_release.set()

    def close(self) -> None:
        self.release_slow_calls()
        self.release_longpoll()
        self.release_send_message_response()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


class _CountingHTTPUpstream:
    """A loopback-only upstream that records any forwarded request."""

    def __init__(self):
        owner = self
        self._lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _respond(self):
                with owner._lock:
                    owner.requests.append((self.command, self.path))
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", "2")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = _respond
            do_POST = _respond

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def _runtime_case(home: Path, telegram: _LocalTelegram) -> RuntimeCase:
    case = RuntimeCase(home, telegram)
    _set_random_web_port(home)
    case.gateway = _GatewayProcess(home, telegram)
    return case


def _loopback_accepts(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
            return True
    except OSError:
        return False


def _task_summary_database_state(home: Path) -> tuple[list[dict], dict]:
    with sqlite3.connect(home / "supervisor/task-state.sqlite3", timeout=2.0) as database:
        database.row_factory = sqlite3.Row
        rows = database.execute(
            "SELECT id,job_id,module,state,created_at,updated_at,execution_status,"
            "completion_status,owner FROM requests "
            "WHERE state!='completed' OR updated_at>? ORDER BY created_at DESC LIMIT 200",
            (time.time() - 86400.0,),
        ).fetchall()
        queued_count, oldest_queued_at = database.execute(
            "SELECT count(*),min(created_at) FROM requests WHERE state='queued'"
        ).fetchone()
    return [dict(row) for row in rows], {
        "queued_count": queued_count,
        "oldest_queued_at": oldest_queued_at,
    }


def _request_lock_is_free(path: Path) -> bool:
    with path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return True


def test_64_slow_clear_buttons_calls_do_not_starve_three_bot_deliveries(isolated_root):
    telegram = _LocalTelegram(slow_method="editMessageReplyMarkup")
    home = isolated_root / "home"
    bots = [("bot_one", "101"), ("bot_two", "202"), ("bot_three", "303")]
    modules = [
        {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
        {"id": "beta", "topic": "topic_beta", "job_id": "job_beta"},
        {"id": "gamma", "topic": "topic_gamma", "job_id": "job_gamma"},
    ]
    routes = {"topic_alpha": "bot_one", "topic_beta": "bot_two", "topic_gamma": "bot_three"}
    tokens = _seed_home(home, telegram, bots, modules, routes)
    gateway = _GatewayProcess(home, telegram)
    slow_pool = ThreadPoolExecutor(max_workers=64)
    slow_calls = []
    try:
        gateway.start()
        context = open_context(home, "alpha", "job_alpha")["token"]
        contexts = {
            module: open_context(home, module, job_id)["token"]
            for module, job_id in (
                ("alpha", "job_alpha"),
                ("beta", "job_beta"),
                ("gamma", "job_gamma"),
            )
        }

        deliveries = [
            ("alpha", "job_alpha", "topic_alpha", "clear-load-alpha"),
            ("beta", "job_beta", "topic_beta", "clear-load-beta"),
            ("gamma", "job_gamma", "topic_gamma", "clear-load-gamma"),
        ]
        due_later = time.time() + 3600.0
        for module, job_id, topic, event_id in deliveries:
            rpc_value(
                home,
                "delivery.submit",
                {
                    "id": event_id,
                    "kind": "text",
                    "job_id": job_id,
                    "topic": topic,
                    "next_attempt": due_later,
                    "payload": {"text": f"v054 delivery {event_id}"},
                },
                contexts[module],
            )

        now = time.time()
        with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3.0) as database:
            database.executemany(
                "INSERT INTO events(id,profile,job_id,kind,payload,status,attempts,next_attempt,"
                "created_at,updated_at,receipt,module) VALUES(?,?,?,?,?,'delivered',1,0,?,?,?,?)",
                [
                    (
                        f"clear-button-{index:02d}",
                        "bot_one",
                        "job_alpha",
                        "text",
                        json.dumps({"text": f"clear button fixture {index}"}),
                        now,
                        now,
                        json.dumps([{"message_id": 9000 + index, "chat_id": 101}]),
                        "alpha",
                    )
                    for index in range(64)
                ],
            )

        slow_calls = [
            slow_pool.submit(
                _rpc,
                home,
                "delivery.clear_buttons",
                {"id": f"clear-button-{index:02d}"},
                context,
                timeout=12.0,
            )
            for index in range(64)
        ]
        try:
            wait_for(lambda: telegram.method_count("editMessageReplyMarkup") == 64, timeout=5.0)
        except AssertionError as error:
            details = {
                "edit_calls": telegram.method_count("editMessageReplyMarkup"),
                "finished_rpc_count": sum(future.done() for future in slow_calls),
                "rpc_results": [
                    future.result()
                    for future in slow_calls
                    if future.done() and not future.exception()
                ][:4],
            }
            raise AssertionError(f"only some clear_buttons RPCs reached Telegram: {details}") from error

        # The gateway admits 64 regular RPCs. A 65th regular request must fail
        # quickly while the separate health path remains available.
        rejected_id = "clear-load-over-capacity"
        started = time.monotonic()
        unavailable = _rpc(
            home,
            "delivery.submit",
            {
                "id": rejected_id,
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "payload": {"text": f"v054 delivery {rejected_id}"},
            },
            contexts["alpha"],
            timeout=2.0,
        )
        assert time.monotonic() - started < 1.5
        assert_rpc_unavailable(unavailable)
        with sqlite3.connect(home / "gateway/state.sqlite3", timeout=2.0) as database:
            assert database.execute("SELECT 1 FROM events WHERE id=?", (rejected_id,)).fetchone() is None

        started = time.monotonic()
        health = _rpc_value(home, "health", timeout=0.8)
        assert time.monotonic() - started < 1.0
        assert health["api_version"] == 2

        started = time.monotonic()
        with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3.0) as database:
            cursor = database.execute(
                "UPDATE events SET next_attempt=? WHERE id IN (?,?,?) AND status='pending'",
                (time.time(), *(item[3] for item in deliveries)),
            )
            assert cursor.rowcount == 3

        def all_delivered():
            rows = [event_status(home, event_id, "delivered") for _, _, _, event_id in deliveries]
            return rows if all(rows) else None

        completed = wait_for(all_delivered, timeout=max(0.0, 5.0 - (time.monotonic() - started)))
        elapsed = time.monotonic() - started
        assert elapsed < 5.0
        assert completed is not None
        for _, _, topic, event_id in deliveries:
            assert event_row(home, event_id)["status"] == "delivered"
            assert telegram.send_fragment_count(tokens[routes[topic]], event_id) == 1
    finally:
        telegram.release_slow_calls()
        slow_pool.shutdown(wait=True, cancel_futures=True)
        gateway.stop()
        telegram.close()

    responses = [future.result(timeout=10.0) for future in slow_calls]
    assert len(responses) == 64
    assert all(response.get("ok") is True for response in responses), responses


def test_partial_web_body_does_not_block_admin_reload_or_health(isolated_root):
    telegram = _LocalTelegram()
    upstream = _CountingHTTPUpstream()
    home = isolated_root / "home"
    _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha", "web": True}],
        {"topic_alpha": "bot_one"},
    )
    manifest_path = home / "modules/alpha/module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["web"]["market"]["upstream"] = upstream.url
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    gateway = _GatewayProcess(home, telegram)
    web_port = json.loads((home / "config/bots.json").read_text(encoding="utf-8"))["web_port"]
    partial_request = None
    try:
        gateway.start()
        wait_for(lambda: _loopback_accepts(web_port), timeout=3.0)
        partial_request = socket.create_connection(("127.0.0.1", web_port), timeout=2.0)
        partial_request.sendall(
            b"POST /pylon/market/slow?fixture=partial HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Length: 128\r\n"
            b"Connection: keep-alive\r\n\r\n"
            b"unfinished request body"
        )
        # The declared body is deliberately incomplete. Let the proxy handler
        # wait on the client body while reload and health contend for snapshot access.
        time.sleep(5.5)
        assert upstream.request_count == 0, "incomplete request body was forwarded upstream"

        failures = []
        values = {}
        for method in ("admin.reload", "health"):
            started = time.monotonic()
            try:
                values[method] = _rpc_value(home, method, timeout=2.0)
            except Exception as error:
                failures.append((method, repr(error)))
            elapsed = time.monotonic() - started
            if elapsed >= 2.0:
                failures.append((method, f"took {elapsed:.3f}s while the web body was incomplete"))
        assert not failures, failures
        assert values["admin.reload"] == {"ok": True}
        assert values["health"]["api_version"] == 2
    finally:
        if partial_request is not None:
            partial_request.close()
        gateway.stop()
        upstream.close()
        telegram.close()
    assert upstream.request_count == 0, "incomplete request body reached the loopback upstream"


def test_empty_success_payload_for_send_message_is_uncertain_and_not_replayed(isolated_root):
    telegram = _LocalTelegram(empty_send_result=True)
    home = isolated_root / "home"
    tokens = _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    gateway = _GatewayProcess(home, telegram)
    message = "empty successful Telegram result fixture"
    event_id = "empty-send-result"
    try:
        gateway.start()
        context = open_context(home, "alpha", "job_alpha")["token"]
        rpc_value(
            home,
            "delivery.submit",
            {
                "id": event_id,
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "payload": {"text": message},
            },
            context,
        )
        terminal = wait_for(
            lambda: row
            if (row := event_row(home, event_id))
            and row["status"] in {"delivered", "uncertain", "failed"}
            else None,
            timeout=5.0,
        )
        assert terminal["status"] == "uncertain", terminal
        assert terminal["attempts"] == 1
        assert telegram.send_fragment_count(tokens["bot_one"], "empty successful Telegram result") == 1

        previous_getupdates = telegram.count(tokens["bot_one"], "getUpdates")
        gateway.stop()
        gateway.start()
        wait_for(
            lambda: telegram.count(tokens["bot_one"], "getUpdates") > previous_getupdates,
            timeout=5.0,
        )
        time.sleep(1.2)
        current = event_row(home, event_id)
        assert current["status"] == "uncertain", current
        assert current["attempts"] == 1
        assert telegram.send_fragment_count(tokens["bot_one"], "empty successful Telegram result") == 1
    finally:
        gateway.stop()
        telegram.close()


def test_send_message_receipt_is_retried_across_sixty_second_gateway_database_lock(isolated_root):
    telegram = _LocalTelegram(
        block_send_message_response=True,
        block_getupdates_at_or_after=0,
    )
    home = isolated_root / "home"
    tokens = _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    gateway = _GatewayProcess(home, telegram)
    database_lock = None
    event_id = "receipt-held-sixty-seconds"
    message = "receipt persistence lock fixture"
    try:
        gateway.start()
        context = open_context(home, "alpha", "job_alpha")["token"]
        _rpc_value(
            home,
            "delivery.submit",
            {
                "id": event_id,
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "payload": {"text": message},
            },
            context,
        )
        assert telegram.send_message_entered.wait(timeout=5.0), "delivery did not reach sendMessage"

        database_lock = sqlite3.connect(
            home / "gateway/state.sqlite3", timeout=5.0, isolation_level=None
        )
        database_lock.execute("BEGIN IMMEDIATE")
        telegram.release_send_message_response()
        assert telegram.send_message_response_sent.wait(timeout=5.0), "fake Telegram did not return the receipt"
        lock_started = time.monotonic()

        started = time.monotonic()
        health = _rpc_value(home, "health", timeout=2.0)
        assert time.monotonic() - started < 2.0
        assert health["api_version"] == 2
        assert telegram.send_fragment_count(tokens["bot_one"], "receipt persistence lock fixture") == 1

        halfway = 30.0 - (time.monotonic() - lock_started)
        if halfway > 0:
            time.sleep(halfway)
        started = time.monotonic()
        health = _rpc_value(home, "health", timeout=2.0)
        assert time.monotonic() - started < 2.0
        assert health["api_version"] == 2
        assert telegram.send_fragment_count(tokens["bot_one"], "receipt persistence lock fixture") == 1

        remaining = 60.0 - (time.monotonic() - lock_started)
        if remaining > 0:
            time.sleep(remaining)
        database_lock.execute("ROLLBACK")
        database_lock.close()
        database_lock = None

        delivered = wait_for(
            lambda: event_status(home, event_id, "delivered"), timeout=10.0
        )
        assert delivered["attempts"] == 1
        assert delivered["receipt"] == [{"message_id": 7001, "chat_id": 101}]
        assert telegram.send_fragment_count(tokens["bot_one"], "receipt persistence lock fixture") == 1
    finally:
        telegram.release_send_message_response()
        if database_lock is not None:
            database_lock.execute("ROLLBACK")
            database_lock.close()
        gateway.stop()
        telegram.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="offline runtime fixtures execute business processes as a non-root user")
def test_execute_admission_survives_three_second_supervisor_write_lock(isolated_root):
    telegram = _LocalTelegram()
    case = _runtime_case(isolated_root / "home", telegram)
    blocker = None
    try:
        case.gateway.start()
        case.start_executor()
        database_path = case.home / "supervisor/task-state.sqlite3"
        wait_for(database_path.is_file, timeout=5.0)
        blocker = sqlite3.connect(database_path, timeout=5.0, isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        process = case.start_execute("supervisor-lock-three-seconds")
        time.sleep(3.0)
        blocker.execute("ROLLBACK")
        blocker.close()
        blocker = None

        stdout, stderr = process.communicate(timeout=10.0)
        assert process.returncode == 0, stderr or stdout
        response = json.loads(stdout)
        assert response["task"] == JOB
        assert response["status"] in {"queued", "ok"}, response
        completed = wait_for(lambda: case.requests_in("completed"), timeout=10.0)
        assert len(completed) == 1
        assert completed[0]["execution_status"] == "ok"
        assert case.count("business-count") == 1
    finally:
        if blocker is not None:
            blocker.execute("ROLLBACK")
            blocker.close()
        case.close()
        telegram.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="offline runtime fixtures execute business processes as a non-root user")
def test_successful_command_waits_out_two_second_gateway_database_lock(isolated_root):
    telegram = _LocalTelegram(block_getupdates_at_or_after=2)
    case = _runtime_case(isolated_root / "home", telegram)
    database_lock = None
    try:
        manifest_path = case.home / "modules/alpha/module.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["commands"] = [
            {
                "name": "alpha_cmd",
                "description": "isolated fixture command",
                "topic": TOPIC,
                "menu": True,
                "order": 1,
            }
        ]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        envelope = json.dumps({"api_version": 2, "value": None}, separators=(",", ":"))
        (case.home / "modules/alpha/handler.sh").write_text(
            "#!/bin/bash\nset -eu\n"
            'cat > "$PYLON_HOME/data/completion-input.json"\n'
            'printf "ready\\n" >> "$PYLON_HOME/data/completion-count"\n'
            'while [ ! -e "$PYLON_HOME/data/release-completion" ]; do sleep 0.02; done\n'
            f"printf '%s\\n' '{envelope}'\n"
            'printf "returned\\n" >> "$PYLON_HOME/data/handler-returned"\n',
            encoding="utf-8",
        )
        telegram.set_updates(
            _fake_token("bot_one", "101"),
            [
                {
                    "update_id": 1,
                    "message": {
                        "message_id": 11,
                        "date": int(time.time()),
                        "chat": {"id": 101, "type": "private"},
                        "from": {"id": 101, "is_bot": False},
                        "text": "/alpha_cmd",
                    },
                }
            ],
        )
        case.gateway.start()
        case.start_executor()
        assert wait_for(lambda: case.count("completion-count") == 1, timeout=5.0)
        assert telegram.longpoll_entered.wait(timeout=5.0), "poll lane did not settle after saving the command"

        state_path = case.home / "gateway/state.sqlite3"
        with sqlite3.connect(state_path, timeout=3.0) as database:
            status = database.execute(
                "SELECT status FROM updates WHERE profile='bot_one' AND update_id=1"
            ).fetchone()
        assert status == ("running",)

        database_lock = sqlite3.connect(state_path, timeout=5.0, isolation_level=None)
        database_lock.execute("BEGIN IMMEDIATE")
        held_at = time.monotonic()
        (case.data / "release-completion").touch()
        returned = case.data / "handler-returned"
        wait_for(returned.is_file, timeout=1.5)
        remaining = 2.0 - (time.monotonic() - held_at)
        if remaining > 0:
            time.sleep(remaining)
        database_lock.execute("ROLLBACK")
        database_lock.close()
        database_lock = None

        def command_done():
            with sqlite3.connect(state_path, timeout=3.0) as database:
                return database.execute(
                    "SELECT status FROM updates WHERE profile='bot_one' AND update_id=1"
                ).fetchone()

        wait_for(lambda: command_done() == ("done",), timeout=6.0)
        assert command_done() == ("done",)
        assert case.count("completion-count") == 1
    finally:
        (case.data / "release-completion").touch(exist_ok=True)
        if database_lock is not None:
            database_lock.execute("ROLLBACK")
            database_lock.close()
        telegram.release_longpoll()
        case.close()
        telegram.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="offline runtime fixtures execute business processes as a non-root user")
def test_executor_refreshes_summary_while_run_finish_rpc_is_blocked(isolated_root):
    telegram = _LocalTelegram()
    case = _runtime_case(isolated_root / "home", telegram)
    database_lock = None
    executor = None
    queued_id = "queued-during-blocked-report"
    try:
        case.gateway.start()
        completed = case.execute("summary-complete-fixture")
        assert completed.returncode == 0, completed.stderr or completed.stdout
        assert json.loads(completed.stdout)["status"] == "ok"
        completed_rows = case.requests_in("completed")
        assert len(completed_rows) == 1, completed_rows
        report_id = completed_rows[0]["id"]

        request_lock = case.home / "run" / f"request-{hashlib.sha256(report_id.encode()).hexdigest()}.lock"
        assert request_lock.is_file(), "completed execute did not create its request lock"
        assert _request_lock_is_free(request_lock), "execute left a request lock behind"

        token = open_context(case.home, "alpha", "job_alpha")["token"]
        report = {
            "id": report_id,
            "status": "ok",
            "execution_status": "ok",
            "completion_status": "ok",
            "notification_status": "unknown",
            "detail": "",
        }
        supervisor_path = case.home / "supervisor/task-state.sqlite3"
        with sqlite3.connect(supervisor_path, timeout=3.0) as database:
            cursor = database.execute(
                "UPDATE requests SET state='reporting',report=?,token=?,execution_status='ok',"
                "completion_status='ok',detail='',updated_at=? WHERE id=? AND state='completed'",
                (json.dumps(report, separators=(",", ":")), token, time.time(), report_id),
            )
            assert cursor.rowcount == 1

        state_path = case.home / "gateway/state.sqlite3"
        database_lock = sqlite3.connect(state_path, timeout=5.0, isolation_level=None)
        database_lock.execute("BEGIN IMMEDIATE")
        executor = case.start_executor()

        # No business process is active now: the executor itself must own the
        # request lock while its run.finish RPC waits for the gateway DB lock.
        wait_for(lambda: not _request_lock_is_free(request_lock), timeout=5.0)
        assert executor.poll() is None, "executor exited while run.finish was blocked"
        assert case.requests_in("reporting")
        summary_path = case.home / "run/task-summary.json"

        # A direct state transition avoids calling queue.admit(), which would
        # eagerly rewrite the summary and mask the executor refresh contract.
        (case.home / "run/freeze").touch()
        now = time.time()
        with sqlite3.connect(supervisor_path, timeout=3.0) as database:
            database.execute(
                "INSERT INTO requests(id,job_id,module,topic,model,state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'queued',?,?)",
                (queued_id, JOB, "alpha", TOPIC, 0, now, now),
            )

        started = time.monotonic()

        def summary_matches_database():
            if not summary_path.is_file():
                return None
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            expected_requests, expected_totals = _task_summary_database_state(case.home)
            return summary if (
                summary.get("requests") == expected_requests
                and summary.get("totals") == expected_totals
            ) else None

        refreshed = wait_for(summary_matches_database, timeout=3.0)
        assert time.monotonic() - started < 3.0
        assert any(
            row["id"] == queued_id and row["state"] == "queued"
            for row in refreshed["requests"]
        ), refreshed
        assert refreshed["totals"]["queued_count"] == 1, refreshed["totals"]
        assert executor.poll() is None, "reporting RPC should remain blocked until the DB lock is released"
        assert not _request_lock_is_free(request_lock), "executor released the reporting request lock early"

        database_lock.execute("ROLLBACK")
        database_lock.close()
        database_lock = None
        wait_for(lambda: case.requests_in("completed"), timeout=5.0)
    finally:
        if database_lock is not None:
            database_lock.execute("ROLLBACK")
            database_lock.close()
        try:
            case.close()
        finally:
            telegram.close()


def test_idle_longpoll_shutdown_closes_listener_within_three_seconds(isolated_root):
    telegram = _LocalTelegram(block_getupdates_at_or_after=0)
    home = isolated_root / "home"
    _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    port = json.loads((home / "config/bots.json").read_text(encoding="utf-8"))["web_port"]
    gateway = _GatewayProcess(home, telegram)
    try:
        gateway.start()
        assert telegram.longpoll_entered.wait(timeout=5.0), "gateway did not enter the fake idle long poll"
        wait_for(lambda: _loopback_accepts(port), timeout=3.0)
        elapsed = gateway.stop(timeout=3.0)
        assert elapsed < 3.0
        assert not _loopback_accepts(port), "gateway web listener remained open after SIGTERM"
    finally:
        telegram.release_longpoll()
        gateway.stop()
        telegram.close()
