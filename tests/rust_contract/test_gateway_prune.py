"""Gateway release and backup retention after a healthy deploy."""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys

from test_gateway_contract import ROOT

SCRIPT = ROOT / "scripts/gateway-prune.py"
NOW = dt.datetime(2026, 10, 31, tzinfo=dt.timezone.utc).timestamp()


def sha(n: int) -> str:
    return f"{n:040x}"


def layout(tmp_path: Path):
    home = tmp_path / "home"
    releases, backups, units = home / "releases", home / "backups", tmp_path / "units"
    for path in (home / "bin", releases, backups, units):
        path.mkdir(parents=True)
    names = {
        "old": f"v0.5.20-{sha(1)}", "previous": f"v0.5.22-{sha(2)}", "current": f"v0.5.23-{sha(3)}",
        "stability": f"stability-{sha(4)}", "hardening": f"hardening-{sha(5)}",
        "unit": f"v0.5.9-{sha(6)}", "linked": f"stability-{sha(7)}",
    }
    for name in names.values():
        (releases / name / "dist").mkdir(parents=True)
        (releases / name / "dist/pylon-linux-x86_64").write_text("bin")
    for other in ("external-abc", "privileged-abc", "v0.5.x-notours", "rust-releases"):
        (releases / other).mkdir()
    (releases / f"v0.5.1-{sha(8)}").symlink_to(releases / "external-abc")
    (home / "bin/pylon").symlink_to(releases / names["current"] / "dist/pylon-linux-x86_64")
    (home / "venv").symlink_to(releases / names["linked"])
    (units / "pylon-x.service").write_text(f"ExecStart={releases}/{names['unit']}/dist/pylon-linux-x86_64\n")
    for stamp in ("20261001T000000Z-aaa", "20261016T195625Z-bbb-retry", "20261020T000000Z", "20261030T000000Z-ccc"):
        (backups / f"gateway-{stamp}").mkdir()
    for other in ("capabilities", "vacuum-20260101T000000Z", "nginx"):
        (backups / other).mkdir()
    return home, releases, backups, units, names


def run(home, units, *args, proc=None, check=True):
    proc = Path(proc) if proc is not None else home / "proc"
    proc.mkdir(exist_ok=True)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--home", str(home), "--systemd-dir", str(units),
         "--proc-dir", str(proc), "--now", str(NOW), *args], capture_output=True, text=True, timeout=30)
    if check:
        assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def write_proc(proc, pid, exe, unit=None, cwd=None):
    directory = proc / str(pid)
    directory.mkdir(parents=True)
    (directory / "exe").symlink_to(exe)
    if cwd is not None:
        (directory / "cwd").symlink_to(cwd)
    if unit is not None:
        (directory / "cgroup").write_text(f"0::/system.slice/{unit}\n")


def test_keeps_current_previous_and_referenced_releases_only(tmp_path):
    home, releases, backups, units, names = layout(tmp_path)
    before = sorted(p.name for p in releases.iterdir())
    previous = releases / names["previous"] / "dist/pylon-linux-x86_64"
    dry = run(home, units, "--keep", str(previous), "--dry-run")
    assert sorted(p.name for p in releases.iterdir()) == before, "dry run deletes nothing"
    result = run(home, units, "--keep", str(previous))
    assert dry["releases"]["deleted"] == result["releases"]["deleted"]
    assert sorted(result["releases"]["deleted"]) == sorted([names["old"], names["stability"], names["hardening"]])
    left = {p.name for p in releases.iterdir()}
    for kept in ("previous", "current", "unit", "linked"):
        assert names[kept] in left
    assert {"external-abc", "privileged-abc", "v0.5.x-notours", "rust-releases", f"v0.5.1-{sha(8)}"} <= left
    assert (releases / "external-abc").is_dir(), "symlinked candidates never follow their target"


