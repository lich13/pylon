#!/usr/bin/env python3
"""Prune gateway-owned releases and backups after a healthy deploy.

Releases: keep the current binary's release and the previous (rollback) one;
only directories named v<semver>-<sha>, stability-<sha> or hardening-<sha>
are candidates. Anything referenced by a /srv/pylon symlink or a systemd unit
is retained. Backups: gateway-* directories older than --backup-days are
removed, but the newest one always stays. Nothing else is touched.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

RELEASE = re.compile(r"(?:v\d+\.\d+\.\d+|stability|hardening)-[0-9a-f]{40}")
BACKUP = re.compile(r"gateway-(\d{8}T\d{6}Z)(?:-[A-Za-z0-9._-]+)?")


def release_of(path: Path, releases: Path) -> str | None:
    try:
        relative = Path(os.path.realpath(path)).relative_to(os.path.realpath(releases))
    except ValueError:
        return None
    name = relative.parts[0] if relative.parts else None
    return name if name and RELEASE.fullmatch(name) else None


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


def prune_releases(home: Path, keep: list[Path], systemd: list[Path], dry_run: bool) -> dict:
    releases = home / "releases"
    current = release_of(home / "bin/pylon", releases)
    kept = {name for name in (release_of(path, releases) for path in keep) if name}
    if not current or len(kept) != len(keep):
        return {"status": "skipped_unresolved", "deleted": [], "kept": sorted(kept | {current} - {None})}
    kept.add(current)
    kept |= referenced(home, releases, systemd)
    parent = releases.lstat().st_dev
    deleted = []
    for entry in sorted(releases.iterdir()):
        if RELEASE.fullmatch(entry.name) and entry.name not in kept:
            if remove(entry, parent, dry_run):
                deleted.append(entry.name)
    return {"status": "ok", "deleted": deleted, "kept": sorted(kept)}


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default="/srv/pylon")
    parser.add_argument("--keep", action="append", default=[], help="binary or directory whose release is retained")
    parser.add_argument("--backup-days", type=int, default=14)
    parser.add_argument("--systemd-dir", action="append", default=None)
    parser.add_argument("--now", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.backup_days < 1:
        parser.error("--backup-days must be positive")
    home = Path(args.home)
    systemd = [Path(p) for p in (args.systemd_dir or ["/etc/systemd/system", "/lib/systemd/system"])]
    result = {"dry_run": args.dry_run}
    try:
        result["releases"] = prune_releases(home, [Path(p) for p in args.keep], systemd, args.dry_run)
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
