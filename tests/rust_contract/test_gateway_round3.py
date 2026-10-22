"""Offline lifecycle regressions for the gateway completion buffer and shutdown."""

from __future__ import annotations

import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import threading
import time

import pytest

from test_gateway_contract import (
    event_row,
    event_status,
    _fake_token,
    open_context,
    rpc_value,
    run_cli,
    wait_for,
)
from test_gateway_v054 import _GatewayProcess, _LocalTelegram, _runtime_case, _seed_home


@pytest.fixture
def round3_root():
    with tempfile.TemporaryDirectory(prefix="pylon-round3-", dir="/tmp") as directory:
        yield Path(directory)


def _completion_files(home: Path) -> list[Path] | None:
    directory = home / "gateway/completions"
    try:
        return [path for path in directory.iterdir() if path.is_file()]
    except (FileNotFoundError, NotADirectoryError):
        return None


def test_received_receipt_is_buffered_before_sigterm_and_recovered_after_sqlite_lock(
    round3_root,
):
    telegram = _LocalTelegram(block_send_message_response=True)
    home = round3_root / "home"
    tokens = _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    gateway = _GatewayProcess(home, telegram)
    database_lock = None
    event_id = "round3-receipt-held-lock"
    message = "round3 receipt must be recovered"

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
        assert telegram.send_message_entered.wait(timeout=5.0), "delivery did not reach sendMessage"

        database_lock = sqlite3.connect(
            home / "gateway/state.sqlite3", timeout=5.0, isolation_level=None
        )
        database_lock.execute("BEGIN IMMEDIATE")
        telegram.release_send_message_response()
        assert telegram.send_message_response_sent.wait(timeout=5.0), "fake Telegram did not return the receipt"

        buffered_files = wait_for(lambda: _completion_files(home) or None, timeout=5.0)
        completion_dir = home / "gateway/completions"
        assert stat.S_IMODE(completion_dir.stat().st_mode) == 0o700
        assert buffered_files
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in buffered_files)
        assert telegram.method_count("sendMessage", text=message + "\n") == 1

        elapsed = gateway.stop(timeout=8.0)
        assert elapsed < 8.0, f"SIGTERM exceeded the global eight-second budget: {elapsed:.3f}s"

        # Keep the SQLite writer lock held until after the gateway has exited.
        database_lock.execute("ROLLBACK")
        database_lock.close()
        database_lock = None

        gateway.start()
        delivered = wait_for(lambda: event_status(home, event_id, "delivered"), timeout=10.0)
        assert delivered["attempts"] == 1
        assert delivered["receipt"] == [{"message_id": 7001, "chat_id": 101}]
        assert telegram.count(tokens["bot_one"], "sendMessage") == 1
    finally:
        telegram.release_send_message_response()
        if database_lock is not None:
            database_lock.execute("ROLLBACK")
            database_lock.close()
        gateway.stop()
        telegram.close()


def test_unconfirmed_slow_send_becomes_uncertain_on_sigterm_and_is_not_replayed(round3_root):
    telegram = _LocalTelegram(block_send_message_response=True)
    home = round3_root / "home"
    tokens = _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    gateway = _GatewayProcess(home, telegram)
    event_id = "round3-send-without-receipt"
    message = "round3 send has no receipt"

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
        assert telegram.send_message_entered.wait(timeout=5.0), "delivery did not reach sendMessage"
        assert not telegram.send_message_response_sent.is_set(), "fixture unexpectedly returned a receipt"
        polls_before_restart = telegram.count(tokens["bot_one"], "getUpdates")

        elapsed = gateway.stop(timeout=8.0)
        assert elapsed < 8.0, f"SIGTERM exceeded the global eight-second budget: {elapsed:.3f}s"
        telegram.release_send_message_response()

        gateway.start()
        uncertain = wait_for(lambda: event_status(home, event_id, "uncertain"), timeout=8.0)
        assert uncertain["attempts"] == 1
        wait_for(
            lambda: telegram.count(tokens["bot_one"], "getUpdates") > polls_before_restart,
            timeout=5.0,
        )
        assert telegram.method_count("sendMessage", text=message + "\n") == 1
        assert event_row(home, event_id)["status"] == "uncertain"
    finally:
        telegram.release_send_message_response()
        gateway.stop()
        telegram.close()


