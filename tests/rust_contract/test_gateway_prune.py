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


def run(home, units, *args):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--home", str(home), "--systemd-dir", str(units),
         "--now", str(NOW), *args], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
