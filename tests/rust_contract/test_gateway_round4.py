"""Read-only downgrade compatibility and completion-buffer contracts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import pwd
import sqlite3
import stat
import tempfile

import pytest

from test_gateway_contract import (
    ROOT,
    FakeTelegram,
    Gateway,
    assert_rpc_rejected,
    open_context,
    rpc,
    rpc_value,
    run_cli,
    seed_home,
)


COUNTER_KEY = "uncertain:counter:v2"
WATERMARK_KEY = "uncertain:watermark"
RETIRED_COUNTER_KEY = "uncertain:counter:v2:retired"
TRIGGERS_KEY = "uncertain:triggers"


@pytest.fixture
def round4_root():
    with tempfile.TemporaryDirectory(prefix="pylon-round4-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def round4_telegram():
    telegram = FakeTelegram()
    yield telegram
    telegram.close()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _counter(*, pending: int = 0) -> str:
    return _json({"version": 2, "pending": pending, "last_day": ""})


def _seed_home(
    root: Path,
    *,
    kv: dict[str, str] | None = None,
    completion_files: list[tuple[str, bytes, int]] | None = None,
    journal_mode: str = "DELETE",
    create_db: bool = True,
    uncertain_event_ids: tuple[str, ...] = (),
) -> Path:
    home = root / "home"
    home.mkdir(mode=0o700)
    gateway = home / "gateway"
    gateway.mkdir(mode=0o700)

    if create_db:
        database_path = gateway / "state.sqlite3"
        schema = (ROOT / "rust/schema.sql").read_text(encoding="utf-8")
        schema = schema.replace(
            "PRAGMA journal_mode=WAL;", f"PRAGMA journal_mode={journal_mode};", 1
        )
        connection = sqlite3.connect(database_path)
        try:
            connection.executescript(schema)
            if kv:
                connection.executemany(
                    "INSERT INTO kv(key,value) VALUES(?,?)", sorted(kv.items())
                )
            for event_id in uncertain_event_ids:
                connection.execute(
                    """INSERT INTO events(
                           id,profile,job_id,kind,payload,status,attempts,next_attempt,
                           created_at,updated_at,receipt,error,module
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        event_id,
                        "fixture_bot",
                        "fixture_job",
                        "text",
                        "{}",
                        "uncertain",
                        1,
                        0.0,
                        1.0,
                        1.0,
                        None,
                        None,
                        "fixture_module",
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        database_path.chmod(0o600)

    if completion_files is not None:
        completion_dir = gateway / "completions"
        completion_dir.mkdir(mode=0o700)
        completion_dir.chmod(0o700)
        for name, payload, mode in completion_files:
            path = completion_dir / name
            path.write_bytes(payload)
            path.chmod(mode)

    gateway.chmod(0o700)
    home.chmod(0o700)
    return home


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_snapshot(
    root: Path,
    *,
    volatile_entries: tuple[str, ...] = (),
) -> tuple:
    """Capture a fixture tree without following links or reading special files."""

    if not os.path.lexists(root):
        return ("missing",)

    records: list[tuple] = []

    def record(path: Path, relative: str, metadata: os.stat_result) -> None:
        kind = (
            "directory"
            if stat.S_ISDIR(metadata.st_mode)
            else "regular"
            if stat.S_ISREG(metadata.st_mode)
            else "symlink"
            if stat.S_ISLNK(metadata.st_mode)
            else "other"
        )
        is_volatile = relative in volatile_entries
        digest = None
        target = None
        if kind == "regular" and not is_volatile:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            with os.fdopen(descriptor, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        elif kind == "symlink":
            target = os.readlink(path)

        if is_volatile:
            records.append(
                (relative, kind, stat.S_IMODE(metadata.st_mode), metadata.st_uid, metadata.st_gid)
            )
            return
        records.append(
            (
                relative,
                kind,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_uid,
                metadata.st_gid,
                metadata.st_nlink,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                metadata.st_dev,
                metadata.st_ino,
                digest,
                target,
            )
        )

    def visit(directory: Path, relative: str) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda entry: entry.name)
        for entry in ordered:
            path = Path(entry.path)
            child_relative = f"{relative}/{entry.name}" if relative else entry.name
            metadata = entry.stat(follow_symlinks=False)
            record(path, child_relative, metadata)
            if stat.S_ISDIR(metadata.st_mode):
                visit(path, child_relative)

    root_metadata = os.lstat(root)
    record(root, ".", root_metadata)
    if stat.S_ISDIR(root_metadata.st_mode):
        visit(root, "")
    return tuple(records)


def _kv_snapshot(database_path: Path, *, immutable: bool = False) -> tuple[tuple[str, str], ...]:
    options = "mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(f"{database_path.as_uri()}?{options}", uri=True)
    try:
        return tuple(
            connection.execute("SELECT key,value FROM kv ORDER BY key").fetchall()
        )
    finally:
        connection.close()


def _invoke_check(
    home: Path,
    target: str,
    *,
    exit_code: int,
    ok: bool,
    pending: int | None,
    reason: str | None,
) -> dict:
    result = run_cli(home, "downgrade-check", "--target-version", target)
    assert result.returncode == exit_code, result.stderr or result.stdout
    report = json.loads(result.stdout)
    assert set(report) == {
        "ok",
        "target_version",
        "completions_pending",
        "reason",
    }
    assert type(report["ok"]) is bool
    assert report["target_version"] == target
    assert report["completions_pending"] is None or type(report["completions_pending"]) is int
    assert report["reason"] is None or (
        isinstance(report["reason"], str)
        and report["reason"].isascii()
        and len(report["reason"]) <= 48
    )
    assert report == {
        "ok": ok,
        "target_version": target,
        "completions_pending": pending,
        "reason": reason,
    }
    return report


def _assert_read_only_check(
    root: Path,
    home: Path,
    target: str,
    *,
    exit_code: int,
    ok: bool,
    pending: int | None,
    reason: str | None,
    readable_db: bool = True,
    volatile_entries: tuple[str, ...] = (),
) -> dict:
    database_path = home / "gateway/state.sqlite3"
    before_kv = _kv_snapshot(database_path) if readable_db and database_path.is_file() else None
    before_hash = _file_hash(database_path) if database_path.is_file() else None
    before_tree = _tree_snapshot(root, volatile_entries=volatile_entries)

    report = _invoke_check(
        home,
        target,
        exit_code=exit_code,
        ok=ok,
        pending=pending,
        reason=reason,
    )

    after_tree = _tree_snapshot(root, volatile_entries=volatile_entries)
    assert after_tree == before_tree, "downgrade-check changed the fixture tree"
    if before_hash is not None:
        assert _file_hash(database_path) == before_hash, "downgrade-check changed SQLite bytes"
    if before_kv is not None:
        assert _kv_snapshot(database_path) == before_kv, "downgrade-check changed SQLite kv state"
    return report


@pytest.mark.parametrize("completion_files", [None, []], ids=["missing-buffer", "empty-buffer"])
@pytest.mark.parametrize("target", ["0.5.4", "0.5.5", "0.5.6", "0.5.7", "0.5.8", "0.5.14", "0.5.15"])
def test_missing_and_empty_buffer_are_safe_for_current_targets(
    round4_root, completion_files, target
):
    home = _seed_home(
        round4_root,
        kv={COUNTER_KEY: _counter()},
        completion_files=completion_files,
    )
    gateway = home / "gateway"
    assert stat.S_IMODE(gateway.stat().st_mode) == 0o700
    assert stat.S_IMODE((gateway / "state.sqlite3").stat().st_mode) == 0o600
    if completion_files is None:
        assert not (gateway / "completions").exists()
    else:
        assert stat.S_IMODE((gateway / "completions").stat().st_mode) == 0o700

    _assert_read_only_check(
        round4_root,
        home,
        target,
        exit_code=0,
        ok=True,
        pending=0,
        reason=None,
    )
    if completion_files is None:
        assert not (gateway / "completions").exists()


@pytest.mark.parametrize(
    "filename,payload",
    [
        ("receipt.json", b'{"api_version":2,"value":{"fixture":"only"}}\n'),
        ("damaged.json", b'{"api_version":2,"value":'),
        (".partial.tmp", b"fixture partial write\n"),
    ],
    ids=["record", "damaged-json", "temporary-file"],
)
def test_every_regular_buffer_file_blocks_downgrade_without_being_parsed(
    round4_root, filename, payload
):
    home = _seed_home(
        round4_root,
        kv={COUNTER_KEY: _counter()},
        completion_files=[(filename, payload, 0o600)],
    )
    completion_dir = home / "gateway/completions"
    assert stat.S_IMODE(completion_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((completion_dir / filename).stat().st_mode) == 0o600

    _assert_read_only_check(
        round4_root,
        home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=1,
        reason="pending_completions",
    )


def test_buffer_count_includes_json_temporary_and_corrupt_files(round4_root):
    files = [
        ("one.json", b'{"fixture":"first"}\n', 0o600),
        ("two.json", b"not-json", 0o600),
        (".write.tmp", b"partial", 0o600),
    ]
    home = _seed_home(round4_root, kv={COUNTER_KEY: _counter()}, completion_files=files)
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.4",
        exit_code=1,
        ok=False,
        pending=3,
        reason="pending_completions",
    )


@pytest.mark.parametrize(
    "target",
    ["0.5.2", "0.5.16", "v0.5.6", "0.5.6.0"],
    ids=["older", "newer", "prefix", "extra-component"],
)
def test_unknown_target_returns_fixed_json_without_creating_home(round4_root, target):
    home = round4_root / "home"
    before_tree = _tree_snapshot(round4_root)
    _invoke_check(
        home,
        target,
        exit_code=1,
        ok=False,
        pending=None,
        reason="unsupported_target",
    )
    assert _tree_snapshot(round4_root) == before_tree
    assert not home.exists()


def test_legacy_target_accepts_a_valid_old_watermark_without_conversion(round4_root):
    event_id = "fixture-old-uncertain-01"
    kv = {WATERMARK_KEY: _json([event_id])}
    home = _seed_home(
        round4_root,
        kv=kv,
        uncertain_event_ids=(event_id,),
        completion_files=[],
    )
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.3",
        exit_code=0,
        ok=True,
        pending=0,
        reason=None,
    )
    saved = dict(_kv_snapshot(home / "gateway/state.sqlite3"))
    assert COUNTER_KEY not in saved, "legacy check must not prepare a new v2 counter"
    assert saved == kv, "legacy watermark or retired counter was converted"