def test_returned_command_result_is_buffered_and_recovered_without_rerunning_handler(
    round3_root,
):
    telegram = _LocalTelegram(block_getupdates_at_or_after=2)
    case = _runtime_case(round3_root / "home", telegram)
    state_path = case.home / "gateway/state.sqlite3"
    database_lock = None
    reply_text = "round3 command reply is recovered"

    try:
        manifest_path = case.home / "modules/alpha/module.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["commands"] = [
            {
                "name": "alpha_cmd",
                "description": "round3 isolated command fixture",
                "topic": "topic_alpha",
                "menu": True,
                "order": 1,
            }
        ]
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        envelope = json.dumps(
            {
                "api_version": 2,
                "value": {"text": reply_text, "reply_to_message_id": 11},
            },
            separators=(",", ":"),
        )
        (case.home / "modules/alpha/handler.sh").write_text(
            "#!/bin/bash\nset -eu\n"
            'cat > "$PYLON_HOME/data/completion-input.json"\n'
            'printf "started\\n" >> "$PYLON_HOME/data/completion-count"\n'
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

        # Executor must be listening before the gateway can claim a command;
        # otherwise request_executor fails and the update becomes uncertain.
        executor = case.start_executor()
        case.gateway.start()
        wait_for(lambda: case.count("completion-count") == 1, timeout=10.0)
        assert telegram.longpoll_entered.wait(timeout=5.0), "poll lane did not settle after storing the command"

        def update_status():
            with sqlite3.connect(state_path, timeout=3.0) as database:
                return database.execute(
                    "SELECT status FROM updates WHERE profile='bot_one' AND update_id=1"
                ).fetchone()

        assert update_status() == ("running",)
        database_lock = sqlite3.connect(state_path, timeout=5.0, isolation_level=None)
        database_lock.execute("BEGIN IMMEDIATE")
        (case.data / "release-completion").touch()
        wait_for((case.data / "handler-returned").is_file, timeout=5.0)

        completion_dir = case.home / "gateway/completions"

        def buffered_command_result():
            try:
                candidates = completion_dir.glob("*.json")
                return next(
                    (path for path in candidates if reply_text in path.read_text(encoding="utf-8")),
                    None,
                )
            except (FileNotFoundError, NotADirectoryError):
                return None

        completion_file = wait_for(buffered_command_result, timeout=5.0)
        assert stat.S_IMODE(completion_file.stat().st_mode) == 0o600

        with sqlite3.connect(state_path, timeout=3.0) as database:
            context_count = database.execute(
                "SELECT count(*) FROM contexts WHERE module='alpha' AND bot='bot_one' AND origin='command'"
            ).fetchone()[0]
        assert context_count == 1, "command context disappeared before its buffered result was applied"

        def reply_rows():
            with sqlite3.connect(state_path, timeout=3.0) as database:
                rows = database.execute(
                    "SELECT id,status,attempts,payload,module,job_id FROM events "
                    "WHERE profile='bot_one' AND kind='text'"
                ).fetchall()
            return [
                {
                    "id": row[0],
                    "status": row[1],
                    "attempts": row[2],
                    "payload": json.loads(row[3]),
                    "module": row[4],
                    "job_id": row[5],
                }
                for row in rows
                if json.loads(row[3]).get("text") == reply_text
            ]

        assert reply_rows() == [], "reply was written despite the held SQLite writer lock"
        polls_before_restart = telegram.count(_fake_token("bot_one", "101"), "getUpdates")
        elapsed = case.gateway.stop(timeout=8.0)
        assert elapsed < 8.0, f"SIGTERM exceeded the global eight-second budget: {elapsed:.3f}s"
        assert executor.poll() is None, "gateway shutdown stopped the independent executor"

        # Release the writer lock only after the gateway process has exited.
        database_lock.execute("ROLLBACK")
        database_lock.close()
        database_lock = None

        case.gateway.start()
        wait_for(lambda: update_status() == ("done",), timeout=8.0)
        recovered = wait_for(
            lambda: next((row for row in reply_rows() if row["status"] == "delivered"), None),
            timeout=8.0,
        )
        assert recovered["attempts"] == 1
        assert recovered["module"] == "alpha"
        assert recovered["job_id"] == "bot"
        assert recovered["payload"] == {"text": reply_text, "reply_to_message_id": 11}
        assert telegram.method_count("getUpdates") > polls_before_restart
        assert case.count("completion-count") == 1
        assert telegram.send_fragment_count(_fake_token("bot_one", "101"), reply_text) == 1

        with sqlite3.connect(state_path, timeout=3.0) as database:
            remaining_contexts = database.execute(
                "SELECT count(*) FROM contexts WHERE module='alpha' AND bot='bot_one' AND origin='command'"
            ).fetchone()[0]
        assert remaining_contexts == 0, "recovery did not clean up the command context"

        with telegram._lock:
            reply_calls = [
                request
                for token, method, request in telegram._calls
                if token == _fake_token("bot_one", "101")
                and method == "sendMessage"
                and request.get("text") == reply_text + "\n"
            ]
        assert len(reply_calls) == 1
        assert str(reply_calls[0].get("chat_id")) == "101"
        assert reply_calls[0].get("reply_parameters") == {"message_id": 11}
    finally:
        (case.data / "release-completion").touch(exist_ok=True)
        if database_lock is not None:
            database_lock.execute("ROLLBACK")
            database_lock.close()
        telegram.release_longpoll()
        case.close()
        telegram.close()


def test_sigterm_stops_four_blocked_bot_pollers_within_one_global_budget(round3_root):
    telegram = _LocalTelegram(block_getupdates_at_or_after=0)
    home = round3_root / "home"
    bots = [(f"bot_{index}", str(100 + index)) for index in range(4)]
    modules = [
        {"id": f"module_{index}", "topic": f"topic_{index}", "job_id": f"job_{index}"}
        for index in range(4)
    ]
    routes = {f"topic_{index}": f"bot_{index}" for index in range(4)}
    tokens = _seed_home(home, telegram, bots, modules, routes)
    gateway = _GatewayProcess(home, telegram)

    try:
        gateway.start()
        wait_for(
            lambda: all(telegram.count(tokens[bot_id], "getUpdates") > 0 for bot_id, _ in bots),
            timeout=6.0,
        )
        assert telegram.longpoll_entered.is_set(), "fake Telegram did not hold the bot pollers"

        elapsed = gateway.stop(timeout=8.0)
        assert elapsed < 8.0, f"multi-bot SIGTERM exceeded the global eight-second budget: {elapsed:.3f}s"
    finally:
        telegram.release_longpoll()
        gateway.stop()
        telegram.close()


class _MalformedTelegram:
    """Loopback Telegram fixture that returns selected invalid write envelopes."""

    def __init__(self, responses: dict[str, tuple[int, str, bytes | str | dict]]):
        self._responses = responses
        self._lock = threading.RLock()
        self._calls: list[tuple[str, str, dict]] = []
        self._message_id = 8100
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
                    response = (
                        owner._responses.get(str(request.get("text", "")))
                        if method == "sendMessage"
                        else None
                    )
                    if response is None:
                        status, content_type, payload = owner._valid_response(method, request)
                    else:
                        status, content_type, payload = response

                if isinstance(payload, bytes):
                    output = payload
                elif isinstance(payload, str):
                    output = payload.encode("utf-8")
                else:
                    output = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(output)))
                self.end_headers()
                try:
                    self.wfile.write(output)
                    self.wfile.flush()
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _valid_response(self, method: str, request: dict) -> tuple[int, str, dict]:
        if method == "getUpdates":
            result = []
        elif method == "getMyCommands":
            result = []
        elif method == "setMyCommands":
            result = True
        elif method == "getChatMenuButton":
            result = {"type": "commands"}
        elif method == "setChatMenuButton":
            result = True
        elif method == "sendMessage":
            self._message_id += 1
            chat_id = request.get("chat_id")
            if isinstance(chat_id, str) and chat_id.isdigit():
                chat_id = int(chat_id)
            result = {"message_id": self._message_id, "chat": {"id": chat_id}}
        elif method == "sendDocument":
            self._message_id += 1
            result = {"message_id": self._message_id, "chat": {"id": request.get("chat_id")}, "document": {}}
        elif method == "getMe":
            result = {"id": 1, "username": "fixture_bot"}
        elif method == "getChat":
            result = {"id": request.get("chat_id")}
        else:
            result = True
        return 200, "application/json", {"ok": True, "result": result}

    def method_count(self, method: str, *, text: str | None = None) -> int:
        with self._lock:
            return sum(
                1
                for _, call_method, request in self._calls
                if call_method == method and (text is None or request.get("text") == text)
            )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def _grant_diagnostics(home: Path, module_id: str) -> None:
    manifest_path = home / "modules" / module_id / "module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = ["diagnostics:read"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def test_invalid_telegram_write_envelopes_are_uncertain_counted_and_never_replayed(round3_root):
    cases = [
        (
            "round3 proxy html 502",
            502,
            "text/html; charset=utf-8",
            "<html>fixture gateway error</html>",
            "telegram_proxy_invalid",
        ),
        (
            "round3 proxy json 502",
            502,
            "application/json",
            {"message": "fixture bad gateway"},
            "telegram_proxy_invalid",
        ),
        (
            "round3 proxy json 503",
            503,
            "application/json",
            {"message": "fixture unavailable"},
            "telegram_proxy_invalid",
        ),
        (
            "round3 proxy json 504",
            504,
            "application/json",
            {"error_code": 504, "description": "fixture timeout"},
            "telegram_proxy_invalid",
        ),
        (
            "round3 response missing ok",
            200,
            "application/json",
            {"result": {"message_id": 9999}},
            "telegram_response_invalid",
        ),
    ]
    telegram = _MalformedTelegram(
        {text + "\n": (status, content_type, payload) for text, status, content_type, payload, _ in cases}
    )
    home = round3_root / "home"
    tokens = _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [
            {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
            {"id": "observer", "topic": "topic_alpha", "job_id": "job_observer"},
        ],
        {"topic_alpha": "bot_one"},
    )
    _grant_diagnostics(home, "observer")
    gateway = _GatewayProcess(home, telegram)
    event_ids = {text: f"round3-invalid-response-{index}" for index, (text, *_rest) in enumerate(cases)}

    try:
        gateway.start()
        observer_context = open_context(home, "observer", "job_observer")["token"]
        context = open_context(home, "alpha", "job_alpha")["token"]
        for text, *_rest in cases:
            rpc_value(
                home,
                "delivery.submit",
                {
                    "id": event_ids[text],
                    "kind": "text",
                    "job_id": "job_alpha",
                    "topic": "topic_alpha",
                    "payload": {"text": text},
                },
                context,
            )

        def uncertain_rows():
            rows = [event_row(home, event_ids[text]) for text, *_rest in cases]
            return rows if all(row is not None and row["status"] == "uncertain" for row in rows) else None

        terminal = wait_for(uncertain_rows, timeout=8.0)
        assert all(row["attempts"] == 1 for row in terminal), terminal

        response = rpc_value(home, "diagnostics", context=observer_context)
        metrics = response["metrics"]
        assert metrics["telegram_proxy_invalid"] == 4
        assert metrics["telegram_response_invalid"] == 1
        legacy_metric_keys = {
            "sqlite_busy",
            "workers",
            "queued_count",
            "oldest_queued_seconds",
            "new_uncertain",
        }
        assert legacy_metric_keys <= metrics.keys()
        assert event_row(home, event_ids[cases[0][0]])["status"] == "uncertain"
        assert all(telegram.method_count("sendMessage", text=text + "\n") == 1 for text, *_rest in cases)

        polls_before_restart = telegram.method_count("getUpdates")
        gateway.stop(timeout=8.0)
        gateway.start()
        wait_for(
            lambda: telegram.method_count("getUpdates") > polls_before_restart,
            timeout=5.0,
        )
        observer_context = open_context(home, "observer", "job_observer")["token"]
        after_restart = rpc_value(home, "diagnostics", context=observer_context)
        after_restart_metrics = after_restart["metrics"]
        assert after_restart_metrics["telegram_proxy_invalid"] == 0
        assert after_restart_metrics["telegram_response_invalid"] == 0
        assert legacy_metric_keys <= after_restart_metrics.keys()
        for text, *_rest in cases:
            row = event_row(home, event_ids[text])
            assert row["status"] == "uncertain"
            assert row["attempts"] == 1
            assert telegram.method_count("sendMessage", text=text + "\n") == 1
    finally:
        gateway.stop()
        telegram.close()


