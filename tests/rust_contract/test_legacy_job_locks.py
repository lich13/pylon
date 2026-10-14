"""Linux root contracts for locks left by the previous runtime UID."""

from __future__ import annotations

import errno
import fcntl
import json
import os
from pathlib import Path
import pwd
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile

import pytest

from test_gateway_contract import BIN, ROOT, FakeTelegram, running_gateway, seed_home, wait_for


JOB_ID = "job_alpha"
RUN_MODES = [
    pytest.param(0o3770, id="root-group-sticky-3770"),
    pytest.param(0o1777, id="world-writable-sticky-1777"),
]


def _require_linux_root_lock_fixture() -> None:
    if sys.platform != "linux":
        pytest.skip("legacy protected_regular behavior is tested on Linux only")
    if os.geteuid() != 0:
        pytest.skip("legacy lock ownership requires a Linux root test process")

    protected_regular = Path("/proc/sys/fs/protected_regular")
    try:
        value = int(protected_regular.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        pytest.fail("cannot inspect Linux fs.protected_regular")
    if value != 2:
        pytest.fail(f"this regression requires fs.protected_regular=2; found {value}")

    if not os.environ.get("PYLON_GATEWAY_BIN"):
        pytest.fail("set PYLON_GATEWAY_BIN to the already-built Linux gateway binary")
    if not BIN.is_file() or not os.access(BIN, os.X_OK):
        pytest.fail(f"PYLON_GATEWAY_BIN is missing or not executable: {BIN}")


@pytest.fixture
def root_workdir():
    _require_linux_root_lock_fixture()
    base = Path("/run")
    try:
        metadata = base.stat()
    except OSError as error:
        pytest.fail(f"cannot inspect trusted root fixture parent /run: {error}")
    if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
        pytest.fail("/run is not a root-owned, non-writable fixture parent")

    try:
        temporary = tempfile.TemporaryDirectory(prefix="pylon-legacy-lock-", dir=base)
    except OSError as error:
        pytest.fail(f"cannot create an isolated fixture under /run: {error}")
    try:
        root = Path(temporary.name)
        root.chmod(0o755)
        yield root
    finally:
        temporary.cleanup()


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


def _nonroot_identity() -> tuple[int, int]:
    for account in sorted(pwd.getpwall(), key=lambda item: item.pw_uid):
        if 0 < account.pw_uid <= 65535 and 0 < account.pw_gid <= 65535:
            return account.pw_uid, account.pw_gid
    pytest.fail("no non-root UID/GID is available for the module runtime fixture")


def _seed_home(root: Path, telegram: FakeTelegram, run_mode: int) -> tuple[Path, Path, int, int]:
    uid, gid = _nonroot_identity()
    home = root / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": JOB_ID}],
        {"topic_alpha": "bot_one"},
    )

    for directory in (
        home,
        home / "config",
        home / "gateway",
        home / "gateway/secrets",
        home / "modules",
        home / "modules/alpha",
        home / "profiles",
        home / "profiles/default",
    ):
        directory.chmod(0o755)

    settings_path = home / "config/bots.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["runtime_users"] = {"alpha": {"uid": uid, "gid": gid}}
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    run_dir = home / "run"
    run_dir.mkdir()
    run_gid = gid if run_mode == 0o3770 else 0
    os.chown(run_dir, 0, run_gid)
    run_dir.chmod(run_mode)
    metadata = run_dir.stat()
    assert metadata.st_uid == 0
    assert metadata.st_gid == run_gid
    assert stat.S_IMODE(metadata.st_mode) == run_mode
    return home, run_dir, uid, gid


def _write_job(home: Path, script: str) -> None:
    (home / "modules/alpha/job.sh").write_text(script, encoding="utf-8")


def _create_legacy_lock(path: Path, uid: int, gid: int, contents: bytes) -> None:
    path.write_bytes(contents)
    os.chown(path, uid, gid)
    path.chmod(0o660)
    metadata = path.stat()
    assert metadata.st_uid == uid
    assert metadata.st_gid == gid
    assert stat.S_IMODE(metadata.st_mode) == 0o660


def _regular_snapshot(path: Path) -> tuple[int, int, int, int, int, bytes]:
    metadata = path.stat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        path.read_bytes(),
    )


def _assert_old_create_open_is_denied(path: Path) -> None:
    # The pre-fix OpenOptions used O_CREAT|O_APPEND on this existing lock.
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o660,
        )
    except OSError as error:
        assert error.errno == errno.EACCES, error
    else:
        os.close(descriptor)
        pytest.fail("fixture did not reproduce protected_regular denial of O_CREAT")


def _offline_env(telegram: FakeTelegram, invocation_id: str) -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
        if key in os.environ
    }
    env["PYLON_TEST_TELEGRAM"] = telegram.url
    env["INVOCATION_ID"] = invocation_id
    return env


def _execute(home: Path, telegram: FakeTelegram, invocation_id: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BIN), "--home", str(home), "--offline", "execute", JOB_ID],
        cwd=ROOT,
        env=_offline_env(telegram, invocation_id),
        capture_output=True,
        text=True,
        timeout=12,
    )


def _run_statuses(home: Path) -> list[tuple[str]]:
    with sqlite3.connect(home / "gateway/state.sqlite3", timeout=3) as database:
        return database.execute(
            "SELECT status FROM runs WHERE job_id=? ORDER BY rowid", (JOB_ID,)
        ).fetchall()


def _legacy_symlink_snapshot(path: Path) -> tuple[int, int, int, int, int, str]:
    metadata = path.lstat()
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        os.readlink(path),
    )