def test_v3_trigger_marker_requires_a_valid_retired_counter(round4_root):
    kv = {
        WATERMARK_KEY: _json([]),
        TRIGGERS_KEY: _json(3),
        RETIRED_COUNTER_KEY: _counter(),
    }
    home = _seed_home(round4_root, kv=kv, completion_files=[])
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.3",
        exit_code=0,
        ok=True,
        pending=0,
        reason=None,
    )
    saved = dict(_kv_snapshot(home / "gateway/state.sqlite3"))
    assert COUNTER_KEY not in saved
    assert saved == kv


def test_v3_trigger_marker_without_retired_counter_requires_preparation(round4_root):
    kv = {WATERMARK_KEY: _json([]), TRIGGERS_KEY: _json(3)}
    home = _seed_home(round4_root, kv=kv, completion_files=[])
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.3",
        exit_code=1,
        ok=False,
        pending=0,
        reason="legacy_counter_preparation_required",
    )
    assert dict(_kv_snapshot(home / "gateway/state.sqlite3")) == kv


def test_corrupt_retired_counter_is_unavailable(round4_root):
    kv = {
        WATERMARK_KEY: _json([]),
        TRIGGERS_KEY: _json(3),
        RETIRED_COUNTER_KEY: "{fixture-invalid-json",
    }
    home = _seed_home(round4_root, kv=kv, completion_files=[])
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.3",
        exit_code=1,
        ok=False,
        pending=0,
        reason="state_unavailable",
    )
    assert dict(_kv_snapshot(home / "gateway/state.sqlite3")) == kv


