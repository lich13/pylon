"""Isolated regressions for attachment reads and uncertain RPC writes."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import tempfile
import threading
import time

import pytest

from test_gateway_contract import (
    FakeTelegram, Gateway, event_status, open_context, rpc, rpc_value,
    seed_home, wait_for,
)

UNCERTAIN = "operation result uncertain; inspect retained state"


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory(prefix="pylon-io-", dir="/tmp") as directory:
        yield Path(directory).resolve()


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


def seed(home, telegram, *, hooks=False):
    modules = [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha", "command_name": "alpha"}]
    if hooks:
        modules.append({"id": "beta", "topic": "topic_alpha"})
    tokens = seed_home(home, telegram, [("bot_one", "101")], modules, {"topic_alpha": "bot_one"})
    (home / "run").mkdir()
    if hooks:
        for module, field in [("alpha", "capabilities"), ("beta", "hooks")]:
            path = home / f"modules/{module}/module.json"
            manifest = json.loads(path.read_text())
            manifest[field] = ["hook:fixture" if module == "alpha" else "fixture"]
            path.write_text(json.dumps(manifest))
    return tokens


@contextmanager
def remote(home, name, body, status=200):
    calls = []
    stopping = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.connection.settimeout(3)
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            if body is None:
                self.close_connection = True
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = socketserver.UnixStreamServer(str(home / f"run/{name}.sock"), Handler)
    server.timeout = 0.1

    def serve():
        while not stopping.is_set():
            server.handle_request()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield calls
    finally:
        stopping.set()
        thread.join(timeout=4)
        server.server_close()
        assert not thread.is_alive()


@pytest.mark.parametrize("backend", ["executor", "broker"])
@pytest.mark.parametrize("body,status,error,retryable", [
    (b'{"ok":true,"value":null}', 200, None, None),
    (json.dumps({"ok": False, "error": UNCERTAIN, "retryable": False}).encode(), 200, UNCERTAIN, False),
    (b'{"ok":false,"error":"request rejected","retryable":false}', 200, "request rejected", False),
    (b'{"ok":false,"error":"service temporarily unavailable","retryable":true}', 200, "service temporarily unavailable", True),
    (None, 200, UNCERTAIN, False),
    (b'fixture-invalid-response', 200, UNCERTAIN, False),
    (b'{"ok":true}', 200, UNCERTAIN, False),
    (b'{}', 200, UNCERTAIN, False),
    (b'{"ok":true,"value":null}', 503, UNCERTAIN, False),
])
def test_rpc_preserves_write_outcome(home, telegram, backend, body, status, error, retryable):
    seed(home, telegram)
    method, params = ("task.run", {"job_id": "job_alpha"}) if backend == "executor" else ("capability.invoke", {"capability": "fixture"})
    with remote(home, backend, body, status) as calls, Gateway(home, telegram):
        token = open_context(home, "alpha", "job_alpha")["token"]
        response = rpc(home, method, params, token)
        assert response["ok"] is (error is None)
        if error:
            assert response["error"] == error
            assert response["retryable"] is retryable
        assert len(calls) == 1


@pytest.mark.parametrize("backend", ["executor", "broker"])
def test_connection_failure_is_retryable_before_acceptance(home, telegram, backend):
    seed(home, telegram)
    with Gateway(home, telegram):
        token = open_context(home, "alpha", "job_alpha")["token"]
        method, params = ("task.run", {"job_id": "job_alpha"}) if backend == "executor" else ("capability.invoke", {"capability": "fixture"})
        response = rpc(home, method, params, token)
        assert response["error"] == "service temporarily unavailable"
        assert response["retryable"] is True


@pytest.mark.parametrize("body", [
    None,
    b'fixture-invalid-response',
    json.dumps({"ok": False, "error": UNCERTAIN, "retryable": False}).encode(),
])
def test_uncertain_command_retains_execution_context_without_replay(home, telegram, body):
    tokens = seed(home, telegram)
    with remote(home, "executor", body) as calls, Gateway(home, telegram):
        telegram.set_updates(tokens["bot_one"], [{
            "update_id": 1,
            "message": {"message_id": 1, "date": 1, "from": {"id": 101, "is_bot": False},
                        "chat": {"id": 101, "type": "private"}, "text": "/alpha"},
        }])

        def settled():
            with sqlite3.connect(home / "gateway/state.sqlite3") as db:
                row = db.execute("SELECT status FROM updates WHERE update_id=1").fetchone()
                return row and row[0] == "uncertain"

        wait_for(settled)
        assert len(calls) == 1
        context = rpc_value(home, "context.inspect", {}, calls[0]["context"])
        assert context["module"] == "alpha"


@pytest.mark.parametrize("body,retained", [
    (None, True),
    (b'{"ok":false,"error":"request rejected","retryable":false}', False),
    (b'{"ok":true,"value":null}', False),
])
def test_extension_keeps_only_uncertain_execution_context(home, telegram, body, retained):
    seed(home, telegram, hooks=True)
    with remote(home, "executor", body) as calls, Gateway(home, telegram):
        token = open_context(home, "alpha", "job_alpha")["token"]
        rpc(home, "extension.invoke", {"action": "fixture", "module": "beta"}, token)
        assert len(calls) == 1
        response = rpc(home, "context.inspect", {}, calls[0]["context"])
        assert response["ok"] is retained


@pytest.mark.parametrize("path_kind", ["outside", "parent", "directory"])
def test_attachment_boundary_is_also_enforced_before_enqueue(home, telegram, path_kind):
    seed(home, telegram)
    spool = home / "data/attachments"
    spool.mkdir(parents=True)
    outside = home / "data/fixture.txt"
    outside.write_bytes(b"fixture")
    path = {"outside": outside, "parent": spool / "../fixture.txt", "directory": spool}[path_kind]
    with Gateway(home, telegram):
        token = open_context(home, "alpha", "job_alpha")["token"]
        response = rpc(home, "delivery.submit", {
            "id": "fixture_document", "kind": "document", "topic": "topic_alpha",
            "payload": {"path": str(path), "sha256": hashlib.sha256(b"fixture").hexdigest()},
        }, token)
        assert response["ok"] is False
        with sqlite3.connect(home / "gateway/state.sqlite3") as db:
            assert db.execute("SELECT COUNT(*) FROM events WHERE id='fixture_document'").fetchone()[0] == 0


@pytest.mark.parametrize("replacement", ["none", "file-link", "directory-link", "hard-link", "fifo", "changed-content", "too-large"])
def test_document_is_opened_safely_at_send_time(home, telegram, replacement):
    seed(home, telegram)
    received = []
    parent_handler = telegram.server.RequestHandlerClass
    content = b"fixture-attachment"

    class MultipartHandler(parent_handler):
        def do_POST(self):
            if not self.path.endswith("/sendDocument"):
                return super().do_POST()
            received.append(self.rfile.read(int(self.headers["Content-Length"])))
            value = json.dumps({"ok": True, "result": {"message_id": 123, "chat": {"id": 101}, "document": {"file_id": "fixture"}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(value)))
            self.end_headers()
            self.wfile.write(value)

    telegram.server.RequestHandlerClass = MultipartHandler
    spool = home / "data/attachments"
    nested = spool / "nested"
    nested.mkdir(parents=True)
    attachment = nested / "fixture.txt"
    attachment.write_bytes(content)
    outside = home / "outside"
    outside.mkdir()
    (outside / "fixture.txt").write_bytes(content)
    with Gateway(home, telegram):
        token = open_context(home, "alpha", "job_alpha")["token"]
        rpc_value(home, "delivery.submit", {
            "id": "fixture_document", "kind": "document", "topic": "topic_alpha",
            "next_attempt": time.time() + 3600,
            "payload": {"path": str(attachment), "sha256": hashlib.sha256(content).hexdigest()},
        }, token)
        if replacement == "file-link":
            attachment.unlink()
            attachment.symlink_to(outside / "fixture.txt")
        elif replacement == "directory-link":
            nested.rename(spool / "original")
            nested.symlink_to(outside, target_is_directory=True)
        elif replacement == "hard-link":
            attachment.unlink()
            os.link(outside / "fixture.txt", attachment)
        elif replacement == "fifo":
            attachment.unlink()
            os.mkfifo(attachment)
        elif replacement == "too-large":
            with attachment.open("wb") as output:
                output.truncate(50 * 1024 * 1024 + 1)
        elif replacement == "changed-content":
            attachment.write_bytes(b"different-fixture")
        with sqlite3.connect(home / "gateway/state.sqlite3") as db:
            db.execute("UPDATE events SET next_attempt=0 WHERE id='fixture_document'")
        expected = "delivered" if replacement == "none" else "failed"
        event = wait_for(lambda: event_status(home, "fixture_document", expected))
        assert event["attempts"] == 1
        assert len(received) == (1 if replacement == "none" else 0)
        if received:
            assert content in received[0]
