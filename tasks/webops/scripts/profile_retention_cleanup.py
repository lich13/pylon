#!/usr/bin/env python3
"""Retention cleanup for the WebOps Hermes profile.

Deletes only known disposable outputs and old cron-only session history:
- cron/output files older than 2 days
- Hermes state.db sessions where source='cron', ended, and older than 1 day
- Hermes state.db sessions where source is telegram/cli, ended, and ended more than 14 days ago
- orphan session files older than 7 days and not linked from state.db
- Chromium profile cache contents for known non-Chartoo WebOps browser profiles

Intentionally preserves open sessions, current/young Telegram/CLI sessions,
SQLite task state, browser login state, browser profile roots, scripts, config,
and current/young run outputs.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.webops import locks, output, paths
from pylon import events

PROFILE_ROOT = paths.PROFILE_ROOT.resolve()
DEFAULT_PROFILE_ROOT = PROFILE_ROOT
STATE_DB = PROFILE_ROOT / 'state.db'
SESSIONS_DIR = PROFILE_ROOT / 'sessions'
LOCK_PATH = Path('/tmp/pylon_webops_retention_cleanup.lock')
CHARTOO_DATA_ROOT = paths.CHARTOO_DATA_ROOT
CHARTOO_RELEASE_DATES_TO_KEEP = 2
LINUXDO_DATA_ROOT = PROFILE_ROOT / 'data' / 'linuxdo_notice'
LINUXDO_DECISION_LOG_FILE = LINUXDO_DATA_ROOT / 'decisions.jsonl'
LINUXDO_DECISION_ARCHIVE_DIR = LINUXDO_DATA_ROOT / 'archives'
LINUXDO_DECISION_LOG_RETENTION_DAYS = 2

RULES = [
    {
        'name': 'cron_output',
        'path': PROFILE_ROOT / 'cron' / 'output',
        'retention_days': 2,
        'description': 'Hermes cron output files',
    },
]

COMMON_BROWSER_CACHE_ROOTS = [
    'Default/Cache',
    'Default/Code Cache',
    'Default/GPUCache',
    'Default/DawnGraphiteCache',
    'Default/DawnWebGPUCache',
    'Default/Shared Dictionary/cache',
    'Default/Service Worker/CacheStorage',
    'Default/Service Worker/ScriptCache',
]

BROWSER_PROFILE_RULES = [
    {
        'name': 'chiphell_fid26_browser_profile',
        'path': PROFILE_ROOT / 'data' / 'chiphell_fid26' / 'browser-profile',
        'lock_path': PROFILE_ROOT / 'data' / 'chiphell_fid26' / 'monitor.lock',
        'cache_roots': COMMON_BROWSER_CACHE_ROOTS + [
            'GPUPersistentCache',
            'GrShaderCache',
            'GraphiteDawnCache',
            'ShaderCache',
        ],
    },
]

CRON_SESSION_RETENTION_DAYS = 1
NONCRON_SESSION_RETENTION_DAYS = 14
NONCRON_SESSION_SOURCES = ('telegram', 'cli')
ORPHAN_SESSION_FILE_RETENTION_DAYS = 7


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parse_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith('Z'):
        text = text[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def safe_path(path: Path) -> Path:
    resolved = path.resolve()
    root = PROFILE_ROOT.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f'unsafe cleanup path outside profile root: {resolved}')
    return resolved


def iter_files(root: Path):
    if not root.exists():
        return
    for path in root.rglob('*'):
        try:
            if path.is_file() or path.is_symlink():
                yield path
        except OSError:
            continue


def prune_empty_dirs(root: Path, dry_run: bool, errors: List[Dict[str, str]]) -> int:
    pruned = 0
    if not root.exists():
        return pruned
    dirs = [p for p in root.rglob('*') if p.is_dir()]
    for directory in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if any(directory.iterdir()):
                continue
            pruned += 1
            if not dry_run:
                directory.rmdir()
        except OSError as e:
            errors.append({'path': str(directory), 'error': repr(e), 'phase': 'prune_empty_dirs'})
    return pruned


def cleanup_rule(rule: Dict[str, Any], now: float, dry_run: bool) -> Dict[str, Any]:
    root = safe_path(Path(rule['path']))
    retention_days = float(rule['retention_days'])
    cutoff = now - retention_days * 86400
    result: Dict[str, Any] = {
        'name': rule['name'],
        'path': str(root),
        'retention_days': retention_days,
        'cutoff_utc': datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec='seconds'),
        'scanned_files': 0,
        'deleted_files': 0,
        'deleted_bytes': 0,
        'pruned_empty_dirs': 0,
        'missing': not root.exists(),
        'errors': [],
    }
    if not root.exists():
        return result

    for path in iter_files(root) or []:
        result['scanned_files'] += 1
        try:
            st = path.lstat()
            if st.st_mtime >= cutoff:
                continue
            result['deleted_files'] += 1
            result['deleted_bytes'] += st.st_size
            if not dry_run:
                path.unlink()
        except OSError as e:
            result['errors'].append({'path': str(path), 'error': repr(e), 'phase': 'unlink'})

    result['pruned_empty_dirs'] = prune_empty_dirs(root, dry_run, result['errors'])
    return result


def directory_size_bytes(root: Path) -> int:
    total = 0
    if not root.exists():
        return total
    try:
        if root.is_symlink() or root.is_file():
            return root.lstat().st_size
    except OSError:
        return total
    for path in root.rglob('*'):
        try:
            if path.is_symlink() or path.is_file():
                total += path.lstat().st_size
        except OSError:
            continue
    return total


def safe_profile_child(profile_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError(f'unsafe absolute browser cache path: {relative}')
    resolved = (profile_root / relative).resolve()
    if resolved == profile_root or profile_root not in resolved.parents:
        raise ValueError(f'unsafe browser cache path outside profile: {resolved}')
    safe_path(resolved)
    return resolved


def delete_tree(path: Path, dry_run: bool, result: Dict[str, Any], errors: List[Dict[str, str]]) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        errors.append({'path': str(path), 'error': repr(exc), 'phase': 'lstat'})
        return

    try:
        if path.is_symlink() or path.is_file():
            result['deleted_files'] += 1
            result['deleted_bytes'] += st.st_size
            if not dry_run:
                path.unlink()
            return

        if path.is_dir():
            for child in list(path.iterdir()):
                delete_tree(child, dry_run, result, errors)
            result['deleted_dirs'] += 1
            if not dry_run:
                path.rmdir()
            return

        errors.append({'path': str(path), 'error': 'unsupported file type', 'phase': 'delete_tree'})
    except OSError as exc:
        errors.append({'path': str(path), 'error': repr(exc), 'phase': 'delete_tree'})


def clear_directory_contents(root: Path, dry_run: bool, errors: List[Dict[str, str]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        'path': str(root),
        'missing': not root.exists(),
        'deleted_files': 0,
        'deleted_dirs': 0,
        'deleted_bytes': 0,
    }
    if not root.exists():
        return result
    if root.is_symlink() or not root.is_dir():
        errors.append({'path': str(root), 'error': 'cache root is not a directory', 'phase': 'clear_directory_contents'})
        return result

    for child in list(root.iterdir()):
        delete_tree(child, dry_run, result, errors)
    return result


def cleanup_browser_profile(rule: Dict[str, Any], dry_run: bool) -> Dict[str, Any]:
    profile_root = safe_path(Path(rule['path']))
    lock_path = Path(rule['lock_path'])
    result: Dict[str, Any] = {
        'name': rule['name'],
        'path': str(profile_root),
        'lock_path': str(lock_path),
        'dry_run': dry_run,
        'skipped_locked': False,
        'missing': not profile_root.exists(),
        'before_bytes': 0,
        'after_bytes': 0,
        'deleted_files': 0,
        'deleted_dirs': 0,
        'deleted_bytes': 0,
        'cache_roots': [],
        'errors': [],
    }

    if not profile_root.exists():
        return result

    with locks.nonblocking_file_lock(lock_path) as lock_result:
        if not lock_result.acquired:
            result['skipped_locked'] = True
            return result

        result['before_bytes'] = directory_size_bytes(profile_root)
        for relative_path in rule.get('cache_roots') or []:
            cache_root = safe_profile_child(profile_root, str(relative_path))
            cache_result = clear_directory_contents(cache_root, dry_run, result['errors'])
            cache_result['relative_path'] = str(relative_path)
            result['cache_roots'].append(cache_result)
            result['deleted_files'] += cache_result['deleted_files']
            result['deleted_dirs'] += cache_result['deleted_dirs']
            result['deleted_bytes'] += cache_result['deleted_bytes']
        result['after_bytes'] = result['before_bytes'] if dry_run else directory_size_bytes(profile_root)
    return result


def checkpoint_state_db(dry_run: bool) -> Dict[str, Any]:
    """Best-effort WAL truncation for Hermes state.db without deleting history."""
    db = STATE_DB
    result: Dict[str, Any] = {
        'name': 'state_db_wal_checkpoint',
        'path': str(db),
        'dry_run': dry_run,
        'missing': not db.exists(),
        'before_wal_bytes': 0,
        'after_wal_bytes': 0,
        'checkpoint_result': None,
        'error': None,
    }
    wal = Path(str(db) + '-wal')
    if wal.exists():
        result['before_wal_bytes'] = wal.stat().st_size
    if dry_run or not db.exists():
        result['after_wal_bytes'] = result['before_wal_bytes']
        return result
    try:
        with sqlite3.connect(str(db), timeout=30) as conn:
            conn.execute('PRAGMA busy_timeout=30000')
            row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
            result['checkpoint_result'] = list(row) if row is not None else None
    except Exception as exc:
        result['error'] = repr(exc)
    if wal.exists():
        result['after_wal_bytes'] = wal.stat().st_size
    return result


def _chartoo_html_release_date(path: Path) -> str | None:
    name = path.name
    suffix = '-chartoo.html'
    if not name.endswith(suffix):
        return None
    release_date = name[:-len(suffix)]
    try:
        datetime.fromisoformat(release_date)
    except ValueError:
        return None
    return release_date


def _chartoo_state_release_dates(db: Path) -> set[str]:
    dates: set[str] = set()
    if not db.exists():
        return dates
    try:
        with sqlite3.connect(str(db), timeout=30) as conn:
            for table in ('observations', 'finalizations'):
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                for row in conn.execute(f'SELECT DISTINCT release_date FROM {table} WHERE release_date IS NOT NULL'):
                    value = str(row[0] or '')
                    try:
                        datetime.fromisoformat(value)
                    except ValueError:
                        continue
                    dates.add(value)
    except sqlite3.Error:
        return dates
    return dates


def chartoo_data_root() -> Path:
    configured = Path(CHARTOO_DATA_ROOT)
    default = paths.CHARTOO_DATA_ROOT.resolve()
    if PROFILE_ROOT.resolve() != paths.PROFILE_ROOT.resolve() and configured.resolve() == default:
        return PROFILE_ROOT / 'data' / 'chartoo_itunes_daily'
    return configured


def _profile_relative_default(configured: Path, default: Path, fallback: Path) -> Path:
    if PROFILE_ROOT.resolve() != DEFAULT_PROFILE_ROOT and configured.resolve() == default.resolve():
        return fallback
    return configured


def linuxdo_data_root() -> Path:
    return _profile_relative_default(
        Path(LINUXDO_DATA_ROOT),
        DEFAULT_PROFILE_ROOT / 'data' / 'linuxdo_notice',
        PROFILE_ROOT / 'data' / 'linuxdo_notice',
    )


def linuxdo_decision_log_file() -> Path:
    return _profile_relative_default(
        Path(LINUXDO_DECISION_LOG_FILE),
        DEFAULT_PROFILE_ROOT / 'data' / 'linuxdo_notice' / 'decisions.jsonl',
        linuxdo_data_root() / 'decisions.jsonl',
    )


def linuxdo_decision_archive_dir() -> Path:
    return _profile_relative_default(
        Path(LINUXDO_DECISION_ARCHIVE_DIR),
        DEFAULT_PROFILE_ROOT / 'data' / 'linuxdo_notice' / 'archives',
        linuxdo_data_root() / 'archives',
    )








def cleanup_chartoo_daily_data(dry_run: bool) -> Dict[str, Any]:
    root = safe_path(chartoo_data_root())
    html_dir = root / 'html'
    db = root / 'state.sqlite'
    result: Dict[str, Any] = {
        'name': 'chartoo_daily_data',
        'path': str(root),
        'dry_run': dry_run,
        'retained_release_dates': CHARTOO_RELEASE_DATES_TO_KEEP,
        'kept_release_dates': [],
        'html_dir_missing': not html_dir.exists(),
        'state_db_missing': not db.exists(),
        'scanned_html_files': 0,
        'deleted_html_files': 0,
        'deleted_html_bytes': 0,
        'deleted_observation_rows': 0,
        'deleted_finalization_rows': 0,
        'errors': [],
    }
    release_dates = set()
    if html_dir.exists():
        for path in html_dir.iterdir():
            if not path.is_file():
                continue
            release_date = _chartoo_html_release_date(path)
            if release_date:
                result['scanned_html_files'] += 1
                release_dates.add(release_date)
    release_dates.update(_chartoo_state_release_dates(db))
    kept = sorted(release_dates)[-CHARTOO_RELEASE_DATES_TO_KEEP:]
    result['kept_release_dates'] = kept
    keep_set = set(kept)

    if html_dir.exists():
        for path in html_dir.iterdir():
            if not path.is_file():
                continue
            release_date = _chartoo_html_release_date(path)
            if not release_date or release_date in keep_set:
                continue
            try:
                size = path.stat().st_size
                result['deleted_html_files'] += 1
                result['deleted_html_bytes'] += size
                if not dry_run:
                    path.unlink()
            except OSError as exc:
                result['errors'].append({'phase': 'delete_chartoo_html', 'path': str(path), 'error': repr(exc)})

    if db.exists() and release_dates:
        placeholders = ','.join('?' for _ in keep_set)
        keep_params = tuple(kept)
        try:
            with sqlite3.connect(str(db), timeout=60) as conn:
                conn.execute('PRAGMA busy_timeout=60000')
                for table, key in (
                    ('observations', 'deleted_observation_rows'),
                    ('finalizations', 'deleted_finalization_rows'),
                ):
                    exists = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,),
                    ).fetchone()
                    if not exists:
                        continue
                    if keep_set:
                        count_sql = f'DELETE FROM {table} WHERE release_date NOT IN ({placeholders})'
                        count_params = keep_params
                    else:
                        count_sql = f'DELETE FROM {table}'
                        count_params = ()
                    count = conn.execute(
                        count_sql.replace('DELETE', 'SELECT COUNT(*)', 1),
                        count_params,
                    ).fetchone()[0]
                    result[key] = int(count)
                    if not dry_run and count:
                        conn.execute(count_sql, count_params)
                if not dry_run:
                    conn.commit()
        except sqlite3.Error as exc:
            result['errors'].append({'phase': 'cleanup_chartoo_state', 'path': str(db), 'error': repr(exc)})
    return result


def atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.tmp-{os.getpid()}')
    tmp.write_text(payload, encoding='utf-8')
    tmp.replace(path)


def atomic_write_json(path: Path, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + '\n'
    atomic_write_text(path, text)


def archive_linuxdo_decision_log(now: float, dry_run: bool) -> Dict[str, Any]:
    path = safe_path(linuxdo_decision_log_file())
    archive_dir = safe_path(linuxdo_decision_archive_dir())
    cutoff = datetime.fromtimestamp(
        now - LINUXDO_DECISION_LOG_RETENTION_DAYS * 86400,
        tz=timezone.utc,
    )
    result: Dict[str, Any] = {
        'name': 'linuxdo_decision_log_archive',
        'path': str(path),
        'archive_dir': str(archive_dir),
        'dry_run': dry_run,
        'retention_days': LINUXDO_DECISION_LOG_RETENTION_DAYS,
        'cutoff_utc': cutoff.isoformat(timespec='seconds'),
        'missing': not path.exists(),
        'scanned_lines': 0,
        'archived_lines': 0,
        'retained_lines': 0,
        'malformed_lines': 0,
        'undated_lines': 0,
        'removed_active_bytes': 0,
        'archive_files': [],
        'errors': [],
    }
    if not path.exists():
        return result

    retained_lines: List[str] = []
    archived_by_date: Dict[str, List[str]] = {}
    try:
        with path.open('r', encoding='utf-8') as handle:
            for line in handle:
                raw = line.rstrip('\n')
                result['scanned_lines'] += 1
                if not raw.strip():
                    retained_lines.append(raw)
                    result['retained_lines'] += 1
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    retained_lines.append(raw)
                    result['retained_lines'] += 1
                    result['malformed_lines'] += 1
                    continue
                if not isinstance(entry, dict):
                    retained_lines.append(raw)
                    result['retained_lines'] += 1
                    result['malformed_lines'] += 1
                    continue

                logged_at = parse_utc_datetime(entry.get('logged_at') or entry.get('posted_at'))
                if logged_at is None:
                    retained_lines.append(raw)
                    result['retained_lines'] += 1
                    result['undated_lines'] += 1
                    continue
                if logged_at >= cutoff:
                    retained_lines.append(raw)
                    result['retained_lines'] += 1
                    continue

                archive_date = logged_at.date().isoformat()
                archived_by_date.setdefault(archive_date, []).append(raw)
                result['archived_lines'] += 1
                result['removed_active_bytes'] += len(line.encode('utf-8'))
    except OSError as exc:
        result['errors'].append({'phase': 'read_decision_log', 'path': str(path), 'error': repr(exc)})
        return result

    result['archive_files'] = [
        str(archive_dir / f'decisions-{archive_date}.jsonl.gz')
        for archive_date in sorted(archived_by_date)
    ]
    if dry_run or not archived_by_date:
        return result

    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        for archive_date in sorted(archived_by_date):
            archive_path = archive_dir / f'decisions-{archive_date}.jsonl.gz'
            with gzip.open(archive_path, 'at', encoding='utf-8') as handle:
                for raw in archived_by_date[archive_date]:
                    handle.write(raw)
                    handle.write('\n')
    except OSError as exc:
        result['errors'].append({'phase': 'write_decision_archive', 'path': str(archive_dir), 'error': repr(exc)})
        return result

    try:
        active_payload = '\n'.join(retained_lines)
        if active_payload:
            active_payload += '\n'
        atomic_write_text(path, active_payload)
    except OSError as exc:
        result['errors'].append({'phase': 'rewrite_decision_log', 'path': str(path), 'error': repr(exc)})
    return result




def _session_files_for_id(session_id: str) -> List[Path]:
    files = [
        SESSIONS_DIR / f'{session_id}.jsonl',
        SESSIONS_DIR / f'{session_id}.json',
        SESSIONS_DIR / f'session_{session_id}.json',
    ]
    files.extend(SESSIONS_DIR.glob(f'request_dump_{session_id}_*.json'))
    files.extend(SESSIONS_DIR.glob(f'request_dump_session_{session_id}_*.json'))
    # Preserve order while deduping.
    out: List[Path] = []
    seen = set()
    for p in files:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def prune_cron_sessions(now: float, dry_run: bool) -> Dict[str, Any]:
    """Prune only ended cron sessions older than CRON_SESSION_RETENTION_DAYS.

    Telegram/CLI sessions are intentionally untouched.  We delete DB rows and
    the run_agent legacy `session_<id>.json` files that Hermes' generic prune
    command does not remove.
    """
    cutoff = now - CRON_SESSION_RETENTION_DAYS * 86400
    result: Dict[str, Any] = {
        'name': 'cron_sessions',
        'path': str(STATE_DB),
        'retention_days': CRON_SESSION_RETENTION_DAYS,
        'cutoff_utc': datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec='seconds'),
        'candidate_sessions': 0,
        'deleted_db_sessions': 0,
        'deleted_files': 0,
        'deleted_bytes': 0,
        'vacuumed': False,
        'checkpoint_before_vacuum': None,
        'checkpoint_after_vacuum': None,
        'missing': not STATE_DB.exists(),
        'errors': [],
    }
    if not STATE_DB.exists():
        return result

    try:
        with sqlite3.connect(str(STATE_DB), timeout=60) as conn:
            conn.execute('PRAGMA busy_timeout=60000')
            ids = [
                row[0]
                for row in conn.execute(
                    """
                    SELECT id FROM sessions
                    WHERE source='cron' AND ended_at IS NOT NULL AND started_at < ?
                    ORDER BY started_at ASC
                    """,
                    (cutoff,),
                ).fetchall()
            ]
    except Exception as exc:
        result['errors'].append({'phase': 'select_candidates', 'error': repr(exc)})
        return result

    result['candidate_sessions'] = len(ids)
    if dry_run or not ids:
        return result

    try:
        with sqlite3.connect(str(STATE_DB), timeout=120) as conn:
            conn.execute('PRAGMA busy_timeout=120000')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            try:
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    placeholders = ','.join('?' for _ in chunk)
                    conn.execute(
                        f'UPDATE sessions SET parent_session_id=NULL WHERE parent_session_id IN ({placeholders})',
                        chunk,
                    )
                    conn.execute(f'DELETE FROM messages WHERE session_id IN ({placeholders})', chunk)
                    conn.execute(f'DELETE FROM sessions WHERE id IN ({placeholders})', chunk)
                    result['deleted_db_sessions'] += len(chunk)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:
        result['errors'].append({'phase': 'delete_db_rows', 'error': repr(exc)})
        return result

    for sid in ids:
        for path in _session_files_for_id(sid):
            try:
                if path.exists() and path.is_file():
                    size = path.stat().st_size
                    path.unlink()
                    result['deleted_files'] += 1
                    result['deleted_bytes'] += size
            except OSError as exc:
                result['errors'].append({'phase': 'delete_session_file', 'path': str(path), 'error': repr(exc)})

    # Reclaim main DB pages after big cron-only deletes.  This can take seconds
    # but runs only once daily and only after deletion.
    if result['deleted_db_sessions']:
        try:
            with sqlite3.connect(str(STATE_DB), timeout=120) as conn:
                conn.execute('PRAGMA busy_timeout=120000')
                row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                result['checkpoint_before_vacuum'] = list(row) if row is not None else None
                conn.execute('VACUUM')
                row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                result['checkpoint_after_vacuum'] = list(row) if row is not None else None
                result['vacuumed'] = True
        except Exception as exc:
            result['errors'].append({'phase': 'vacuum', 'error': repr(exc)})
    return result


def prune_noncron_sessions(now: float, dry_run: bool) -> Dict[str, Any]:
    """Prune ended Telegram/CLI sessions older than NONCRON_SESSION_RETENTION_DAYS."""
    cutoff = now - NONCRON_SESSION_RETENTION_DAYS * 86400
    result: Dict[str, Any] = {
        'name': 'noncron_sessions',
        'path': str(STATE_DB),
        'retention_days': NONCRON_SESSION_RETENTION_DAYS,
        'sources': list(NONCRON_SESSION_SOURCES),
        'cutoff_utc': datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec='seconds'),
        'candidate_sessions': 0,
        'deleted_db_sessions': 0,
        'deleted_files': 0,
        'deleted_bytes': 0,
        'vacuumed': False,
        'checkpoint_before_vacuum': None,
        'checkpoint_after_vacuum': None,
        'missing': not STATE_DB.exists(),
        'errors': [],
    }
    if not STATE_DB.exists():
        return result

    try:
        placeholders = ','.join('?' for _ in NONCRON_SESSION_SOURCES)
        with sqlite3.connect(str(STATE_DB), timeout=60) as conn:
            conn.execute('PRAGMA busy_timeout=60000')
            ids = [
                row[0]
                for row in conn.execute(
                    f"""
                    SELECT id FROM sessions
                    WHERE source IN ({placeholders}) AND ended_at IS NOT NULL AND ended_at < ?
                    ORDER BY ended_at ASC
                    """,
                    (*NONCRON_SESSION_SOURCES, cutoff),
                ).fetchall()
            ]
    except Exception as exc:
        result['errors'].append({'phase': 'select_candidates', 'error': repr(exc)})
        return result

    result['candidate_sessions'] = len(ids)
    if dry_run or not ids:
        return result

    try:
        with sqlite3.connect(str(STATE_DB), timeout=120) as conn:
            conn.execute('PRAGMA busy_timeout=120000')
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('BEGIN IMMEDIATE')
            try:
                for i in range(0, len(ids), 500):
                    chunk = ids[i:i + 500]
                    placeholders = ','.join('?' for _ in chunk)
                    conn.execute(
                        f'UPDATE sessions SET parent_session_id=NULL WHERE parent_session_id IN ({placeholders})',
                        chunk,
                    )
                    conn.execute(f'DELETE FROM messages WHERE session_id IN ({placeholders})', chunk)
                    conn.execute(f'DELETE FROM sessions WHERE id IN ({placeholders})', chunk)
                    result['deleted_db_sessions'] += len(chunk)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as exc:
        result['errors'].append({'phase': 'delete_db_rows', 'error': repr(exc)})
        return result

    for sid in ids:
        for path in _session_files_for_id(sid):
            try:
                if path.exists() and path.is_file():
                    size = path.stat().st_size
                    path.unlink()
                    result['deleted_files'] += 1
                    result['deleted_bytes'] += size
            except OSError as exc:
                result['errors'].append({'phase': 'delete_session_file', 'path': str(path), 'error': repr(exc)})

    if result['deleted_db_sessions']:
        try:
            with sqlite3.connect(str(STATE_DB), timeout=120) as conn:
                conn.execute('PRAGMA busy_timeout=120000')
                row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                result['checkpoint_before_vacuum'] = list(row) if row is not None else None
                conn.execute('VACUUM')
                row = conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                result['checkpoint_after_vacuum'] = list(row) if row is not None else None
                result['vacuumed'] = True
        except Exception as exc:
            result['errors'].append({'phase': 'vacuum', 'error': repr(exc)})
    return result


def _session_file_is_linked(path: Path, session_ids: set[str]) -> bool:
    name = path.name
    stem = path.stem
    if stem in session_ids:
        return True
    if name.startswith('session_') and stem[len('session_'):] in session_ids:
        return True
    for sid in session_ids:
        if name.startswith(f'request_dump_{sid}_') or name.startswith(f'request_dump_session_{sid}_'):
            return True
    return False


def prune_orphan_session_files(now: float, dry_run: bool) -> Dict[str, Any]:
    cutoff = now - ORPHAN_SESSION_FILE_RETENTION_DAYS * 86400
    result: Dict[str, Any] = {
        'name': 'orphan_session_files',
        'path': str(SESSIONS_DIR),
        'retention_days': ORPHAN_SESSION_FILE_RETENTION_DAYS,
        'cutoff_utc': datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat(timespec='seconds'),
        'scanned_files': 0,
        'candidate_files': 0,
        'deleted_files': 0,
        'deleted_bytes': 0,
        'missing': not SESSIONS_DIR.exists(),
        'errors': [],
    }
    if not SESSIONS_DIR.exists():
        return result
    try:
        with sqlite3.connect(str(STATE_DB), timeout=60) as conn:
            session_ids = {row[0] for row in conn.execute('SELECT id FROM sessions')}
    except Exception as exc:
        result['errors'].append({'phase': 'load_session_ids', 'error': repr(exc)})
        return result

    for path in SESSIONS_DIR.iterdir():
        try:
            if not path.is_file():
                continue
            result['scanned_files'] += 1
            if _session_file_is_linked(path, session_ids):
                continue
            st = path.stat()
            if st.st_mtime >= cutoff:
                continue
            result['candidate_files'] += 1
            result['deleted_files'] += 1
            result['deleted_bytes'] += st.st_size
            if not dry_run:
                path.unlink()
        except OSError as exc:
            result['errors'].append({'phase': 'delete_orphan_file', 'path': str(path), 'error': repr(exc)})
    return result


def run(dry_run: bool) -> Dict[str, Any]:
    PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    with locks.nonblocking_file_lock(LOCK_PATH) as lock_result:
        if not lock_result.acquired:
            return {'status': 'locked', 'dry_run': dry_run, 'generated_at_utc': utc_now_iso()}
        now = time.time()
        results = [cleanup_rule(rule, now, dry_run) for rule in RULES]
        browser_profile_cleanup = [cleanup_browser_profile(rule, dry_run) for rule in BROWSER_PROFILE_RULES]
        cron_session_prune = prune_cron_sessions(now, dry_run)
        noncron_session_prune = prune_noncron_sessions(now, dry_run)
        orphan_session_file_prune = prune_orphan_session_files(now, dry_run)
        chartoo_daily_cleanup = cleanup_chartoo_daily_data(dry_run)
        linuxdo_decision_log_archive = archive_linuxdo_decision_log(now, dry_run)
        state_db_checkpoint = checkpoint_state_db(dry_run)
        total_deleted_files = (
            sum(r['deleted_files'] for r in results)
            + sum(r['deleted_files'] for r in browser_profile_cleanup)
            + cron_session_prune.get('deleted_files', 0)
            + noncron_session_prune.get('deleted_files', 0)
            + orphan_session_file_prune.get('deleted_files', 0)
            + chartoo_daily_cleanup.get('deleted_html_files', 0)
        )
        total_deleted_bytes = (
            sum(r['deleted_bytes'] for r in results)
            + sum(r['deleted_bytes'] for r in browser_profile_cleanup)
            + cron_session_prune.get('deleted_bytes', 0)
            + noncron_session_prune.get('deleted_bytes', 0)
            + orphan_session_file_prune.get('deleted_bytes', 0)
            + chartoo_daily_cleanup.get('deleted_html_bytes', 0)
            + linuxdo_decision_log_archive.get('removed_active_bytes', 0)
        )
        total_errors = (
            sum(len(r['errors']) for r in results)
            + sum(len(r['errors']) for r in browser_profile_cleanup)
            + len(cron_session_prune.get('errors') or [])
            + len(noncron_session_prune.get('errors') or [])
            + len(orphan_session_file_prune.get('errors') or [])
            + len(chartoo_daily_cleanup.get('errors') or [])
            + len(linuxdo_decision_log_archive.get('errors') or [])
            + (1 if state_db_checkpoint.get('error') else 0)
        )
        return {
            'status': 'ok' if total_errors == 0 else 'partial',
            'dry_run': dry_run,
            'generated_at_utc': utc_now_iso(),
            'total_deleted_files': total_deleted_files,
            'total_deleted_bytes': total_deleted_bytes,
            'total_errors': total_errors,
            'rules': results,
            'browser_profile_cleanup': browser_profile_cleanup,
            'cron_session_prune': cron_session_prune,
            'noncron_session_prune': noncron_session_prune,
            'orphan_session_file_prune': orphan_session_file_prune,
            'chartoo_daily_cleanup': chartoo_daily_cleanup,
            'linuxdo_decision_log_archive': linuxdo_decision_log_archive,
            'state_db_checkpoint': state_db_checkpoint,
            'preserved_paths': [
                str(PROFILE_ROOT / 'state.db'),
                str(PROFILE_ROOT / 'data' / 'chartoo_itunes_daily' / 'state.sqlite'),
                str(PROFILE_ROOT / 'data' / 'chartoo_itunes_daily' / 'raw'),
                str(PROFILE_ROOT / 'data' / 'chartoo_itunes_daily' / 'reports'),
                str(PROFILE_ROOT / 'data' / 'chiphell_fid26' / 'browser-profile'),
                str(PROFILE_ROOT / 'scripts'),
                str(PROFILE_ROOT / 'cron' / 'jobs.json'),
            ],
            'noncron_session_retention_days': NONCRON_SESSION_RETENTION_DAYS,
            'orphan_session_file_retention_days': ORPHAN_SESSION_FILE_RETENTION_DAYS,
            'preserved_open_session_sources': ['cron', 'telegram', 'cli'],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description='Apply WebOps profile retention cleanup rules')
    parser.add_argument('--dry-run', action='store_true', help='show what would be deleted without deleting files')
    args = parser.parse_args()
    summary = run(dry_run=args.dry_run)
    events.result('ok' if summary.get('status') in {'ok', 'locked'} else 'failed',
                  f"dry_run={args.dry_run}, deleted_files={summary.get('total_deleted_files', 0)}, deleted_bytes={summary.get('total_deleted_bytes', 0)}, errors={summary.get('total_errors', 0)}",
                  changed=not args.dry_run and bool(summary.get('total_deleted_files') or summary.get('total_deleted_bytes')))
    if not args.dry_run and summary.get('status') in {'ok', 'locked'}:
        # Normal scheduled cleanup is not user-visible and should not wake the cron agent.
        print(output.wake_agent_false())
    else:
        print(output.json_dumps_pretty(summary, sort_keys=True))
    return 0 if summary.get('status') in {'ok', 'locked'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