def test_legacy_target_requires_preparation_when_v2_counter_is_active(round4_root):
    home = _seed_home(
        round4_root,
        kv={COUNTER_KEY: _counter(), WATERMARK_KEY: _json([])},
        completion_files=[],
    )
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.3",
        exit_code=1,
        ok=False,
        pending=0,
        reason="legacy_counter_preparation_required",
    )


@pytest.mark.parametrize(
    "raw",
    [
        "{fixture-invalid-json",
        _json({"version": 1, "pending": 0, "last_day": ""}),
        _json({"version": 2, "pending": -1, "last_day": ""}),
        _json({"version": 2, "pending": 0, "last_day": 7}),
    ],
    ids=["invalid-json", "wrong-version", "negative-count", "wrong-day-type"],
)
def test_corrupt_v2_counter_is_unavailable_and_never_repaired(round4_root, raw):
    home = _seed_home(round4_root, kv={COUNTER_KEY: raw}, completion_files=[])
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=0,
        reason="state_unavailable",
    )
    assert dict(_kv_snapshot(home / "gateway/state.sqlite3"))[COUNTER_KEY] == raw


def test_missing_or_invalid_legacy_watermark_is_unavailable_and_not_seeded(round4_root):
    for raw in (None, "{}"):
        case_root = round4_root / ("missing" if raw is None else "invalid")
        case_root.mkdir()
        kv = {} if raw is None else {WATERMARK_KEY: raw}
        home = _seed_home(case_root, kv=kv, completion_files=[])
        _assert_read_only_check(
            case_root,
            home,
            "0.5.3",
            exit_code=1,
            ok=False,
            pending=0,
            reason="state_unavailable",
        )
        assert dict(_kv_snapshot(home / "gateway/state.sqlite3")) == kv


