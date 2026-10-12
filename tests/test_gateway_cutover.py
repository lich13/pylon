import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops import deploy_gateway as gateway
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
        "pylon-bot.service",
        "pylon-market-web.service",
        "chiphell-fid26-poller.service",
    }
    pending_values = []
    query_calls = []
    sleep_calls = []

    def fake_command(argv):
        command = tuple(str(value) for value in argv)
        commands.append(command)
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
        "old_files": old_files,
        "database": root / "data" / "outbox.sqlite3",
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
    assert cutover["sleep_calls"] == [5]
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
