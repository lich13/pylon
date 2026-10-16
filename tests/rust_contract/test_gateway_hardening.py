"""Black-box contracts for gateway endpoint, path, and error hardening."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import socket
import socketserver
import sqlite3
import tempfile
import threading
import time

import pytest

from test_gateway_contract import (
    FakeTelegram,
    Gateway,
    assert_rpc_rejected,
    event_row,
    open_context,
    rpc,
    rpc_value,
    run_cli,
    seed_home,
    wait_for,
)


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory(prefix="pylon-rust-hardening-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


def _seed_gateway(home: Path, telegram: FakeTelegram) -> dict[str, str]:
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


def _set_bots_enabled(home: Path, enabled: bool) -> None:
    settings_path = home / "config/bots.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    for bot in settings["bots"].values():
        bot["enabled"] = enabled
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _bark_payload(api_base: str, *, device_key: str = "fixture-bark-device-key") -> dict:
    return {
        "api_base": api_base,
        "body": {
            "device_key": device_key,
            "title": "isolated Bark contract",
            "body": "fixture message",
        },
    }


def _submit_params(event_id: str, payload: dict, *, module: str = "alpha") -> dict:
    return {
        "id": event_id,
        "kind": "bark",
        "job_id": f"job_{module}",
        "topic": f"topic_{module}",
        "next_attempt": time.time() + 600,
        "payload": payload,
    }


def _insert_event(
    home: Path,
    event_id: str,
    payload: dict,
    *,
    module: str = "alpha",
    profile: str = "bot_one",
    job_id: str = "job_alpha",
    status: str = "pending",
    attempts: int = 0,
    next_attempt: float = 0.0,
    error: str | None = None,
) -> None:
    now = time.time()
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        database.execute(
            """
            INSERT INTO events(
                id,profile,job_id,kind,payload,status,attempts,next_attempt,
                created_at,updated_at,error,module
            ) VALUES(?,?,?,'bark',?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                profile,
                job_id,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                status,
                attempts,
                next_attempt,
                now,
                now,
                error,
                module,
            ),
        )
        database.commit()


def _event_snapshot(home: Path, event_id: str) -> tuple | None:
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        return database.execute(
            """
            SELECT profile,job_id,kind,payload,status,attempts,next_attempt,
                   created_at,updated_at,receipt,error,module
            FROM events WHERE id=?
            """,
            (event_id,),
        ).fetchone()


def test_bark_submit_rejects_non_public_and_encoded_endpoints_without_network(
    workdir, telegram
):
    home = workdir / "home"
    _seed_gateway(home, telegram)
    _set_bots_enabled(home, False)
    endpoints = [
        "https://169.254.169.254",
        "https://127.0.0.1",
        "https://10.12.0.3",
        "https://172.16.1.1",
        "https://192.168.1.9",
        "https://100.64.0.1",
        "https://[::]",
        "https://[::1]",
        "https://[fc00::1]",
        "https://[fe80::1]",
        "https://[::ffff:127.0.0.1]",
        "https://[64:ff9b::a00:1]",
        "https://[2002:7f00:1::]",
        "https://2130706433",
        "https://0x7f000001",
        "https://0177.0.0.1",
        "https://127.1",
        "https://localhost",
        "http://93.184.216.34",
        "https://fixture-user:fixture-bark-secret@93.184.216.34",
    ]

    with Gateway(home, telegram):
        context = open_context(home, "alpha", "job_alpha")["token"]
        for index, endpoint in enumerate(endpoints):
            event_id = f"invalid-bark-{index:02d}"
            response = rpc(
                home,
                "delivery.submit",
                _submit_params(event_id, _bark_payload(endpoint)),
                context,
            )
            assert_rpc_rejected(response)
            assert event_row(home, event_id) is None
            assert "fixture-bark-secret" not in json.dumps(response)

        # `localhost` resolves only to loopback; no remote Bark host is contacted.
        assert telegram.method_counts() == {}


def test_bark_submit_accepts_public_literal_ip_when_bot_is_disabled(workdir, telegram):
    home = workdir / "home"
    _seed_gateway(home, telegram)
    _set_bots_enabled(home, False)
    event_id = "public-literal-bark"

    with Gateway(home, telegram):
        context = open_context(home, "alpha", "job_alpha")["token"]
        submitted = rpc_value(
            home,
            "delivery.submit",
            _submit_params(
                event_id,
                _bark_payload("https://93.184.216.34/prefix"),
            ),
            context,
        )

        assert submitted["id"] == event_id
        assert submitted["status"] == "pending"
        assert submitted["attempts"] == 0
        assert event_row(home, event_id)["status"] == "pending"
        assert telegram.method_counts() == {}