@pytest.mark.parametrize("run_mode", RUN_MODES)
def test_root_execute_opens_legacy_job_lock_and_prevents_overlap(
    root_workdir, telegram, run_mode
):
    marker_dir = root_workdir / "markers"
    marker_dir.mkdir()
    marker_dir.chmod(0o1777)
    marker = marker_dir / "job-runs"
    release = marker_dir / "release-job"

    home, run_dir, uid, gid = _seed_home(root_workdir, telegram, run_mode)
    _write_job(
        home,
        "#!/bin/sh\nset -eu\n"
        f"printf 'started\\n' >> {shlex.quote(str(marker))}\n"
        f"while [ ! -e {shlex.quote(str(release))} ]; do /bin/sleep 0.05; done\n",
    )
    legacy_lock = run_dir / f"{JOB_ID}.lock"
    _create_legacy_lock(legacy_lock, uid, gid, b"legacy job lock\n")
    legacy_snapshot = _regular_snapshot(legacy_lock)
    _assert_old_create_open_is_denied(legacy_lock)

    first = None
    with running_gateway(home, telegram):
        first = subprocess.Popen(
            [str(BIN), "--home", str(home), "--offline", "execute", JOB_ID],
            cwd=ROOT,
            env=_offline_env(telegram, "legacy-lock-first"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            def started():
                if marker.is_file() and marker.read_text(encoding="utf-8").splitlines() == ["started"]:
                    return True
                if first.poll() is not None:
                    stdout, stderr = first.communicate()
                    raise AssertionError(
                        "first execute exited before the fixture job started "
                        f"({first.returncode}): {stderr or stdout}"
                    )
                return False

            wait_for(started, timeout=8.0)
            duplicate = _execute(home, telegram, "legacy-lock-duplicate")
            assert duplicate.returncode == 0, duplicate.stderr or duplicate.stdout
            assert duplicate.stdout.strip() == ""
            assert marker.read_text(encoding="utf-8").splitlines() == ["started"]

            release.touch()
            stdout, stderr = first.communicate(timeout=8.0)
            assert first.returncode == 0, stderr or stdout
            assert json.loads(stdout.strip()) == {"task": JOB_ID, "status": "ok"}
            assert marker.read_text(encoding="utf-8").splitlines() == ["started"]
            assert _run_statuses(home) == [("ok",)]
        finally:
            if first.poll() is None:
                release.touch(exist_ok=True)
                try:
                    first.communicate(timeout=5.0)
                except subprocess.TimeoutExpired:
                    first.terminate()
                    try:
                        first.wait(timeout=3.0)
                    except subprocess.TimeoutExpired:
                        first.kill()
                        first.wait(timeout=3.0)

    assert _regular_snapshot(legacy_lock) == legacy_snapshot


@pytest.mark.parametrize("run_mode", RUN_MODES)
def test_root_execute_skips_busy_legacy_quota_lock_without_replacing_it(
    root_workdir, telegram, run_mode
):
    marker_dir = root_workdir / "markers"
    marker_dir.mkdir()
    marker_dir.chmod(0o1777)
    marker = marker_dir / "job-runs"
    home, run_dir, uid, gid = _seed_home(root_workdir, telegram, run_mode)
    _write_job(
        home,
        "#!/bin/sh\nset -eu\n"
        f"printf 'started\\n' >> {shlex.quote(str(marker))}\n",
    )

    legacy_quota = run_dir / "quota-topic_alpha-process-0.lock"
    _create_legacy_lock(legacy_quota, uid, gid, b"legacy quota lock\n")
    legacy_snapshot = _regular_snapshot(legacy_quota)
    _assert_old_create_open_is_denied(legacy_quota)
    descriptor = os.open(legacy_quota, os.O_RDWR)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with running_gateway(home, telegram):
            execution = _execute(home, telegram, "legacy-quota-busy-slot")
            assert execution.returncode == 0, execution.stderr or execution.stdout
            assert json.loads(execution.stdout.strip()) == {
                "task": JOB_ID,
                "status": "ok",
            }
            assert marker.read_text(encoding="utf-8").splitlines() == ["started"]
            assert _run_statuses(home) == [("ok",)]
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert _regular_snapshot(legacy_quota) == legacy_snapshot


@pytest.mark.parametrize("run_mode", RUN_MODES)
@pytest.mark.parametrize("lock_kind", ["job", "quota"])
def test_root_execute_rejects_legacy_symlink_locks_without_touching_target(
    root_workdir, telegram, run_mode, lock_kind
):
    marker_dir = root_workdir / "markers"
    marker_dir.mkdir()
    marker_dir.chmod(0o1777)
    marker = marker_dir / "job-runs"
    home, run_dir, uid, gid = _seed_home(root_workdir, telegram, run_mode)
    _write_job(
        home,
        "#!/bin/sh\nset -eu\n"
        f"printf 'started\\n' >> {shlex.quote(str(marker))}\n",
    )

    target = root_workdir / f"{lock_kind}-target"
    target.write_bytes(b"protected symlink target\n")
    os.chown(target, uid, gid)
    target.chmod(0o660)
    target_snapshot = _regular_snapshot(target)

    link = (
        run_dir / f"{JOB_ID}.lock"
        if lock_kind == "job"
        else run_dir / "quota-topic_alpha-process-0.lock"
    )
    link.symlink_to(target)
    link_snapshot = _legacy_symlink_snapshot(link)

    execution = _execute(home, telegram, f"legacy-{lock_kind}-symlink")
    assert execution.returncode != 0, execution.stdout
    assert "symbolic link" in execution.stderr.lower() or "too many levels" in execution.stderr.lower()
    assert not marker.exists()
    assert _legacy_symlink_snapshot(link) == link_snapshot
    assert _regular_snapshot(target) == target_snapshot