def test_missing_database_and_corrupt_database_are_read_only_unavailable(round4_root):
    missing_root = round4_root / "missing-db-case"
    missing_root.mkdir()
    missing_home = _seed_home(missing_root, create_db=False)
    _assert_read_only_check(
        missing_root,
        missing_home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=None,
        reason="state_unavailable",
        readable_db=False,
    )
    assert not (missing_home / "gateway/state.sqlite3").exists()
    assert not (missing_home / "gateway/completions").exists()

    corrupt_root = round4_root / "corrupt-db-case"
    corrupt_root.mkdir()
    corrupt_home = _seed_home(corrupt_root, create_db=False)
    corrupt_db = corrupt_home / "gateway/state.sqlite3"
    corrupt_db.write_bytes(b"fixture: damaged sqlite file\n")
    corrupt_db.chmod(0o600)
    _assert_read_only_check(
        corrupt_root,
        corrupt_home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=0,
        reason="state_unavailable",
        readable_db=False,
    )


@pytest.mark.parametrize(
    "entry_kind",
    ["file-symlink", "directory-symlink", "hardlink", "nested-directory", "fifo"],
)
def test_completion_buffer_rejects_links_hardlinks_nested_and_special_entries(
    round4_root, entry_kind
):
    home = _seed_home(round4_root, kv={COUNTER_KEY: _counter()}, completion_files=[])
    completion_dir = home / "gateway/completions"
    external = round4_root / "outside-fixture"

    if entry_kind == "file-symlink":
        external.write_bytes(b"fixture target\n")
        external.chmod(0o600)
        (completion_dir / "linked.json").symlink_to(external)
    elif entry_kind == "directory-symlink":
        external.mkdir(mode=0o700)
        target = external / "receipt.json"
        target.write_bytes(b"fixture target\n")
        target.chmod(0o600)
        (completion_dir / "linked-directory").symlink_to(external, target_is_directory=True)
    elif entry_kind == "hardlink":
        external.write_bytes(b"fixture hardlink target\n")
        external.chmod(0o600)
        os.link(external, completion_dir / "hardlinked.json")
    elif entry_kind == "nested-directory":
        nested = completion_dir / "nested"
        nested.mkdir(mode=0o700)
        child = nested / "receipt.json"
        child.write_bytes(b"fixture nested file\n")
        child.chmod(0o600)
    elif entry_kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO fixtures require os.mkfifo")
        os.mkfifo(completion_dir / "pipe", 0o600)

    _assert_read_only_check(
        round4_root,
        home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=None,
        reason="state_unavailable",
    )