def test_backups_keep_fourteen_days_and_always_the_newest(tmp_path):
    home, _, backups, units, names = layout(tmp_path)
    result = run(home, units, "--keep", str(home / "releases" / names["previous"]))
    assert sorted(result["backups"]["deleted"]) == ["gateway-20261001T000000Z-aaa", "gateway-20261016T195625Z-bbb-retry"]
    assert {p.name for p in backups.iterdir()} == {
        "gateway-20261020T000000Z", "gateway-20261030T000000Z-ccc", "capabilities", "vacuum-20260101T000000Z", "nginx"}
    result = run(home, units, "--keep", str(home / "releases" / names["previous"]), "--backup-days", "1")
    assert result["backups"]["deleted"] == ["gateway-20261020T000000Z"]
    assert result["backups"]["kept"] == ["gateway-20261030T000000Z-ccc"]


def test_newest_backup_survives_even_when_expired(tmp_path):
    home, _, backups, units, names = layout(tmp_path)
    for path in backups.glob("gateway-*"):
        os.rmdir(path)
    (backups / "gateway-20250101T000000Z").mkdir()
    result = run(home, units, "--keep", str(home / "releases" / names["previous"]))
    assert result["backups"] == {"status": "ok", "deleted": [], "kept": ["gateway-20250101T000000Z"]}


def test_unresolved_previous_release_skips_release_pruning(tmp_path):
    home, releases, _, units, _ = layout(tmp_path)
    before = sorted(p.name for p in releases.iterdir())
    result = run(home, units, "--keep", str(tmp_path / "elsewhere/pylon"))
    assert result["releases"]["status"] == "skipped_unresolved"
    assert sorted(p.name for p in releases.iterdir()) == before


def test_operator_backups_are_dated_by_name_not_mtime(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gateway_prune", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home, _, backups, units, names = layout(tmp_path)
    old = backups / "gateway-ops-20261001T000000Z-bots-remove"
    recent = backups / "gateway-ops-20261030T120000Z-routes-set"
    for path in (old, recent):
        path.mkdir()
    expected = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc).timestamp()
    assert module.backup_time(old) == expected
    assert module.backup_time(recent) == dt.datetime(2026, 10, 30, 12, tzinfo=dt.timezone.utc).timestamp()
    result = run(home, units, "--keep", str(home / "releases" / names["previous"]))
    assert "gateway-ops-20261001T000000Z-bots-remove" in result["backups"]["deleted"]
    assert "gateway-ops-20261030T120000Z-routes-set" in result["backups"]["kept"]
    assert not old.exists() and recent.exists()



def test_process_exe_and_cwd_references_are_kept(tmp_path):
    home, releases, _, units, names = layout(tmp_path)
    proc = home / "proc"
    write_proc(proc, 11, releases / names["old"] / "dist/pylon-linux-x86_64",
               unit="pylon-module@gmail--relay.service")
    write_proc(proc, 12, tmp_path / "other-bin", cwd=releases / names["stability"])
    (tmp_path / "other-bin").write_text("x")
    previous = releases / names["previous"] / "dist/pylon-linux-x86_64"
    result = run(home, units, "--keep", str(previous), proc=proc)
    assert result["releases"]["status"] == "ok"
    assert names["old"] in result["releases"]["kept"]
    assert names["stability"] in result["releases"]["kept"]
    assert names["old"] in result["releases"]["held"]
    assert names["stability"] in result["releases"]["held"]
    assert names["hardening"] in result["releases"]["deleted"]
    assert (releases / names["old"]).is_dir()
    assert (releases / names["stability"]).is_dir()
    assert not (releases / names["hardening"]).exists()


