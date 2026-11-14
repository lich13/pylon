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
new = VERSION == "0.5.15"
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
    switched = "v0.5.15-" in str((home / "bin/pylon").resolve())
    if switched and control.get("rollback_blocked"): sys.exit(1)
else:
    sys.exit(2)
'''
        old = home / "old/pylon"
        old.parent.mkdir()
        executable(old, program.format(version="0.5.14"))
        executable(dist / "pylon-linux-x86_64", program.format(version="0.5.15"))
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
with (home / "calls").open("a") as log: log.write("systemctl " + " ".join(sys.argv[1:]) + "\\n")
new = "v0.5.15-" in str((home / "bin/pylon").resolve())
if new and ((command == "start" and control.get("start_fail")) or (command == "is-active" and control.get("inactive"))): sys.exit(1)
if command == "restart":
    if control.get("restart_fail"): sys.exit(1)
    proc = pathlib.Path(os.environ.get("PYLON_DEPLOY_PROC") or (home / "proc"))
    target = (home / "bin/pylon").resolve()
    units = sys.argv[2:]
    if not proc.is_dir():
        sys.exit(0)
    for pid in proc.iterdir():
        cgroup = pid / "cgroup"
        if not cgroup.is_file():
            continue
        text = cgroup.read_text()
        if any(unit in text for unit in units):
            exe = pid / "exe"
            if exe.exists() or exe.is_symlink(): exe.unlink()
            exe.symlink_to(target)
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

        def run(keep_frozen=False, script=None, timeout=20, **settings):
            control.write_text(json.dumps(settings))
            result = subprocess.run(
                ["bash", str(script or ROOT / "scripts/deploy-gateway.sh"), "--home", str(home),
                 "--release-dir", str(dist), "--drain-timeout", "1", "--health-timeout", "3",
                 *(["--keep-frozen"] if keep_frozen else [])],
                env={**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                     "PYLON_DEPLOY_FIXTURE": str(home), "TMPDIR": str(root / "tmp"),
                     "PYLON_DEPLOY_PROC": str(home / "proc")},
                capture_output=True, text=True, timeout=timeout,
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
    assert summary["active_version"] == "0.5.15"
    assert summary["health"] == "target_healthy"
    assert not summary["freeze"]
    assert not (home / "run/freeze").exists()
    assert (home / "REVISION").read_text().strip() == NEW
    assert (home / "bin/pylon").resolve() != old
    assert old.exists()
    # The previous binary lives outside releases/, so release pruning is skipped.
    assert summary["prune"]["releases"]["status"] == "skipped_unresolved"
    assert summary["prune"]["backups"]["status"] == "ok"
    assert summary["migration"]["status"] in ("ok", "deferred")
    assert summary["migration"]["restarted"] == []


def test_coordinated_upgrade_keeps_frozen_until_external_health_passes(deployment):
    home, _, _, run = deployment
    result, summary = run(keep_frozen=True)
    assert result.returncode == 0
    assert summary["phase"] == "complete" and summary["freeze"]
    assert (home / "run/freeze").is_file()
    assert "thaw" not in (home / "calls").read_text()


@pytest.mark.parametrize("failure", ["health_fail", "wrong_version", "inactive", "start_fail", "thaw_fail", "thaw_remove_then_fail"])
def test_failed_cutover_restores_previous_binary_and_keeps_frozen(deployment, failure):
    home, _, old, run = deployment
    result, summary = run(**{failure: True})
    assert result.returncode != 0
    assert summary["phase"] == "failed"
    assert summary["rollback"] == "restored", (result.stderr, summary)
    assert summary["prune"] == {"status": "not_run"}
    assert summary["migration"] == {"status": "not_run"}
    assert summary["active_version"] == "0.5.14"
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



def _proc(home, pid, exe, unit):
    directory = home / "proc" / str(pid)
    directory.mkdir(parents=True)
    (directory / "exe").symlink_to(exe)
    (directory / "cgroup").write_text(f"0::/system.slice/{unit}\n")


def test_leftover_module_units_are_restarted_and_oneshot_holds_are_kept(deployment):
    import shutil

    home, dist, old, run = deployment
    stale_rev = "c" * 40
    previous_dir = home / f"releases/v0.5.14-{OLD}" / "dist"
    stale_dir = home / f"releases/v0.5.13-{stale_rev}" / "dist"
    for directory in (previous_dir, stale_dir):
        directory.mkdir(parents=True)
        shutil.copy(old, directory / "pylon-linux-x86_64")
    (home / "bin/pylon").unlink()
    (home / "bin/pylon").symlink_to(previous_dir / "pylon-linux-x86_64")
    _proc(home, 41, previous_dir / "pylon-linux-x86_64", "pylon-module@gmail--relay.service")
    _proc(home, 42, stale_dir / "pylon-linux-x86_64", "pylon-task@sync.service")
    result, summary = run()
    assert result.returncode == 0, result.stderr
    assert summary["phase"] == "complete"
    assert summary["health"] == "target_healthy"
    assert not summary["freeze"]
    assert summary["migration"]["status"] == "ok"
    assert summary["migration"]["restarted"] == ["pylon-module@gmail--relay.service"]
    assert "pylon-task@sync.service" in summary["migration"]["held_units"]
    assert f"v0.5.13-{stale_rev}" in summary["migration"]["held"]
    assert "systemctl restart pylon-module@gmail--relay.service" in (home / "calls").read_text()
    assert "pylon-task@sync.service" not in (home / "calls").read_text()
    assert (home / "proc/41/exe").resolve() != old.resolve()
    assert "v0.5.15-" in str((home / "proc/41/exe").resolve())
    assert (home / "proc/42/exe").resolve() == (stale_dir / "pylon-linux-x86_64").resolve()
    assert summary["prune"]["releases"]["status"] == "ok"
    assert f"v0.5.13-{stale_rev}" in summary["prune"]["releases"]["kept"]
    assert f"v0.5.13-{stale_rev}" in summary["prune"]["releases"]["held"]
    assert (home / f"releases/v0.5.13-{stale_rev}").is_dir()
    assert (home / f"releases/v0.5.14-{OLD}").is_dir()
    assert (home / f"releases/v0.5.15-{NEW}").is_dir()


def test_keep_frozen_defers_module_restart_and_keeps_occupied_release(deployment):
    import shutil

    home, dist, old, run = deployment
    stale_rev = "c" * 40
    previous_dir = home / f"releases/v0.5.14-{OLD}" / "dist"
    stale_dir = home / f"releases/v0.5.13-{stale_rev}" / "dist"
    for directory in (previous_dir, stale_dir):
        directory.mkdir(parents=True)
        shutil.copy(old, directory / "pylon-linux-x86_64")
    (home / "bin/pylon").unlink()
    (home / "bin/pylon").symlink_to(previous_dir / "pylon-linux-x86_64")
    _proc(home, 51, stale_dir / "pylon-linux-x86_64", "pylon-module@gmail--relay.service")
    result, summary = run(keep_frozen=True)
    assert result.returncode == 0, result.stderr
    assert summary["phase"] == "complete" and summary["freeze"]
    assert summary["migration"]["status"] == "deferred"
    assert summary["migration"]["restarted"] == []
    assert "pylon-module@gmail--relay.service" in summary["migration"]["deferred"]
    assert "systemctl restart" not in (home / "calls").read_text()
    assert (home / "proc/51/exe").resolve() == (stale_dir / "pylon-linux-x86_64").resolve()
    assert f"v0.5.13-{stale_rev}" in summary["prune"]["releases"]["kept"]
    assert (home / f"releases/v0.5.13-{stale_rev}").is_dir()


def test_restart_failure_keeps_release_and_does_not_fail_cutover(deployment):
    import shutil

    home, dist, old, run = deployment
    stale_rev = "c" * 40
    previous_dir = home / f"releases/v0.5.14-{OLD}" / "dist"
    stale_dir = home / f"releases/v0.5.13-{stale_rev}" / "dist"
    for directory in (previous_dir, stale_dir):
        directory.mkdir(parents=True)
        shutil.copy(old, directory / "pylon-linux-x86_64")
    (home / "bin/pylon").unlink()
    (home / "bin/pylon").symlink_to(previous_dir / "pylon-linux-x86_64")
    _proc(home, 61, stale_dir / "pylon-linux-x86_64", "pylon-module@itunes--relay.service")
    result, summary = run(restart_fail=True)
    assert result.returncode == 0, result.stderr
    assert summary["phase"] == "complete"
    assert summary["health"] == "target_healthy"
    assert not summary["freeze"]
    assert summary["migration"]["status"] == "partial"
    assert summary["migration"]["restarted"] == []
    assert summary["migration"]["failed"] == [
        {"error": "restart_failed", "unit": "pylon-module@itunes--relay.service"}
    ]
    assert f"v0.5.13-{stale_rev}" in summary["migration"]["held"]
    assert (home / "proc/61/exe").resolve() == (stale_dir / "pylon-linux-x86_64").resolve()
    assert (home / f"releases/v0.5.13-{stale_rev}").is_dir()
    assert f"v0.5.13-{stale_rev}" in summary["prune"]["releases"]["kept"]


def test_prune_failure_does_not_reverse_healthy_cutover(deployment, tmp_path):
    home, dist, old, run = deployment
    script_dir = tmp_path / "scripts"
    script_dir.mkdir()
    script = script_dir / "deploy-gateway.sh"
    script.write_bytes((ROOT / "scripts/deploy-gateway.sh").read_bytes())
    script.chmod(0o755)
    (script_dir / "gateway-prune.py").write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if '--migrate' in sys.argv:\n"
        "    print(json.dumps({'status':'ok','restarted':[],'failed':[],'deferred':[],"
        "'held':[],'held_units':[],'keep':[]}))\n"
        "    sys.exit(0)\n"
        "print(json.dumps({'dry_run':False,'releases':{'status':'failed','error':'OSError'},"
        "'backups':{'status':'failed','error':'OSError'}}))\n"
        "sys.exit(1)\n"
    )
    (script_dir / "gateway-prune.py").chmod(0o755)
    result, summary = run(script=script)
    assert result.returncode == 0, result.stderr
    assert summary["phase"] == "complete"
    assert summary["health"] == "target_healthy"
    assert summary["rollback"] == "not_needed"
    assert not summary["freeze"]
    assert (home / "bin/pylon").resolve() != old
    assert summary["prune"]["releases"]["status"] == "failed"
    assert not (home / "run/freeze").exists()