@pytest.mark.parametrize(
    "unsafe_part",
    ["database-mode", "completion-directory-mode", "completion-file-mode"],
)
def test_private_state_rejects_unsafe_permissions(round4_root, unsafe_part):
    home = _seed_home(
        round4_root,
        kv={COUNTER_KEY: _counter()},
        completion_files=[("receipt.json", b'{"fixture":"private"}\n', 0o600)],
    )
    gateway = home / "gateway"
    database_path = gateway / "state.sqlite3"
    completion_dir = gateway / "completions"
    completion_file = completion_dir / "receipt.json"

    if unsafe_part == "database-mode":
        database_path.chmod(0o644)
    elif unsafe_part == "completion-directory-mode":
        completion_dir.chmod(0o755)
    else:
        completion_file.chmod(0o644)

    _assert_read_only_check(
        round4_root,
        home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=None,
        reason="state_unavailable",
    )


def test_completion_directory_owner_must_match_private_database_owner(round4_root):
    if os.geteuid() != 0:
        pytest.skip("owner mismatch fixture needs a controlled chown target")
    other = next(
        (account for account in pwd.getpwall() if account.pw_uid not in (0, os.getuid())),
        None,
    )
    if other is None:
        pytest.skip("no alternate UID is available for the isolated owner fixture")

    home = _seed_home(round4_root, kv={COUNTER_KEY: _counter()}, completion_files=[])
    completion_dir = home / "gateway/completions"
    os.chown(completion_dir, other.pw_uid, -1)
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=None,
        reason="state_unavailable",
    )


def test_completion_file_owner_must_match_private_database_owner(round4_root):
    if os.geteuid() != 0:
        pytest.skip("owner mismatch fixture needs a controlled chown target")
    other = next(
        (account for account in pwd.getpwall() if account.pw_uid not in (0, os.getuid())),
        None,
    )
    if other is None:
        pytest.skip("no alternate UID is available for the isolated owner fixture")

    home = _seed_home(
        round4_root,
        kv={COUNTER_KEY: _counter()},
        completion_files=[("receipt.json", b'{"fixture":"private"}\n', 0o600)],
    )
    completion_file = home / "gateway/completions/receipt.json"
    os.chown(completion_file, other.pw_uid, -1)
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.6",
        exit_code=1,
        ok=False,
        pending=None,
        reason="state_unavailable",
    )


def test_closed_wal_database_is_read_only_and_creates_no_sidecars(round4_root):
    expected_kv = {COUNTER_KEY: _counter(), WATERMARK_KEY: _json([])}
    home = _seed_home(
        round4_root,
        kv=expected_kv,
        completion_files=None,
        journal_mode="WAL",
    )
    database_path = home / "gateway/state.sqlite3"
    assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()

    before_tree = _tree_snapshot(round4_root)
    before_hash = _file_hash(database_path)
    report = _invoke_check(
        home,
        "0.5.6",
        exit_code=0,
        ok=True,
        pending=0,
        reason=None,
    )
    assert report["ok"] is True
    assert _tree_snapshot(round4_root) == before_tree
    assert _file_hash(database_path) == before_hash
    assert _kv_snapshot(database_path, immutable=True) == tuple(sorted(expected_kv.items()))
    assert not Path(f"{database_path}-wal").exists()
    assert not Path(f"{database_path}-shm").exists()


