"""Black-box contract for restarting services through a gateway outage."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess

import pytest

from test_gateway_contract import BIN, ROOT, assert_rpc_rejected, rpc, rpc_value
from test_runtime_reliability import runtime_case


pytestmark = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="offline runtime fixtures execute business processes as the current non-root user",
)


def _run_service(case) -> subprocess.CompletedProcess:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "PYLON_TEST_TELEGRAM": case.telegram.url,
        "PYLON_CONTEXT": "fixture-parent-context",
        "TELEGRAM_BOT_TOKEN": "fixture-parent-token",
    }
    return subprocess.run(
        [str(BIN), "--home", str(case.home), "--offline", "serve", "alpha--observer"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=8,
    )


def _evidence(home: Path) -> list[list[str]]:
    path = home / "data/service-evidence"
    if not path.is_file():
        return []
    return [line.split(",") for line in path.read_text(encoding="utf-8").splitlines()]


def test_service_reconnect_reuses_only_same_module_context(runtime_case):
    case = runtime_case
    manifest_path = case.home / "modules/alpha/module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["services"]["observer"] = {
        "entry": ["bash", "service.sh"],
        "workspace": "default",
        "topic": "topic_alpha",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (case.home / "profiles/default/.env").write_text(
        """TELEGRAM_BOT_TOKEN=fixture-env-token
PYLON_CONTEXT=fixture-env-context
PYLON_TEST_TELEGRAM=http://example.invalid/
""",
        encoding="utf-8",
    )
    service_path = case.home / "modules/alpha/service.sh"
    service_path.write_text(
        """#!/bin/bash
set -eu
mkdir -p "$PYLON_HOME/data"
context=present
if [ -z "${PYLON_CONTEXT:-}" ] || [ "$PYLON_CONTEXT" = fixture-env-context ] || [ "$PYLON_CONTEXT" = fixture-parent-context ]; then context=missing; fi
if [ "${TELEGRAM_BOT_TOKEN+x}" = x ]; then bot_token=present; else bot_token=absent; fi
if [ "${PYLON_TEST_TELEGRAM+x}" = x ]; then test_endpoint=present; else test_endpoint=absent; fi
printf '%s,%s,%s,%s\\n' "$context" "$bot_token" "$test_endpoint" "$PYLON_MODULE_VERSION" >> "$PYLON_HOME/data/service-evidence"
""",
        encoding="utf-8",
    )

    case.gateway.start()
    first = _run_service(case)
    assert first.returncode == 0, first.stderr or first.stdout

    cached_path = case.home / "supervisor/service-alpha--observer.json"
    cached = json.loads(cached_path.read_text(encoding="utf-8"))
    old_token = cached["token"]
    old_module_hash = cached["module_hash"]
    assert stat.S_IMODE((case.home / "supervisor").stat().st_mode) == 0o700
    assert stat.S_IMODE(cached_path.stat().st_mode) == 0o600
    inspected = rpc_value(case.home, "context.inspect", context=old_token)
    assert inspected["module"] == "alpha"
    assert inspected["origin"] == "service:observer"
    assert _evidence(case.home) == [["present", "absent", "absent", old_module_hash]]

    case.gateway.stop()
    offline = _run_service(case)
    assert offline.returncode == 0, offline.stderr or offline.stdout
    assert _evidence(case.home) == [
        ["present", "absent", "absent", old_module_hash],
        ["present", "absent", "absent", old_module_hash],
    ]

    service_path.write_text(
        service_path.read_text(encoding="utf-8") + chr(10) + "# changed module source" + chr(10),
        encoding="utf-8",
    )
    changed_hash_offline = _run_service(case)
    assert changed_hash_offline.returncode != 0
    assert len(_evidence(case.home)) == 2

    case.gateway.start()
    reconnected = _run_service(case)
    assert reconnected.returncode == 0, reconnected.stderr or reconnected.stdout
    replacement = json.loads(cached_path.read_text(encoding="utf-8"))
    assert replacement["module_hash"] != old_module_hash
    assert_rpc_rejected(rpc(case.home, "context.inspect", context=old_token))
    replacement_context = rpc_value(
        case.home, "context.inspect", context=replacement["token"]
    )
    assert replacement_context["origin"] == "service:observer"
    assert _evidence(case.home) == [
        ["present", "absent", "absent", old_module_hash],
        ["present", "absent", "absent", old_module_hash],
        ["present", "absent", "absent", replacement["module_hash"]],
    ]