class _LoopbackAttemptSink:
    """Count only local TCP attempts; it never forwards or reaches the network."""

    def __init__(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._connections = 0
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def connections(self) -> int:
        with self._lock:
            return self._connections

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self._connections += 1
            connection.close()

    def close(self) -> None:
        self._stop.set()
        self._listener.close()
        self._thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def test_bark_delivery_rechecks_a_persisted_private_ip_before_connecting(workdir, telegram):
    home = workdir / "home"
    _seed_gateway(home, telegram)
    event_id = "persisted-private-bark"

    with _LoopbackAttemptSink() as sink:
        with Gateway(home, telegram):
            _insert_event(
                home,
                event_id,
                _bark_payload(f"https://127.0.0.1:{sink.port}/"),
            )

            def settled_event():
                current = event_row(home, event_id)
                return (
                    current
                    if current and current["status"] not in {"pending", "sending"}
                    else None
                )

            failed = wait_for(
                settled_event,
                timeout=5,
            )

            assert failed["status"] == "failed"
            assert sink.connections == 0


def test_historical_uncertain_bark_submit_is_idempotent_and_module_scoped(
    workdir, telegram
):
    home = workdir / "home"
    _seed_gateway(home, telegram)
    _set_bots_enabled(home, False)
    event_id = "historical-bark-uncertain"
    payload = _bark_payload("https://localhost/prefix")

    with Gateway(home, telegram):
        alpha = open_context(home, "alpha", "job_alpha")["token"]
        beta = open_context(home, "beta", "job_beta")["token"]
        _insert_event(
            home,
            event_id,
            payload,
            status="uncertain",
            attempts=4,
            next_attempt=time.time() + 900,
            error="fixture prior outcome uncertain",
        )
        before = _event_snapshot(home, event_id)

        repeated = rpc(
            home,
            "delivery.submit",
            _submit_params(event_id, payload),
            alpha,
        )
        assert repeated["ok"] is True, repeated
        assert repeated["value"]["status"] == "uncertain"
        assert repeated["value"]["attempts"] == 4
        assert _event_snapshot(home, event_id) == before

        changed_payload = _bark_payload(
            "https://localhost/prefix", device_key="different-fixture-device-key"
        )
        conflict = rpc(
            home,
            "delivery.submit",
            _submit_params(event_id, changed_payload),
            alpha,
        )
        assert_rpc_rejected(conflict)

        cross_module = rpc(
            home,
            "delivery.submit",
            _submit_params(event_id, payload, module="beta"),
            beta,
        )
        assert_rpc_rejected(cross_module)
        assert _event_snapshot(home, event_id) == before
        # localhost is local-only, and bots are disabled, so this idempotency
        # check cannot trigger DNS traffic or a delivery attempt.
        assert telegram.method_counts() == {}


class _UncertainBroker:
    def __init__(
        self,
        path: Path,
        internal_secret: str,
        behavior: str = "uncertain_response",
    ):
        owner = self
        self.requests: list[dict] = []
        self._lock = threading.Lock()
        self.behavior = behavior
        self.internal_secret = internal_secret

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                with owner._lock:
                    owner.requests.append(request)

                if owner.behavior == "drop_after_request":
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return

                if owner.behavior == "invalid_json":
                    body = f'{{"detail":"{owner.internal_secret}",'.encode()
                else:
                    body = json.dumps(
                        {
                            "ok": False,
                            "error": "operation result uncertain; inspect retained state",
                            "detail": owner.internal_secret,
                        }
                    ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self._server = Server(str(path), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


@pytest.mark.parametrize(
    "behavior",
    ["uncertain_response", "drop_after_request", "invalid_json"],
    ids=["explicit-uncertain", "disconnect-after-receive", "invalid-json"],
)
def test_uncertain_broker_result_is_not_replayed_or_exposed(
    workdir, telegram, behavior
):
    home = workdir / "home"
    _seed_gateway(home, telegram)
    _set_bots_enabled(home, False)
    secret = "fixture-broker-secret-must-not-leak"
    manifest_path = home / "modules/alpha/module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = ["fixture.write"]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    with Gateway(home, telegram):
        context = open_context(home, "alpha", "job_alpha")["token"]
        with _UncertainBroker(
            home / "run/broker.sock", secret, behavior=behavior
        ) as broker:
            response = rpc(
                home,
                "capability.invoke",
                {"capability": "fixture.write", "params": {}},
                context,
            )

        assert response.get("ok") is False, response
        assert response.get("error") == "operation result uncertain; inspect retained state"
        assert response.get("error_code") == "rejected", response
        assert response.get("retryable") is False, response
        assert len(broker.requests) == 1
        assert broker.requests[0]["request"]["capability"] == "fixture.write"
        assert secret not in json.dumps(response)


def test_workspace_contained_allows_missing_suffix_through_internal_symlink(
    workdir, telegram
):
    home = workdir / "home"
    _seed_gateway(home, telegram)
    target = home / "profiles/contained-target"
    target.mkdir()
    (home / "profiles/internal-link").symlink_to(target, target_is_directory=True)

    settings_path = home / "config/bots.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["workspaces"]["default"]["root"] = (
        "profiles/internal-link/missing/suffix"
    )
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    result = run_cli(home, "check")
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.parametrize("dangling", [False, True], ids=["external", "dangling"])
def test_workspace_contained_rejects_missing_suffix_through_external_symlink(
    workdir, telegram, dangling
):
    home = workdir / "home"
    _seed_gateway(home, telegram)
    target = workdir / "outside-workspace"
    if not dangling:
        target.mkdir()
    link = home / "profiles/external-link"
    link.symlink_to(target, target_is_directory=True)
    assert link.is_symlink()
    assert link.exists() is (not dangling)

    settings_path = home / "config/bots.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["workspaces"]["default"]["root"] = (
        "profiles/external-link/missing/suffix"
    )
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    result = run_cli(home, "check")
    assert result.returncode != 0
    assert result.stderr.strip(), result.stdout
    assert not (home / "gateway/state.sqlite3").exists()
