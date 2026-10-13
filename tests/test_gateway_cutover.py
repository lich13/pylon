import hashlib
import json
import os
import sqlite3
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import deploy_gateway as gateway
from pylon.admin import chiphell
from pylon import bot_menu, module_store, module_units


REVISION = "a" * 40
PREVIOUS = "b" * 40


def _write_tree(root: Path, revision: str, marker: str) -> Path:
    stage = root / "gateway-releases" / revision
    (stage / "src" / "pylon").mkdir(parents=True)
    (stage / "ops").mkdir()
    (stage / "src" / "pylon" / "marker.txt").write_text(marker)
    (stage / "ops" / "marker.txt").write_text(marker)
    for name in ("jobs.json", "pyproject.toml", "requirements.lock", "README.md", "AGENTS.md"):
        (stage / name).write_text(f"{name}:{marker}\n")
    return stage


def _write_database(root: Path) -> Path:
    path = root / "data" / "outbox.sqlite3"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE updates (id INTEGER PRIMARY KEY, status TEXT, payload TEXT)")
        db.execute("INSERT INTO updates(status, payload) VALUES ('done', 'fixture')")
        db.commit()
    return path


def _write_profile_state(root: Path) -> None:
    profile = root / "profiles" / "webops"
    profile.mkdir(parents=True)
    with sqlite3.connect(profile / "module.sqlite3") as db:
        db.execute("CREATE TABLE profile_rows (id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO profile_rows(value) VALUES ('profile-db-secret')")
        db.commit()
    (profile / "state.json").write_text(json.dumps({"value": "profile-json-secret"}) + "\n")


