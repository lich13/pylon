#!/usr/bin/env python3
"""Prune gateway-owned releases and backups after a healthy deploy.

Releases: keep the current binary's release and the previous (rollback) one;
only directories named v<semver>-<sha>, stability-<sha> or hardening-<sha>
are candidates. Anything referenced by a /srv/pylon symlink, a systemd unit,
or a live process exe/cwd is retained. Backups: gateway-* directories older
than --backup-days are removed, but the newest one always stays. Nothing
else is touched.

--migrate scans leftover supervisors after a healthy cutover and, when
--restart is set, restarts only pylon-module@ units still executing an old
gateway binary. Oneshot pylon-task@ / pylon-manual@ units are never
restarted; their releases are reported and left for pruning to retain.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

RELEASE = re.compile(r"(?:v\d+\.\d+\.\d+|stability|hardening)-[0-9a-f]{40}")
# gateway-<UTC>[-suffix] from deploys, gateway-ops-<UTC>-<op> from operator changes.
BACKUP = re.compile(r"gateway-(?:ops-)?(\d{8}T\d{6}Z)(?:-[A-Za-z0-9._-]+)?")
DELETED = " (deleted)"
UNIT = re.compile(
    r"(pylon-module@[A-Za-z0-9_.-]+\.service|"
    r"pylon-task@[A-Za-z0-9_.-]+\.service|"
    r"pylon-manual@[A-Za-z0-9_.-]+\.service|"
    r"pylon-gateway\.service|pylon-broker\.service|pylon-executor\.service)"
)
MODULE_UNIT = re.compile(r"^pylon-module@[A-Za-z0-9_.-]+\.service$")


def release_of(path: Path, releases: Path) -> str | None:
    text = os.fsdecode(os.fspath(path))
    if text.endswith(DELETED):
        text = text[: -len(DELETED)]
    if not text:
        return None
    try:
        root = os.path.realpath(releases)
    except OSError:
        return None
    candidates = [text]
    try:
        candidates.append(os.path.realpath(text))
    except OSError:
        pass
    for candidate in candidates:
        try:
            relative = Path(candidate).relative_to(root)
        except ValueError:
            continue
        name = relative.parts[0] if relative.parts else None
        if name and RELEASE.fullmatch(name):
            return name
    return None


def _readlink(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def unit_of(pid_dir: Path) -> str | None:
    try:
        text = (pid_dir / "cgroup").read_text(errors="replace")
    except OSError:
        return None
    found = UNIT.findall(text)
    return found[-1] if found else None


def process_holders(releases: Path, proc: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        entries = list(proc.iterdir())
    except OSError:
        return rows
    for entry in entries:
        if not entry.name.isdigit():
            continue
        exe = _readlink(entry / "exe")
        cwd = _readlink(entry / "cwd")
        rows.append(
            {
                "pid": int(entry.name),
                "unit": unit_of(entry),
                "exe": exe,
                "exe_release": release_of(Path(exe), releases) if exe else None,
                "cwd_release": release_of(Path(cwd), releases) if cwd else None,
            }
        )
    return rows


def process_referenced(releases: Path, proc: Path) -> set[str]:
    names: set[str] = set()
    for row in process_holders(releases, proc):
        for key in ("exe_release", "cwd_release"):
            name = row[key]
            if name:
                names.add(name)
    return names


def referenced(home: Path, releases: Path, systemd: list[Path]) -> set[str]:
    names: set[str] = set()
    for directory in (home, home / "bin"):
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                name = release_of(Path(entry.path), releases)
                if name:
                    names.add(name)
    marker = re.compile(re.escape(str(releases)) + r"/([^/\s]+)")
    for directory in systemd:
        for unit in sorted(directory.glob("*")) + sorted(directory.glob("*.d/*.conf")):
            try:
                if unit.is_file():
                    names.update(
                        name for name in marker.findall(unit.read_text(errors="replace"))
                        if RELEASE.fullmatch(name)
                    )
            except OSError:
                continue
    return names


def remove(path: Path, parent_dev: int, dry_run: bool) -> bool:
    info = path.lstat()
    if not path.is_dir() or path.is_symlink() or info.st_dev != parent_dev:
        return False
    if not dry_run:
        shutil.rmtree(path)
    return True


def prune_releases(
    home: Path, keep: list[Path], systemd: list[Path], proc: Path, dry_run: bool
) -> dict:
    releases = home / "releases"
    current = release_of(home / "bin/pylon", releases)
    resolved = [release_of(path, releases) for path in keep]
    if not current or any(name is None for name in resolved):
        return {
            "status": "skipped_unresolved",
            "deleted": [],
            "kept": sorted({name for name in resolved + [current] if name}),
            "held": [],
        }
    kept = set(resolved)
    kept.add(current)
    kept |= referenced(home, releases, systemd)
    held = process_referenced(releases, proc)
    kept |= held
    parent = releases.lstat().st_dev
    deleted = []
    for entry in sorted(releases.iterdir()):
        if RELEASE.fullmatch(entry.name) and entry.name not in kept:
            if remove(entry, parent, dry_run):
                deleted.append(entry.name)
    return {"status": "ok", "deleted": deleted, "kept": sorted(kept), "held": sorted(held)}


def backup_time(path: Path) -> float:
    match = BACKUP.fullmatch(path.name)
    if match:
        moment = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
        return moment.replace(tzinfo=dt.timezone.utc).timestamp()
    return path.lstat().st_mtime


def prune_backups(home: Path, days: int, now: float, dry_run: bool) -> dict:
    backups = home / "backups"
    if not backups.is_dir():
        return {"status": "ok", "deleted": [], "kept": []}
    found = sorted(
        (backup_time(entry), entry)
        for entry in backups.iterdir()
        if entry.name.startswith("gateway-") and entry.is_dir() and not entry.is_symlink()
    )
    parent = backups.lstat().st_dev
    deleted, kept = [], []
    for index, (moment, entry) in enumerate(found):
        newest = index == len(found) - 1
        if not newest and now - moment > days * 86400 and remove(entry, parent, dry_run):
            deleted.append(entry.name)
        else:
            kept.append(entry.name)
    return {"status": "ok", "deleted": deleted, "kept": kept}


def _realpath(path: Path) -> str | None:
    try:
        return os.path.realpath(path)
    except OSError:
        return None


def leftover_binaries(
    holders: list[dict], releases: Path, target_binary: Path, previous_binary: Path
) -> list[dict]:
    target = _realpath(target_binary)
    previous = _realpath(previous_binary)
    leftover = []
    for row in holders:
        exe = row["exe"]
        if not exe:
            continue
        text = exe[: -len(DELETED)] if exe.endswith(DELETED) else exe
        resolved = _realpath(Path(text))
        if target and resolved == target:
            continue
        if row["exe_release"] or (previous and resolved == previous):
            leftover.append(row)
    return leftover


def _keep_paths(rows: list[dict], releases: Path) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for row in rows:
        exe = row["exe"]
        if exe:
            text = exe[: -len(DELETED)] if exe.endswith(DELETED) else exe
            resolved = _realpath(Path(text))
            if resolved and resolved not in seen:
                seen.add(resolved)
                paths.append(resolved)
        name = row["exe_release"] or row["cwd_release"]
        if name:
            directory = str((releases / name).resolve()) if (releases / name).exists() else str(releases / name)
            if directory not in seen:
                seen.add(directory)
                paths.append(directory)
    return paths


def migrate_leftovers(
    home: Path,
    target_binary: Path,
    previous_binary: Path,
    proc: Path,
    restart: bool,
) -> dict:
    releases = home / "releases"
    started = leftover_binaries(
        process_holders(releases, proc), releases, target_binary, previous_binary
    )
    restarted: list[str] = []
    failed: list[dict] = []
    deferred: list[str] = []
    if restart:
        units = sorted({row["unit"] for row in started if row["unit"] and MODULE_UNIT.fullmatch(row["unit"])})
        for unit in units:
            try:
                completed = subprocess.run(
                    ["systemctl", "restart", unit],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                failed.append({"unit": unit, "error": type(error).__name__})
                continue
            if completed.returncode != 0:
                failed.append({"unit": unit, "error": "restart_failed"})
            else:
                restarted.append(unit)
    else:
        deferred = sorted(
            {row["unit"] for row in started if row["unit"] and MODULE_UNIT.fullmatch(row["unit"])}
        )
    remaining = leftover_binaries(
        process_holders(releases, proc), releases, target_binary, previous_binary
    )
    held_units = sorted({row["unit"] for row in remaining if row["unit"]})
    held = sorted(
        {row["exe_release"] or row["cwd_release"] for row in remaining if row["exe_release"] or row["cwd_release"]}
        - {None}
    )
    status = "ok"
    if not restart:
        status = "deferred"
    elif failed and remaining:
        status = "partial"
    return {
        "status": status,
        "restarted": restarted,
        "failed": failed,
        "deferred": deferred,
        "held": held,
        "held_units": held_units,
        "keep": _keep_paths(remaining, releases),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default="/srv/pylon")
    parser.add_argument("--keep", action="append", default=[], help="binary or directory whose release is retained")
    parser.add_argument("--backup-days", type=int, default=14)
    parser.add_argument("--systemd-dir", action="append", default=None)
    parser.add_argument("--proc-dir", default="/proc")
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--migrate", action="store_true", help="scan leftover supervisors instead of pruning")
    parser.add_argument("--target-binary", default=None)
    parser.add_argument("--previous-binary", default=None)
    parser.add_argument("--restart", action="store_true", help="with --migrate, restart leftover pylon-module@ units")
    args = parser.parse_args()
    if args.backup_days < 1:
        parser.error("--backup-days must be positive")
    home = Path(args.home)
    if args.migrate:
        if not args.target_binary or not args.previous_binary:
            parser.error("--migrate requires --target-binary and --previous-binary")
        try:
            result = migrate_leftovers(
                home,
                Path(args.target_binary),
                Path(args.previous_binary),
                Path(args.proc_dir),
                args.restart,
            )
        except OSError as error:
            result = {"status": "failed", "error": type(error).__name__, "restarted": [], "failed": [], "deferred": [], "held": [], "held_units": [], "keep": []}
        print(json.dumps(result, sort_keys=True))
        return 0
    systemd = [Path(p) for p in (args.systemd_dir or ["/etc/systemd/system", "/lib/systemd/system"])]
    result = {"dry_run": args.dry_run}
    try:
        result["releases"] = prune_releases(
            home, [Path(p) for p in args.keep], systemd, Path(args.proc_dir), args.dry_run
        )
    except OSError as error:
        result["releases"] = {"status": "failed", "error": type(error).__name__}
    try:
        result["backups"] = prune_backups(home, args.backup_days, args.now or time.time(), args.dry_run)
    except (OSError, ValueError) as error:
        result["backups"] = {"status": "failed", "error": type(error).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if all(result[k]["status"] != "failed" for k in ("releases", "backups")) else 1


if __name__ == "__main__":
    sys.exit(main())