def test_active_wal_counter_is_seen_without_checkpointing_or_sidecar_changes(round4_root):
    watermark = _json(["fixture-old-uncertain-02"])
    home = _seed_home(
        round4_root,
        kv={WATERMARK_KEY: watermark},
        completion_files=[],
        journal_mode="WAL",
    )
    database_path = home / "gateway/state.sqlite3"
    writer = sqlite3.connect(database_path, timeout=5.0, isolation_level=None)
    try:
        assert writer.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        writer.execute(
            "INSERT INTO kv(key,value) VALUES(?,?)", (COUNTER_KEY, _counter())
        )
        assert writer.execute("SELECT value FROM kv WHERE key=?", (COUNTER_KEY,)).fetchone() == (
            _counter(),
        )
        wal_path = Path(f"{database_path}-wal")
        shm_path = Path(f"{database_path}-shm")
        assert wal_path.is_file() and wal_path.stat().st_size > 0
        assert shm_path.is_file()

        immutable = sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro&immutable=1", uri=True
        )
        try:
            assert immutable.execute(
                "SELECT value FROM kv WHERE key=?", (COUNTER_KEY,)
            ).fetchone() is None, "fixture counter must exist only in the uncheckpointed WAL"
        finally:
            immutable.close()

        volatile = (f"home/gateway/state.sqlite3-shm",)
        before_tree = _tree_snapshot(round4_root, volatile_entries=volatile)
        before_db_hash = _file_hash(database_path)
        before_wal_hash = _file_hash(wal_path)
        _invoke_check(
            home,
            "0.5.3",
            exit_code=1,
            ok=False,
            pending=0,
            reason="legacy_counter_preparation_required",
        )
        assert _tree_snapshot(round4_root, volatile_entries=volatile) == before_tree
        assert _file_hash(database_path) == before_db_hash
        assert _file_hash(wal_path) == before_wal_hash
        assert writer.execute(
            "SELECT value FROM kv WHERE key=?", (COUNTER_KEY,)
        ).fetchone() == (_counter(),)
        assert writer.execute("SELECT value FROM kv WHERE key=?", (WATERMARK_KEY,)).fetchone() == (
            watermark,
        )
    finally:
        writer.close()


def _grant_diagnostics(home: Path, module_id: str) -> None:
    manifest_path = home / "modules" / module_id / "module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["capabilities"] = ["diagnostics:read"]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def test_diagnostics_count_is_top_level_and_permissions_stay_unchanged(
    round4_root, round4_telegram
):
    home = round4_root / "diagnostics-home"
    seed_home(
        home,
        round4_telegram,
        [("bot_one", "101")],
        [
            {"id": "ordinary", "topic": "topic_fixture", "job_id": "job_ordinary"},
            {"id": "observer", "topic": "topic_fixture", "job_id": "job_observer"},
        ],
        {"topic_fixture": "bot_one"},
    )
    _grant_diagnostics(home, "observer")
    gateway_dir = home / "gateway"
    gateway_dir.chmod(0o700)
    (gateway_dir / "secrets").chmod(0o700)
    completion_dir = gateway_dir / "completions"
    completion_dir.mkdir(mode=0o700)
    completion_dir.chmod(0o700)
    gateway = Gateway(home, round4_telegram)

    try:
        gateway.start()
        database_path = gateway_dir / "state.sqlite3"
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(completion_dir.stat().st_mode) == 0o700

        observer_context = open_context(home, "observer", "job_observer")["token"]
        ordinary_context = open_context(home, "ordinary", "job_ordinary")["token"]

        local = rpc(home, "diagnostics")
        assert local["ok"] is True, local
        local_value = local["value"]
        assert type(local_value["completions_pending"]) is int
        assert local_value["completions_pending"] == 0

        allowed = rpc(home, "diagnostics", context=observer_context)
        assert allowed["ok"] is True, allowed
        assert type(allowed["value"]["completions_pending"]) is int
        assert allowed["value"]["completions_pending"] == 0

        denied = rpc(home, "diagnostics", context=ordinary_context)
        assert_rpc_rejected(denied)
    finally:
        gateway.stop()

    assert gateway.proc is not None and gateway.proc.poll() is not None
    _assert_read_only_check(
        round4_root,
        home,
        "0.5.6",
        exit_code=0,
        ok=True,
        pending=0,
        reason=None,
    )