@pytest.fixture
def cutover(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    chiphell_data = tmp_path / "chiphell-fid26-data"
    monkeypatch.setattr(chiphell, "DATA_ROOT", chiphell_data)
    monkeypatch.setattr(chiphell, "IMPORT_LOCK_PATH", chiphell_data / ".cookie-import.lock")
    monkeypatch.setattr(chiphell, "IMPORT_TRANSACTION_PATH", chiphell_data / ".cookie-import-transaction.json")
    stage = _write_tree(root, REVISION, "new")
    (root / "REVISION").write_text(PREVIOUS + "\n")
    (root / "jobs.json").write_text(json.dumps({"fixture": {}}) + "\n")
    _write_database(root)
    _write_profile_state(root)

    # Main is exercised with a temporary root, while root-only stage checks are
    # replaced because this test process is normally an unprivileged user.
    monkeypatch.setattr(gateway, "ROOT", root)
    monkeypatch.setattr(gateway, "PYTHON", tmp_path / "venv" / "bin" / "python")
    monkeypatch.setattr(gateway.os, "geteuid", lambda: 0)
    monkeypatch.setattr(gateway, "validate_stage", lambda _stage, _revision: None)

    commands = []
    active_units = {
        "pylon-job-fixture.timer",
        "pylon-job-external-digest.timer",
        "pylon-job-manual-only.timer",
        "pylon-bot.service",
        "pylon-market-web.service",
        "chiphell-fid26-poller.service",
        f"pylon-job-{chiphell.CRON_ID}.timer",
    }
    pending_values = []
    query_calls = []
    sleep_calls = []
    service_checks = []
    service_states = {
        unit: {
            "ActiveState": "active",
            "SubState": "running",
            "MainPID": str(1000 + index),
            "NRestarts": "0",
        }
        for index, unit in enumerate(sorted(active_units), start=1)
    }

    def fake_command(argv):
        command = tuple(str(value) for value in argv)
        commands.append(command)
        if command[:2] == ("systemctl", "show"):
            unit = command[2]
            service_checks.append(unit)
            return "\n".join(f"{key}={value}" for key, value in service_states[unit].items())
        return ""

    def fake_active(unit):
        return unit in active_units

    def fake_pending():
        if not pending_values:
            raise AssertionError("pending fixture exhausted")
        value = pending_values.pop(0)
        return value

    def fake_query(path, sql):
        query_calls.append((Path(path), sql))
        if Path(path).is_absolute() and Path(path).parts[:2] == ("/", "var"):
            raise AssertionError("fixture attempted to access /var")
        return []

    monkeypatch.setattr(gateway, "command", fake_command)
    monkeypatch.setattr(gateway, "active", fake_active)
    monkeypatch.setattr(gateway, "active_timers", lambda: sorted(
        unit for unit in active_units if unit.endswith(".timer")
    ))
    monkeypatch.setattr(gateway, "pending", fake_pending)
    monkeypatch.setattr(gateway, "query", fake_query)
    monkeypatch.setattr(gateway.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    old_files = {}
    for name in gateway.FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if name in {"src", "ops"}:
            target.mkdir(exist_ok=True)
            (target / "marker.txt").write_text("old")
        elif name == "jobs.json":
            target.write_text(json.dumps({"fixture": {}}) + "\n")
        else:
            target.write_text(f"{name}:old\n")
        old_files[name] = target.read_bytes() if target.is_file() else (target / "marker.txt").read_bytes()

    return {
        "root": root,
        "stage": stage,
        "commands": commands,
        "pending": pending_values,
        "query_calls": query_calls,
        "sleep_calls": sleep_calls,
        "service_checks": service_checks,
        "old_files": old_files,
        "database": root / "data" / "outbox.sqlite3",
        "active_timers": sorted(unit for unit in active_units if unit.endswith(".timer")),
    }


def _run(cutover, monkeypatch, modules=("itunes", "1")):
    args = ["deploy_gateway.py", "--stage", str(cutover["stage"]), "--revision", REVISION, "--modules", *modules]
    monkeypatch.setattr("sys.argv", args)
    gateway.main()


def _busy():
    return {
        "commands": 0,
        "agent_states": [("running", 1)],
        "services": ["pylon-task@fixture.service", "pylon-manual@fixture.service", "pylon-model@fixture.service"],
    }


def _quiet():
    return {"commands": 0, "agent_states": [], "services": []}


def test_active_timers_enumerates_external_units_and_deduplicates(monkeypatch):
    calls = []

    def listing(argv):
        calls.append(tuple(argv))
        return (
            "pylon-job-fixture.timer loaded active waiting Fixture timer\n"
            "  pylon-job-external_digest.timer loaded activating start External timer\n"
            "\n"
            "pylon-job-fixture.timer loaded active waiting Fixture timer\n"
        )

    monkeypatch.setattr(gateway, "command", listing)

    assert gateway.active_timers() == [
        "pylon-job-external_digest.timer", "pylon-job-fixture.timer"
    ]
    assert calls == [(
        "systemctl", "list-units", "pylon-job-*.timer", "--state=active,activating",
        "--plain", "--no-legend", "--no-pager",
    )]


@pytest.mark.parametrize("output", ["", "\n  \n\t\n"])
def test_active_timers_accepts_an_empty_listing(monkeypatch, output):
    monkeypatch.setattr(gateway, "command", lambda _argv: output)

    assert gateway.active_timers() == []


@pytest.mark.parametrize("unit", [
    "unrelated.timer", "pylon-job-fixture.service", "pylon-job-.timer",
    "pylon-job-../../fixture.timer", "pylon-job-fixture.timer;true",
    "pylon-job-fixture@one.timer", "--all", "\u25cf",
])
def test_active_timers_rejects_unexpected_unit_names(monkeypatch, unit):
    monkeypatch.setattr(gateway, "command", lambda _argv: (
        "pylon-job-valid.timer loaded active waiting Fixture\n"
        f"{unit} loaded active waiting Invalid fixture\n"
    ))

    with pytest.raises(ValueError, match="unexpected timer listing"):
        gateway.active_timers()


@pytest.mark.parametrize("outcome", ["success", "quiesce", "install", "startup"])
def test_cutover_stops_and_restores_all_active_timers_without_enabling_disabled(
    cutover, monkeypatch, outcome
):
    inactive = "pylon-job-disabled.timer"
    (cutover["root"] / "jobs.json").write_text(json.dumps({"fixture": {}, "disabled": {}}))
    timers = cutover["active_timers"]
    assert "pylon-job-external-digest.timer" in timers
    # A manually active but disabled timer must resume without being enabled.
    assert "pylon-job-manual-only.timer" in timers
    cutover["pending"].extend([_quiet(), _busy() if outcome == "quiesce" else _quiet()])
    command = gateway.command
    pending = gateway.pending
    failed = False

    def drain_after_timer_stop():
        assert ("systemctl", "stop", *timers) in cutover["commands"]
        return pending()

    def fail_once(argv):
        nonlocal failed
        value = tuple(str(arg) for arg in argv)
        trigger = (
            outcome == "install" and "pylon.module_cli" in value
        ) or (
            outcome == "startup" and value[:2] == ("systemctl", "start")
        )
        if trigger and not failed:
            failed = True
            raise RuntimeError(f"fixture {outcome} failure")
        return command(argv)

    monkeypatch.setattr(gateway, "pending", drain_after_timer_stop)
    monkeypatch.setattr(gateway, "command", fail_once)
    if outcome == "success":
        _run(cutover, monkeypatch)
    else:
        match = "new work started during quiesce" if outcome == "quiesce" else f"fixture {outcome} failure"
        with pytest.raises(RuntimeError, match=match):
            _run(cutover, monkeypatch)

    commands = cutover["commands"]
    assert commands.count(("systemctl", "stop", *timers)) == 1
    assert commands.count(("systemctl", "start", *timers)) == 1
    assert not any(inactive in command for command in commands)
    assert not any(command[:2] == ("systemctl", "enable") for command in commands)
    receipt = json.loads((cutover["stage"] / "cutover.json").read_text())
    assert receipt["timers"] == timers
    assert receipt["phase"] == ("complete" if outcome == "success" else "failed-old-restored")


@pytest.mark.parametrize("fail_start", [False, True], ids=["success", "rollback"])
def test_cutover_restores_services_and_timers_under_real_chiphell_lock(
    cutover, monkeypatch, fail_start
):
    cutover["pending"].extend([_quiet(), _quiet()])
    chiphell_timer = f"pylon-job-{chiphell.CRON_ID}.timer"
    original_lock = chiphell.cookie_import_lock
    lock_attempts = []
    reconciliations = []
    guarded_service_starts = []
    guarded_timer_starts = []
    controller_stop_attempts = []
    controller_timer_active = True
    failed = False

    @contextmanager
    def tracked_real_lock():
        attempt = {"outcome": "attempting"}
        lock_attempts.append(attempt)
        try:
            with original_lock():
                attempt["outcome"] = "acquired"
                yield
        except chiphell.CookieImportFailure as error:
            attempt["outcome"] = "busy"
            attempt["error"] = error.render()
            raise

    def controller_run(argv, *, check=True, capture=False):
        command = tuple(str(value) for value in argv)
        if command == ("systemctl", "is-active", "--quiet", chiphell_timer):
            return SimpleNamespace(returncode=0 if controller_timer_active else 3)
        if command == ("systemctl", "is-active", "--quiet", chiphell.SERVICE):
            return SimpleNamespace(returncode=0)
        if command == ("systemctl", "stop", chiphell.SERVICE):
            controller_stop_attempts.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(chiphell, "cookie_import_lock", tracked_real_lock)
    monkeypatch.setattr(chiphell, "run", controller_run)
    command = gateway.command

    def systemctl_with_controller(argv):
        nonlocal controller_timer_active, failed
        value = tuple(str(item) for item in argv)

        if value[:2] == ("systemctl", "stop") and chiphell_timer in value:
            controller_timer_active = False
        if value[:2] == ("systemctl", "start") and chiphell_timer in value:
            # The cutover guard remains active through timer restoration.
            with pytest.raises(chiphell.CookieImportFailure, match="REMOTE_VALIDATION_FAILED"):
                with original_lock():
                    pass
            guarded_timer_starts.append(value)
            controller_timer_active = True
        if value[:2] == ("systemctl", "start") and "pylon-bot.service" in value:
            # A separate file descriptor must fail to acquire the real lock
            # while services are being restored, in both success and rollback.
            with pytest.raises(chiphell.CookieImportFailure, match="REMOTE_VALIDATION_FAILED"):
                with original_lock():
                    pass
            guarded_service_starts.append(value)

        result = command(argv)
        if value[:2] == ("systemctl", "start") and "pylon-bot.service" in value:
            before = len(lock_attempts)
            chiphell.command_reconcile()
            assert len(lock_attempts) == before + 1
            attempt = lock_attempts[-1]
            assert attempt["outcome"] == "busy"
            assert "status=BUSY" in attempt["error"]
            reconciliations.append(value)
            if fail_start and not failed:
                failed = True
                raise RuntimeError("fixture bot startup failure")
        return result

    monkeypatch.setattr(gateway, "command", systemctl_with_controller)
    if fail_start:
        with pytest.raises(RuntimeError, match="fixture bot startup failure"):
            _run(cutover, monkeypatch)
    else:
        _run(cutover, monkeypatch)

    state = json.loads((cutover["stage"] / "cutover.json").read_text())
    assert state["phase"] == ("failed-old-restored" if fail_start else "complete")
    assert guarded_service_starts
    assert guarded_timer_starts
    assert len(reconciliations) == len(guarded_service_starts)
    assert controller_stop_attempts == []

    # Reconciliation can acquire the same lock and act normally after exit.
    controller_timer_active = False
    chiphell.command_reconcile()
    assert lock_attempts[-1]["outcome"] == "acquired"
    assert controller_stop_attempts == [("systemctl", "stop", chiphell.SERVICE)]


def test_control_guard_without_poller_does_not_acquire_chiphell_lock(monkeypatch, tmp_path):
    lock_path = tmp_path / "chiphell-fid26-data" / ".cookie-import.lock"
    monkeypatch.setattr(chiphell, "DATA_ROOT", lock_path.parent)
    monkeypatch.setattr(chiphell, "IMPORT_LOCK_PATH", lock_path)

    def unexpected_lock():
        raise AssertionError("gateway without the Chiphell poller must not acquire its lock")

    monkeypatch.setattr(chiphell, "cookie_import_lock", unexpected_lock)
    with gateway.control_guard(["pylon-bot.service"]):
        pass

    assert not lock_path.exists()


def test_waiting_work_is_drained_without_killing_task_manual_or_model(cutover, monkeypatch):
    cutover["pending"].extend([_busy(), _quiet(), _quiet()])
    _run(cutover, monkeypatch)

    preflight_index = next(index for index, command in enumerate(cutover["commands"])
                           if any("import setuptools" in token for token in command))
    timer_stop_index = next(index for index, command in enumerate(cutover["commands"])
                            if command[:2] == ("systemctl", "stop"))
    assert preflight_index < timer_stop_index
    assert not any(
        any(name in token for name in ("pylon-task@", "pylon-manual@", "pylon-model@", "kill"))
        for command in cutover["commands"]
        for token in command
    )
    assert cutover["sleep_calls"] == [5, 1, 1, 1, 1, 1]
    assert len(cutover["service_checks"]) == 18
    assert not cutover["query_calls"]


def test_new_work_in_quiesce_window_rejects_switch_and_restores_old_services(cutover, monkeypatch):
    cutover["pending"].extend([_quiet(), _busy()])

    with pytest.raises(RuntimeError, match="new work started during quiesce"):
        _run(cutover, monkeypatch)

    root = cutover["root"]
    assert (root / "REVISION").read_text() == PREVIOUS + "\n"
    state = json.loads((cutover["stage"] / "cutover.json").read_text())
    assert state["phase"] == "failed-old-restored"
    assert all(not (root / name).is_symlink() for name in gateway.FILES)
    assert any(command[:2] == ("systemctl", "stop") for command in cutover["commands"])
    assert any(command[:2] == ("systemctl", "start") for command in cutover["commands"])
    assert not any("kill" in token for command in cutover["commands"] for token in command)


def test_install_failure_restores_source_pointer_revision_and_data(cutover, monkeypatch):
    cutover["pending"].extend([_quiet(), _quiet()])
    before = gateway.storage_identity()
    original_command = gateway.command

    def fail_install(argv):
        command = tuple(str(value) for value in argv)
        cutover["commands"].append(command)
        if "pylon.module_cli" in command:
            raise RuntimeError("fixture install failed")
        return original_command(argv)

    monkeypatch.setattr(gateway, "command", fail_install)
    with pytest.raises(RuntimeError, match="fixture install failed"):
        _run(cutover, monkeypatch)

    root = cutover["root"]
    assert (root / "REVISION").read_text() == PREVIOUS + "\n"
    assert gateway.storage_identity() == before
    assert all(not (root / name).is_symlink() for name in gateway.FILES)
    for name, content in cutover["old_files"].items():
        target = root / name
        actual = target.read_bytes() if target.is_file() else (target / "marker.txt").read_bytes()
        assert actual == content
    state = json.loads((cutover["stage"] / "cutover.json").read_text())
    assert state["phase"] == "failed-old-restored"


def test_data_identity_is_hash_and_count_only_and_detects_fixture_change(cutover):
    before = gateway.data_identity()
    assert set(before) == {"updates"}
    assert set(before["updates"]) == {"rows", "sha256"}
    assert before["updates"]["rows"] == 1
    assert len(before["updates"]["sha256"]) == hashlib.sha256().digest_size * 2
    assert all(value not in json.dumps(before) for value in ("fixture", "done"))

    with sqlite3.connect(cutover["database"]) as db:
        db.execute("UPDATE updates SET payload = 'changed' WHERE id = 1")
        db.commit()
    after = gateway.data_identity()
    assert after["updates"]["rows"] == before["updates"]["rows"]
    assert after["updates"]["sha256"] != before["updates"]["sha256"]


def test_success_switches_sources_to_stage_and_preserves_data(cutover, monkeypatch):
    cutover["pending"].extend([_quiet(), _quiet()])
    before = gateway.storage_identity()
    _run(cutover, monkeypatch)

    root = cutover["root"]
    assert (root / "REVISION").read_text() == REVISION + "\n"
    assert gateway.storage_identity() == before
    assert all((root / name).is_symlink() for name in gateway.FILES)
    assert all((root / name).resolve() == cutover["stage"] / name for name in gateway.FILES)
    state = json.loads((cutover["stage"] / "cutover.json").read_text())
    assert state["phase"] == "complete"
    assert state["data_unchanged"] is True
    assert set(state["data_before"]) == {
        "data/outbox.sqlite3",
        "profiles/webops/module.sqlite3",
        "profiles/webops/state.json",
    }
    receipt = json.dumps(state["data_before"])
    assert "profile-db-secret" not in receipt
    assert "profile-json-secret" not in receipt
    runtime_import_index = next(
        index for index, command in enumerate(cutover["commands"])
        if command[:4] == ("/usr/sbin/runuser", "-u", "pylon", "--")
        and "import pylon.cli, pylon.sdk" in command
    )
    service_start_index = next(
        index for index, command in enumerate(cutover["commands"])
        if command[:2] == ("systemctl", "start")
        and any(unit in command for unit in (
            "pylon-bot.service",
            "pylon-market-web.service",
            "chiphell-fid26-poller.service",
        ))
    )
    assert runtime_import_index < service_start_index


def _link_previous_release(cutover, *, relative):
    root = cutover["root"]
    previous = root / "gateway-releases" / PREVIOUS
    previous.mkdir()
    links = {}
    for name in gateway.FILES:
        target = root / name
        target.rename(previous / name)
        destination = Path("gateway-releases") / PREVIOUS / name if relative else previous / name
        target.symlink_to(destination, target_is_directory=(previous / name).is_dir())
        links[name] = os.readlink(target)
    registry = root / "modules" / "registry.json"
    registry.parent.mkdir()
    original_registry = b'{"version":1,"modules":{"gmail":"releases/gmail/old"},"previous":{"gmail":"releases/gmail/older"}}\n'
    registry.write_bytes(original_registry)
    return previous, links, registry, original_registry


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-links", "relative-links"])
def test_linked_previous_release_can_switch_without_changing_its_snapshot(cutover, monkeypatch, relative):
    previous, _links, registry, _original_registry = _link_previous_release(cutover, relative=relative)
    cutover["pending"].extend([_quiet(), _quiet()])
    before = gateway.storage_identity()
    command = gateway.command
    installed_registry = b'{"version":1,"modules":{"gmail":"releases/gmail/new"},"previous":{"gmail":"releases/gmail/old"}}\n'

    def install(argv):
        if "pylon.module_cli" in argv:
            registry.write_bytes(installed_registry)
        return command(argv)

    monkeypatch.setattr(gateway, "command", install)
    _run(cutover, monkeypatch)

    assert registry.read_bytes() == installed_registry
    assert (cutover["root"] / "REVISION").read_text() == REVISION + "\n"
    assert gateway.storage_identity() == before
    for name, content in cutover["old_files"].items():
        target = previous / name
        assert (target.read_bytes() if target.is_file() else (target / "marker.txt").read_bytes()) == content
        assert (cutover["root"] / name).resolve() == cutover["stage"] / name
    assert json.loads((cutover["stage"] / "cutover.json").read_text())["phase"] == "complete"


@pytest.mark.parametrize("relative", [False, True], ids=["absolute-links", "relative-links"])
@pytest.mark.parametrize("failure", ["install", "startup"])
def test_linked_previous_failure_restores_exact_links_and_registry_before_services(cutover, monkeypatch, relative, failure):
    previous, links, registry, original_registry = _link_previous_release(cutover, relative=relative)
    cutover["pending"].extend([_quiet(), _quiet()])
    root = cutover["root"]
    before = gateway.storage_identity()
    command = gateway.command
    failed = False
    restore_starts = []

    def fail_once(argv):
        nonlocal failed
        value = tuple(str(arg) for arg in argv)
        if "pylon.module_cli" in value:
            registry.write_text('{"modules":{"gmail":"releases/gmail/new"}}\n')
            if failure == "install":
                failed = True
                raise RuntimeError("fixture linked install failed")
        if value[:2] == ("systemctl", "start"):
            if failure == "startup" and not failed:
                assert (root / "REVISION").read_text() == REVISION + "\n"
                failed = True
                raise RuntimeError("fixture linked startup failed")
            if failed:
                assert registry.read_bytes() == original_registry
                assert {name: os.readlink(root / name) for name in gateway.FILES} == links
                assert (root / "REVISION").read_text() == PREVIOUS + "\n"
                restore_starts.append(value)
        return command(argv)

    monkeypatch.setattr(gateway, "command", fail_once)
    with pytest.raises(RuntimeError, match=f"fixture linked {failure} failed"):
        _run(cutover, monkeypatch)

    assert restore_starts
    assert registry.read_bytes() == original_registry
    assert not registry.with_suffix(".rollback").exists()
    assert gateway.storage_identity() == before
    for name, content in cutover["old_files"].items():
        assert (root / name).resolve() == previous / name
        target = previous / name
        assert (target.read_bytes() if target.is_file() else (target / "marker.txt").read_bytes()) == content
    state = json.loads((cutover["stage"] / "cutover.json").read_text())
    assert state["phase"] == "failed-old-restored"
    assert state["previous"] == PREVIOUS


@pytest.mark.parametrize("changed_state", [
    "ActiveState=active\nSubState=running\nMainPID=100\nNRestarts=1",
    "ActiveState=active\nSubState=running\nMainPID=200\nNRestarts=0",
])
def test_ready_services_rejects_restart_or_pid_change(monkeypatch, changed_state):
    outputs = iter([
        "ActiveState=active\nSubState=running\nMainPID=100\nNRestarts=0",
        changed_state,
    ])
    monkeypatch.setattr(gateway, "command", lambda _argv: next(outputs))
    monkeypatch.setattr(gateway.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="service restarted during startup"):
        gateway.ready_services(["pylon-bot.service"])


def test_ready_services_waits_for_a_cold_start_to_become_stable(monkeypatch):
    unit = "pylon-bot.service"
    starting = "ActiveState=activating\nSubState=start\nMainPID=0\nNRestarts=0"
    running = "ActiveState=active\nSubState=running\nMainPID=100\nNRestarts=0"
    outputs = iter([starting, starting, *([running] * 6)])
    checks = []
    sleeps = []

    def fake_command(argv):
        checks.append(tuple(argv))
        return next(outputs)

    monkeypatch.setattr(gateway, "command", fake_command)
    monkeypatch.setattr(gateway.time, "sleep", sleeps.append)

    gateway.ready_services([unit])

    assert len(checks) == 8
    assert sleeps == [1] * 7


def test_ready_services_waits_for_each_service_to_stabilize(monkeypatch):
    first = "pylon-bot.service"
    delayed = "pylon-market-web.service"
    checks = {first: 0, delayed: 0}
    sleeps = []

    def fake_command(argv):
        unit = argv[2]
        checks[unit] += 1
        if unit == delayed and checks[unit] <= 7:
            return "ActiveState=activating\nSubState=start\nMainPID=0\nNRestarts=0"
        pid = "100" if unit == first else "200"
        return f"ActiveState=active\nSubState=running\nMainPID={pid}\nNRestarts=0"

    monkeypatch.setattr(gateway, "command", fake_command)
    monkeypatch.setattr(gateway.time, "sleep", sleeps.append)

    gateway.ready_services([first, delayed])

    # The first service is ready immediately, but the six-round stable window
    # starts only after the delayed service is also running.
    assert checks == {first: 13, delayed: 13}
    assert sleeps == [1] * 12


def test_ready_services_timeout_includes_last_observed_service_state(monkeypatch):
    unit = "pylon-bot.service"
    state = "ActiveState=activating\nSubState=start\nMainPID=0\nNRestarts=0"
    checks = []
    sleeps = []

    def fake_command(argv):
        checks.append(tuple(argv))
        return state

    monkeypatch.setattr(gateway, "command", fake_command)
    monkeypatch.setattr(gateway.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="service startup did not settle") as error:
        gateway.ready_services([unit])

    assert len(checks) == 36
    assert sleeps == [1] * 35
    assert '"ActiveState": "activating"' in str(error.value)
    assert '"SubState": "start"' in str(error.value)
    assert '"MainPID": "0"' in str(error.value)


@pytest.mark.parametrize(
    "state",
    [
        "ActiveState=failed\nSubState=failed\nMainPID=0\nNRestarts=0",
        "ActiveState=activating\nSubState=auto-restart\nMainPID=0\nNRestarts=1",
    ],
    ids=["failed", "auto-restart"],
)
def test_ready_services_rejects_failed_or_auto_restart_state(monkeypatch, state):
    unit = "pylon-bot.service"

    def fake_command(argv):
        return state

    monkeypatch.setattr(gateway, "command", fake_command)

    with pytest.raises(RuntimeError, match="service did not stay running") as error:
        gateway.ready_services([unit])

    assert unit in str(error.value)
    for field in state.splitlines():
        key, value = field.split("=", 1)
        assert f'"{key}": "{value}"' in str(error.value)


def test_ready_services_rejects_a_service_that_drops_after_becoming_healthy(monkeypatch):
    outputs = iter([
        "ActiveState=active\nSubState=running\nMainPID=100\nNRestarts=0",
        "ActiveState=activating\nSubState=start\nMainPID=0\nNRestarts=0",
    ])
    checks = []
    sleeps = []

    def fake_command(argv):
        checks.append(tuple(argv))
        return next(outputs)

    monkeypatch.setattr(gateway, "command", fake_command)
    monkeypatch.setattr(gateway.time, "sleep", sleeps.append)

    with pytest.raises(RuntimeError, match="service did not stay running"):
        gateway.ready_services(["pylon-bot.service"])

    assert len(checks) == 2
    assert sleeps == [1]


def test_command_umask_keeps_generated_package_paths_and_files_readable(tmp_path):
    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    script = (
        "from pathlib import Path\n"
        f"site = Path({str(site)!r})\n"
        "dist = site / 'pylon_gateway-1.0.dist-info'\n"
        "package = site / 'pylon'\n"
        "site.mkdir(parents=True)\n"
        "dist.mkdir()\n"
        "package.mkdir()\n"
        "(site / 'pylon_gateway.pth').write_text('pylon\\n')\n"
        "(dist / 'METADATA').write_text('Name: pylon-gateway\\n')\n"
        "(package / '__init__.py').write_text('')\n"
    )

    previous_umask = os.umask(0o077)
    try:
        gateway.command([sys.executable, "-c", script])
    finally:
        os.umask(previous_umask)

    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o755
        for path in (site, site / "pylon_gateway-1.0.dist-info", site / "pylon")
    )
    assert all(
        stat.S_IMODE(path.stat().st_mode) == 0o644
        for path in (
            site / "pylon_gateway.pth",
            site / "pylon_gateway-1.0.dist-info" / "METADATA",
            site / "pylon" / "__init__.py",
        )
    )


def test_schedule_failure_restores_registry_before_restarting_old_timer(monkeypatch, tmp_path):
    timer = "pylon-job-fixture.timer"
    job = {"name": "fixture", "schedule": "*/5 * * * *", "lock": "fixture-lock"}
    old = {"demo": SimpleNamespace(manifest={"jobs": {"fixture": {**job, "name": "old"}}})}
    proposed = {"demo": SimpleNamespace(manifest={"jobs": {"fixture": {**job, "name": "new"}}})}
    events = []

    monkeypatch.setattr(module_store, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(module_units, "paths", lambda _key, _job: {})

    def fake_run(argv, **_kwargs):
        event = ("run", *argv)
        events.append(event)
        if argv[1] == "start" and argv[-1] == timer:
            raise RuntimeError("fixture timer start failed")
        if argv[1] == "show":
            stdout = "active\n"
        elif argv[1] == "is-enabled":
            stdout = "enabled\n"
        else:
            stdout = ""
        return SimpleNamespace(stdout=stdout, stderr="", returncode=0)

    monkeypatch.setattr(module_store.subprocess, "run", fake_run)
    restored = []

    def restore():
        events.append(("restore",))
        restored.append("registry")

    with pytest.raises(RuntimeError, match="fixture timer start failed"):
        with module_store.schedules(old, proposed, ["demo"], restore=restore):
            pass

    assert restored == ["registry"]
    restore_index = events.index(("restore",))
    stop_indices = [
        index for index, event in enumerate(events)
        if event[1:] == ("systemctl", "stop", timer)
    ]
    start_indices = [
        index for index, event in enumerate(events)
        if event[1:] == ("systemctl", "start", timer)
    ]
    assert len(stop_indices) >= 2
    assert len(start_indices) >= 2
    assert stop_indices[-1] < restore_index < start_indices[-1]


def test_module_install_umask077_keeps_release_and_registry_readable(monkeypatch, tmp_path):
    monkeypatch.setenv("PYLON_HOME", str(tmp_path / "home"))
    source = tmp_path / "module"
    namespace = source / "src" / "pylon_tasks" / "demo"
    namespace.mkdir(parents=True)
    (source / "module.json").write_text(json.dumps({
        "id": "demo",
        "api_version": 1,
        "data_version": 1,
        "handler": "pylon_tasks.demo.handler",
        "packages": ["pylon_tasks.demo"],
    }))
    (namespace / "handler.py").write_text(
        "from pylon.sdk import serve\n"
        "def handle(action, profile, payload): return {'status': 'ok'}\n"
        "if __name__ == '__main__': serve(handle)\n"
    )
    archive = tmp_path / "demo.tar.gz"
    receipt = module_store.pack(source, archive)
    destination = tmp_path / "installed"

    previous_umask = os.umask(0o077)
    try:
        result = module_store.install([(archive, receipt["archive_sha256"])], destination, services=False)
    finally:
        os.umask(previous_umask)

    assert result["restarted"] == []
    index = json.loads((destination / "registry.json").read_text())
    release = destination / index["modules"]["demo"]
    readable_dirs = [destination, destination / "releases", release.parent, release]
    readable_files = [destination / ".leases.lock", destination / "registry.json", release / "module.json",
                      release / "src" / "pylon_tasks" / "demo" / "handler.py"]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o755 for path in readable_dirs)
    assert all(stat.S_IMODE(path.stat().st_mode) & 0o444 == 0o444 for path in readable_files)


def test_menu_watcher_syncs_once_per_registration_and_stops_without_network(monkeypatch):
    stamps = iter([("v1",), ("v1",), ("v2",), ("v2",)])
    sync_calls = []

    class Stop:
        def __init__(self):
            self.waits = []
            self.stopped = False

        def is_set(self):
            return self.stopped

        def wait(self, seconds):
            self.waits.append(seconds)
            if len(self.waits) >= 3:
                self.stopped = True
                return True
            return False

    stop = Stop()
    monkeypatch.setattr(bot_menu, "registration_stamp", lambda: next(stamps, ("v2",)))
    monkeypatch.setattr(bot_menu, "synchronize_background", lambda event: sync_calls.append(event))

    bot_menu.watch_registration(stop)

    assert sync_calls == [stop, stop]
    assert stop.waits == [5, 5, 5]
    assert stop.stopped is True
