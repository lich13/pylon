"""Run the complete deployment script against isolated binaries and service controls."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading

import pytest

from test_gateway_contract import ROOT

OLD = "a" * 40
NEW = "b" * 40


@pytest.fixture
def deployment():
    with tempfile.TemporaryDirectory(prefix="pylon-deploy-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        home, dist, commands = root / "home", root / "dist", root / "commands"
        for path in (home / "bin", home / "run", dist, commands, root / "tmp"):
            path.mkdir(parents=True, exist_ok=True)
        control = home / "fixture.json"
        control.write_text("{}")
        (home / "REVISION").write_text(OLD + "\n")

        def executable(path, body):
            path.write_text(f"#!{sys.executable}\n" + body)
            path.chmod(0o755)

        program = '''import json, pathlib, sys
VERSION = {version!r}
if sys.argv[1:] == ["--version"]:
    print("pylon", VERSION)
    sys.exit(0)
home = pathlib.Path(sys.argv[2])
command = sys.argv[3]
control = json.loads((home / "fixture.json").read_text())
new = VERSION == "0.5.13"
with (home / "calls").open("a") as log: log.write(VERSION + " " + command + "\\n")
freeze = home / "run/freeze"
if command == "freeze":
    freeze.touch()
elif command == "thaw":
    if control.get("thaw_remove_then_fail"): freeze.unlink()
    if control.get("thaw_fail") or control.get("thaw_remove_then_fail"): sys.exit(1)
    freeze.unlink()
elif command == "drain-status":
    if control.get("drain_busy"): sys.exit(1)
elif command == "diagnose":
    if new and control.get("health_fail"): sys.exit(1)
    version = "0.0.0" if new and control.get("wrong_version") else VERSION
    print(json.dumps({{"version":version,"maintenance":freeze.exists()}}))
elif command == "downgrade-check":
    switched = "v0.5.13-" in str((home / "bin/pylon").resolve())
    if switched and control.get("rollback_blocked"): sys.exit(1)
else:
    sys.exit(2)
'''
        old = home / "old/pylon"
        old.parent.mkdir()
        executable(old, program.format(version="0.5.12"))
        executable(dist / "pylon-linux-x86_64", program.format(version="0.5.13"))
        (home / "bin/pylon").symlink_to(old)
        (dist / "REVISION").write_text(NEW + "\n")
        digest = hashlib.sha256((dist / "pylon-linux-x86_64").read_bytes()).hexdigest()
        (dist / "SHA256SUMS").write_text(digest + "  dist/pylon-linux-x86_64\n")
        executable(commands / "id", 'print("0")\n')
        executable(commands / "install", '''import os, sys
args = []
it = iter(sys.argv[1:])
for argument in it:
    if argument in ("-o", "-g"): next(it)
    else: args.append(argument)
os.execv("/usr/bin/install", ["install", *args])
''')
        executable(commands / "systemctl", '''import json, os, pathlib, sys
home = pathlib.Path(os.environ["PYLON_DEPLOY_FIXTURE"])
control = json.loads((home / "fixture.json").read_text())
command = sys.argv[1]
with (home / "calls").open("a") as log: log.write("systemctl " + command + "\\n")
new = "v0.5.13-" in str((home / "bin/pylon").resolve())
if new and ((command == "start" and control.get("start_fail")) or (command == "is-active" and control.get("inactive"))): sys.exit(1)
''')
        stopping = threading.Event()
        listeners = []
        threads = []
        for name in ("executor", "broker"):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(home / f"run/{name}.sock"))
            listener.listen()
            listener.settimeout(0.1)
            listeners.append(listener)

            def accept(sock=listener):
                while not stopping.is_set():
                    try:
                        connection, _ = sock.accept()
                        connection.close()
                    except socket.timeout:
                        continue
                    except OSError:
                        return

            thread = threading.Thread(target=accept, daemon=True)
            thread.start()
            threads.append(thread)

        def run(**settings):
            control.write_text(json.dumps(settings))
            result = subprocess.run(
                ["bash", str(ROOT / "scripts/deploy-gateway.sh"), "--home", str(home),
                 "--release-dir", str(dist), "--drain-timeout", "1", "--health-timeout", "3"],
                env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                     "PYLON_DEPLOY_FIXTURE": str(home), "TMPDIR": str(root / "tmp")},
                capture_output=True, text=True, timeout=20,
            )
            summaries = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
            assert not list((root / "tmp").iterdir()), "private diagnostics must be removed"
            return result, summaries[-1] if summaries else None

        try:
            yield home, dist, old, run
        finally:
            stopping.set()
            for listener in listeners: listener.close()
            for thread in threads: thread.join(timeout=1)


def test_success_requires_new_version_and_thaws(deployment):
    home, _, old, run = deployment
    result, summary = run()
    assert result.returncode == 0, result.stderr
    assert summary["phase"] == "complete"
    assert summary["active_version"] == "0.5.13"
    assert summary["health"] == "target_healthy"
    assert not summary["freeze"]
    assert not (home / "run/freeze").exists()
    assert (home / "REVISION").read_text().strip() == NEW
    assert (home / "bin/pylon").resolve() != old
    assert old.exists()


@pytest.mark.parametrize("failure", ["health_fail", "wrong_version", "inactive", "start_fail", "thaw_fail", "thaw_remove_then_fail"])
def test_failed_cutover_restores_previous_binary_and_keeps_frozen(deployment, failure):
    home, _, old, run = deployment
    result, summary = run(**{failure: True})
    assert result.returncode != 0
    assert summary["phase"] == "failed"
    assert summary["rollback"] == "restored", (result.stderr, summary)
    assert summary["active_version"] == "0.5.12"
    assert summary["health"] == "previous_healthy"
    assert summary["freeze"]
    assert (home / "run/freeze").is_file()
    assert (home / "bin/pylon").resolve() == old
    assert (home / "REVISION").read_text().strip() == OLD
    assert (home / "calls").read_text().count("systemctl start") == 2


def test_drain_timeout_does_not_stop_services_or_switch(deployment):
    home, _, old, run = deployment
    result, summary = run(drain_busy=True)
    assert result.returncode != 0
    assert summary["rollback"] == "not_needed"
    assert summary["freeze"]
    assert (home / "bin/pylon").resolve() == old
    assert "systemctl stop" not in (home / "calls").read_text()


def test_unsafe_rollback_stays_frozen_and_reports_block(deployment):
    home, _, old, run = deployment
    result, summary = run(health_fail=True, rollback_blocked=True)
    assert result.returncode != 0
    assert summary["rollback"] == "blocked"
    assert summary["freeze"]
    assert (home / "bin/pylon").resolve() != old
    assert (home / "calls").read_text().count("systemctl start") == 1


def test_checksum_failure_does_not_execute_unverified_binary_or_freeze(deployment):
    home, dist, old, run = deployment
    with (dist / "pylon-linux-x86_64").open("a") as file:
        file.write("# fixture corruption\n")
    result, _ = run()
    assert result.returncode != 0
    assert not (home / "calls").exists()
    assert not (home / "run/freeze").exists()
    assert (home / "bin/pylon").resolve() == old