def test_deleted_exe_suffix_still_identifies_a_release(tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gateway_prune", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home, releases, _, _, names = layout(tmp_path)
    held = releases / names["old"] / "dist/pylon-linux-x86_64"
    assert module.release_of(Path(f"{held} (deleted)"), releases) == names["old"]


def _fake_systemctl(path, home, fail=None):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        f"home = pathlib.Path({str(home)!r})\n"
        "command, *units = sys.argv[1:]\n"
        "with (home / 'calls').open('a') as log: log.write('systemctl ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        f"if command == 'restart' and {fail!r} in units: sys.exit(1)\n"
        "proc = pathlib.Path(os.environ['PYLON_DEPLOY_PROC'])\n"
        "target = (home / 'bin/pylon').resolve()\n"
        "for pid in proc.iterdir():\n"
        "    cgroup = pid / 'cgroup'\n"
        "    if not cgroup.is_file():\n"
        "        continue\n"
        "    text = cgroup.read_text()\n"
        "    if command == 'restart' and any(unit in text for unit in units):\n"
        "        exe = pid / 'exe'\n"
        "        if exe.exists() or exe.is_symlink(): exe.unlink()\n"
        "        exe.symlink_to(target)\n"
    )
    path.chmod(0o755)


def test_migrate_restarts_only_module_units_and_keeps_oneshot_holds(tmp_path):
    home, releases, _, units, names = layout(tmp_path)
    proc = home / "proc"
    current = releases / names["current"] / "dist/pylon-linux-x86_64"
    previous = releases / names["previous"] / "dist/pylon-linux-x86_64"
    stale = releases / names["old"] / "dist/pylon-linux-x86_64"
    write_proc(proc, 21, previous, unit="pylon-module@gmail--relay.service")
    write_proc(proc, 22, stale, unit="pylon-task@sync.service")
    write_proc(proc, 23, current, unit="pylon-module@operations--relay.service")
    write_proc(proc, 24, stale, unit="pylon-manual@sync.service")
    commands = tmp_path / "bin"
    commands.mkdir()
    _fake_systemctl(commands / "systemctl", home)
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "PYLON_DEPLOY_PROC": str(proc)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--migrate", "--restart", "--home", str(home),
         "--target-binary", str(current), "--previous-binary", str(previous),
         "--proc-dir", str(proc)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "ok"
    assert data["restarted"] == ["pylon-module@gmail--relay.service"]
    assert data["failed"] == []
    assert "pylon-task@sync.service" in data["held_units"]
    assert "pylon-manual@sync.service" in data["held_units"]
    assert "pylon-module@operations--relay.service" not in data["restarted"]
    assert "pylon-module@gmail--relay.service" not in data["held_units"]
    assert names["old"] in data["held"]
    assert names["previous"] not in data["held"]
    calls = (home / "calls").read_text()
    assert "systemctl restart pylon-module@gmail--relay.service" in calls
    assert "pylon-task@sync.service" not in calls
    assert "pylon-manual@sync.service" not in calls
    assert (proc / "21" / "exe").resolve() == current.resolve()
    assert (proc / "22" / "exe").resolve() == stale.resolve()


def test_migrate_restart_failure_reports_and_keeps_release(tmp_path):
    home, releases, _, units, names = layout(tmp_path)
    proc = home / "proc"
    current = releases / names["current"] / "dist/pylon-linux-x86_64"
    previous = releases / names["previous"] / "dist/pylon-linux-x86_64"
    write_proc(proc, 31, previous, unit="pylon-module@itunes--relay.service")
    commands = tmp_path / "bin"
    commands.mkdir()
    _fake_systemctl(commands / "systemctl", home, fail="pylon-module@itunes--relay.service")
    env = {**os.environ, "PATH": str(commands) + os.pathsep + os.environ["PATH"],
           "PYLON_DEPLOY_PROC": str(proc)}
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--migrate", "--restart", "--home", str(home),
         "--target-binary", str(current), "--previous-binary", str(previous),
         "--proc-dir", str(proc)],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "partial"
    assert data["restarted"] == []
    assert data["failed"] == [{"error": "restart_failed", "unit": "pylon-module@itunes--relay.service"}]
    assert names["previous"] in data["held"]
    assert "pylon-module@itunes--relay.service" in data["held_units"]
    assert (proc / "31" / "exe").resolve() == previous.resolve()
