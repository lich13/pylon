#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


PROFILE_ROOT = Path(os.environ.get("HERMES_HOME", "/srv/pylon/profiles/default"))
DEFAULT_SILENT_OUTPUT_RETENTION_DAYS = int(os.environ.get("XIANBAO_SILENT_OUTPUT_RETENTION_DAYS", "1"))
DEFAULT_TMP_RETENTION_DAYS = int(os.environ.get("XIANBAO_TMP_RETENTION_DAYS", "1"))
SILENT_STATUS_MARKERS = (
    "**Status:** silent (empty output)",
    "**Status:** silent (wakeAgent=false)",
)


def _is_safe_child(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _cutoff_ts(days: int) -> float:
    return time.time() - max(0, int(days)) * 86400


def _old_regular_files(directory: Path, cutoff_ts: float, *, patterns: tuple[str, ...] = ("*",)) -> list[Path]:
    if not directory.exists():
        return []
    if not directory.is_dir() or directory.is_symlink():
        return []
    candidates: list[Path] = []
    for pattern in patterns:
        for child in sorted(directory.glob(pattern)):
            try:
                if child.is_symlink() or not child.is_file():
                    continue
                if child.stat().st_mtime < cutoff_ts:
                    candidates.append(child)
            except OSError:
                continue
    return candidates




def _is_silent_output(path: Path, errors: list[str]) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        errors.append(f"{path}: {exc}")
        return False
    return any(marker in text for marker in SILENT_STATUS_MARKERS)


def collect_silent_output_candidates(profile_root: Path, *, retention_days: int) -> tuple[list[Path], list[str]]:
    output_dir = profile_root / "cron" / "output" / "xianbao-hermes-poll"
    errors: list[str] = []
    if not _is_safe_child(output_dir, profile_root):
        return [], errors
    old_outputs = _old_regular_files(output_dir, _cutoff_ts(retention_days), patterns=("*.md",))
    return [path for path in old_outputs if _is_silent_output(path, errors)], errors


def collect_tmp_candidates(profile_root: Path, *, retention_days: int) -> list[Path]:
    data_dir = profile_root / "data" / "xianbao_filter"
    if not _is_safe_child(data_dir, profile_root):
        return []
    return _old_regular_files(
        data_dir,
        _cutoff_ts(retention_days),
        patterns=("xianbao-poll-out.*", "xianbao-poll-err.*"),
    )


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen: set[Path] = set()
    output: list[Path] = []
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        output.append(path)
    return output


def _path_size(path: Path) -> int:
    try:
        if path.is_symlink() or not path.is_file():
            return 0
        return int(path.stat().st_size)
    except OSError:
        return 0


def _delete_candidates(candidates: list[Path]) -> tuple[int, int, list[str]]:
    deleted = 0
    deleted_bytes = 0
    errors: list[str] = []
    for path in candidates:
        try:
            if path.is_symlink() or not path.is_file():
                continue
            size = int(path.stat().st_size)
            path.unlink()
            deleted += 1
            deleted_bytes += size
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return deleted, deleted_bytes, errors


def run_cleanup(
    profile_root: str | Path = PROFILE_ROOT,
    *,
    silent_output_retention_days: int = DEFAULT_SILENT_OUTPUT_RETENTION_DAYS,
    tmp_retention_days: int = DEFAULT_TMP_RETENTION_DAYS,
    apply: bool = False,
) -> dict[str, Any]:
    root = Path(profile_root).resolve()
    silent_candidates, errors = collect_silent_output_candidates(root, retention_days=silent_output_retention_days)
    tmp_candidates = collect_tmp_candidates(root, retention_days=tmp_retention_days)
    candidates = _unique_paths(silent_candidates + tmp_candidates)
    candidate_bytes = sum(_path_size(path) for path in candidates)
    deleted = 0
    deleted_bytes = 0
    if apply:
        deleted, deleted_bytes, delete_errors = _delete_candidates(candidates)
        errors.extend(delete_errors)
    return {
        "mode": "apply" if apply else "dry-run",
        "profile_root": str(root),
        "silent_output_retention_days": silent_output_retention_days,
        "tmp_retention_days": tmp_retention_days,
        "silent_output_candidates": len(silent_candidates),
        "tmp_candidates": len(tmp_candidates),
        "candidate_files": len(candidates),
        "candidate_bytes": candidate_bytes,
        "deleted_files": deleted,
        "deleted_bytes": deleted_bytes,
        "errors": errors,
        "candidates": [str(path) for path in candidates],
    }


def format_summary(result: dict[str, Any]) -> str:
    lines = [
        "Xianbao retention cleanup",
        f"mode: {result['mode']}",
        f"silent output candidates: {result['silent_output_candidates']}",
        f"tmp candidates: {result['tmp_candidates']}",
        f"candidate files: {result['candidate_files']}",
        f"candidate bytes: {result['candidate_bytes']}",
        f"deleted files: {result['deleted_files']}",
        f"deleted bytes: {result['deleted_bytes']}",
    ]
    if result["errors"]:
        lines.append("errors:")
        lines.extend(f"- {err}" for err in result["errors"])
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean Xianbao silent poll output and stale poll temp files")
    parser.add_argument("--profile-root", type=Path, default=PROFILE_ROOT)
    parser.add_argument("--silent-output-retention-days", type=int, default=DEFAULT_SILENT_OUTPUT_RETENTION_DAYS)
    parser.add_argument("--tmp-retention-days", type=int, default=DEFAULT_TMP_RETENTION_DAYS)
    parser.add_argument("--apply", action="store_true", help="Delete candidates. Default is dry-run.")
    parser.add_argument("--dry-run", action="store_true", help="Preview only. This is the default.")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_cleanup(
        args.profile_root,
        silent_output_retention_days=args.silent_output_retention_days,
        tmp_retention_days=args.tmp_retention_days,
        apply=bool(args.apply and not args.dry_run),
    )
    if not args.quiet:
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(format_summary(result))
    elif result["errors"]:
        print(format_summary(result), file=sys.stderr)
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
