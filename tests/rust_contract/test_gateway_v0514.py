"""Retired web APIs and non-retryable failures after business execution starts."""
import json
import os
import socket
import sqlite3

import pytest

from test_gateway_contract import open_context, rpc, rpc_value
from test_runtime_reliability import JOB, runtime_case


def test_retired_web_settings_do_not_open_a_listener_and_login_methods_are_absent(runtime_case):
    case = runtime_case
    # Holding the old configured address also proves it is no longer bound at startup.
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        path = case.home / "config/bots.json"
        settings = json.loads(path.read_text())
        settings.update(web_port=occupied.getsockname()[1], public_origin="https://example.invalid")
        path.write_text(json.dumps(settings))
        case.gateway.start()
        token = open_context(case.home, "alpha", JOB)["token"]
        for name in ["url", "issue", "redeem", "session", "logout"]:
            value = rpc(case.home, "auth." + name, {"workspace": "fixture"}, token)
            assert value["ok"] is False
            assert value["retryable"] is False
        assert rpc_value(case.home, "auth.owner", context=token)["owner"] == "101"
        with sqlite3.connect(case.home / "gateway/state.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM sqlite_schema WHERE name IN ('web_tokens','web_sessions')").fetchone()[0] == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="business fixtures use the current non-root UID")
def test_handler_side_effect_then_closed_stdin_cannot_be_automatically_retried(runtime_case):
    case = runtime_case
    manifest_path = case.home / "modules/alpha/module.json"
    manifest = json.loads(manifest_path.read_text())
    manifest.update(hooks=["fixture"], capabilities=["hook:fixture"])
    manifest_path.write_text(json.dumps(manifest))
    (case.home / "modules/alpha/handler.sh").write_text(
        '#!/bin/bash\nprintf "effect\\n" >> "$PYLON_HOME/data/effects"\nexec 0<&-\n'
        'sleep 0.1\nprintf \'{"api_version":2,"value":null}\\n\'\n'
    )
    case.start_executor()
    case.gateway.start()
    token = open_context(case.home, "alpha", JOB)["token"]
    response = rpc(case.home, "extension.invoke", {
        "module": "alpha", "action": "fixture", "payload": "x" * 524288,
    }, token)
    assert response["ok"] is False
    assert response["retryable"] is False
    assert response["error"] == "operation result uncertain; inspect retained state"
    assert case.count("effects") == 1
    with sqlite3.connect(case.home / "gateway/state.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM contexts WHERE origin='hook:fixture'").fetchone()[0] == 1