def test_invalid_socket_parent_exits_with_error_without_panicking(round3_root):
    telegram = _LocalTelegram()
    home = round3_root / "home"
    _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    run_parent = home / "run"
    assert not run_parent.exists(), "fixture expected seed_home to leave run/ absent"
    run_parent.write_text("fixture: run must be a directory", encoding="utf-8")
    env = os.environ.copy()
    env["PYLON_TEST_TELEGRAM"] = telegram.url

    try:
        result = run_cli(home, "--offline", "gateway", env=env)
        output = f"{result.stdout}\n{result.stderr}".lower()
        assert result.returncode != 0, "gateway unexpectedly started with a file at its socket parent"
        assert "panicked at" not in output, output
        assert "result::unwrap" not in output, output
    finally:
        telegram.close()


def test_frozen_web_route_returns_503_and_keeps_gateway_alive(round3_root):
    telegram = _LocalTelegram()
    home = round3_root / "home"
    _seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha", "web": True}],
        {"topic_alpha": "bot_one"},
    )
    freeze = home / "run/freeze"
    freeze.parent.mkdir(parents=True, exist_ok=True)
    freeze.touch()
    settings = json.loads((home / "config/bots.json").read_text(encoding="utf-8"))
    gateway = _GatewayProcess(home, telegram)

    try:
        gateway.start()
        connection = http.client.HTTPConnection("127.0.0.1", settings["web_port"], timeout=3.0)
        try:
            connection.request("GET", "/pylon/market/frozen-fixture")
            response = connection.getresponse()
            body = response.read()
        finally:
            connection.close()

        assert response.status == 503
        assert body
        assert gateway.proc is not None and gateway.proc.poll() is None
        assert rpc_value(home, "health")["api_version"] == 2
    finally:
        gateway.stop()
        telegram.close()
