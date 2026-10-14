"""Root/systemd contracts for live module activation on Linux."""

from __future__ import annotations

import grp
import hashlib
import json
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from test_gateway_contract import BIN, ROOT, FakeTelegram, run_cli, seed_home, wait_for, write_module


SYSTEMD_DIR = Path("/etc/systemd/system")
SYSTEMD_DIRS = (
    Path("/etc/systemd/system"),
    Path("/run/systemd/system"),
    Path("/run/systemd/generator.early"),
    Path("/run/systemd/generator"),
    Path("/run/systemd/generator.late"),
    Path("/usr/local/lib/systemd/system"),
    Path("/usr/lib/systemd/system"),
    Path("/lib/systemd/system"),
)
GATEWAY_UNIT = "pylon-gateway.service"
MODULE_TEMPLATE = "pylon-module@.service"


def _command(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["SYSTEMD_PAGER"] = "cat"
    env["SYSTEMD_COLORS"] = "0"
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = _command(["/usr/bin/systemctl", *args])
    if check and result.returncode != 0:
        raise AssertionError(
            f"systemctl {' '.join(args)} failed ({result.returncode}): "
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _require_linux_root_systemd() -> None:
    if sys.platform != "linux":
        pytest.skip("this contract requires Linux systemd")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        pytest.skip("run this contract as root in the Linux systemd CI VM")
    if not os.environ.get("PYLON_GATEWAY_BIN"):
        pytest.fail("set PYLON_GATEWAY_BIN to the already-built gateway binary")
    if not BIN.is_file() or not os.access(BIN, os.X_OK):
        pytest.fail(f"PYLON_GATEWAY_BIN is missing or not executable: {BIN}")
    if not Path("/run").is_dir() or not os.access("/run", os.W_OK):
        pytest.fail("the Linux systemd fixture requires writable /run")
    if not Path("/usr/bin/systemctl").is_file():
        pytest.fail("systemctl is unavailable in the Linux systemd CI VM")
    manager = _systemctl("show", "--property=Version", "--value", check=False)
    if manager.returncode != 0 or not manager.stdout.strip():
        pytest.fail(
            "a reachable systemd system manager is required; "
            f"systemctl returned {manager.returncode}: {manager.stderr.strip()}"
        )


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _fragment_path(unit: str) -> str:
    result = _systemctl(
        "show", unit, "--property=FragmentPath", "--value", check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _refuse_existing_units(units: list[str]) -> None:
    for unit in units:
        relative_paths = [Path(unit), Path(f"{unit}.d")]
        if unit.endswith(".service"):
            relative_paths.extend(
                [
                    Path("multi-user.target.wants") / unit,
                    Path("default.target.wants") / unit,
                ]
            )
        for directory in SYSTEMD_DIRS:
            for relative in relative_paths:
                path = directory / relative
                if _present(path):
                    raise RuntimeError(
                        f"refusing to replace or use existing systemd resource {path}"
                    )
        fragment = _fragment_path(unit)
        if fragment:
            raise RuntimeError(
                f"refusing to use existing systemd unit {unit} from {fragment}"
            )


def _require_port_free(port: int) -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", port))
    except OSError as error:
        raise RuntimeError(
            f"refusing to start the fixture gateway while 127.0.0.1:{port} is occupied"
        ) from error
    finally:
        listener.close()


def _run_checked(args: list[str]) -> subprocess.CompletedProcess:
    result = _command(args)
    if result.returncode != 0:
        raise AssertionError(
            f"{' '.join(args)} failed ({result.returncode}): "
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def _ensure_contract_group() -> tuple[int, bool]:
    try:
        group = grp.getgrnam("pylon")
        created = False
    except KeyError:
        _run_checked(["/usr/sbin/groupadd", "--system", "pylon"])
        group = grp.getgrnam("pylon")
        created = True
    if group.gr_gid == 0:
        raise RuntimeError("refusing to use the root GID for the pylon group")
    return group.gr_gid, created


def _ensure_gateway_user() -> tuple[int, bool]:
    try:
        account = pwd.getpwnam("pylon-gateway")
        created = False
    except KeyError:
        _run_checked(
            [
                "/usr/sbin/useradd",
                "--system",
                "--no-create-home",
                "--home-dir",
                "/nonexistent",
                "--shell",
                "/usr/sbin/nologin",
                "--gid",
                "pylon",
                "pylon-gateway",
            ]
        )
        account = pwd.getpwnam("pylon-gateway")
        created = True
    if account.pw_uid == 0:
        raise RuntimeError("refusing to run the fixture gateway as the root account")
    return account.pw_uid, created


def _runtime_user_name(module_id: str) -> str:
    digest = hashlib.sha256(module_id.encode()).hexdigest()[:6]
    return f"pyl-m-{module_id[:17]}-{digest}"


def _chown_fixture_tree(path: Path, uid: int, gid: int) -> None:
    for current, directories, files in os.walk(path, followlinks=False):
        os.chown(current, uid, gid, follow_symlinks=False)
        for name in directories + files:
            child = Path(current) / name
            os.chown(child, uid, gid, follow_symlinks=False)


def _write_new_unit(path: Path, content: str, owned: dict[Path, bytes]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    owned[path] = content.encode()
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(owned[path])


def _source_hash() -> str:
    digest = hashlib.sha256()
    paths = [ROOT / "Cargo.toml", *sorted((ROOT / "rust").rglob("*.rs"))]
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _binary_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _main_pid(unit: str) -> int | None:
    result = _systemctl(
        "show", unit, "--property=MainPID", "--value", check=False
    )
    if result.returncode != 0:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 1 else None


def _wait_main_pid(unit: str, *, different_from: int | None = None) -> int:
    return wait_for(
        lambda: (
            pid
            if (pid := _main_pid(unit)) is not None and pid != different_from
            else None
        ),
        timeout=20.0,
        interval=0.1,
    )


def _write_shell_module(
    source: Path,
    module_id: str,
    topic: str,
    *,
    version: str,
    command_name: str,
    web_path: str,
    health_status: str = "ok",
) -> Path:
    write_module(source, module_id, topic, command_name=command_name)
    manifest_path = source / "module.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["services"] = {
        "worker": {
            "entry": ["bash", "service.sh"],
            "workspace": "default",
            "topic": topic,
        }
    }
    manifest["web"] = {
        web_path: {
            "service": "worker",
            "upstream": "http://127.0.0.1:18080/",
        }
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (source / "job.sh").unlink(missing_ok=True)
    health = json.dumps(
        {"api_version": 2, "value": {"status": health_status}},
        separators=(",", ":"),
    )
    (source / "handler.sh").write_text(
        f"#!/bin/sh\nset -eu\ncat >/dev/null\nprintf '%s\\n' '{health}'\n",
        encoding="utf-8",
    )
    (source / "service.sh").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        f"printf '%s:%s\\n' '{version}' \"$(id -u)\" "
        f"> \"$PYLON_HOME/data/{module_id}.service\"\n"
        "exec /bin/sleep 600\n",
        encoding="utf-8",
    )
    (source / "handler.sh").chmod(0o755)
    (source / "service.sh").chmod(0o755)
    return source


def _pack(home: Path, source: Path, archive: Path) -> str:
    packed = run_cli(
        home,
        "modules",
        "pack",
        str(source),
        "--output",
        str(archive),
    )
    assert packed.returncode == 0, packed.stderr or packed.stdout
    return json.loads(packed.stdout)["sha256"]


def _gateway_cli(home: Path, *args: str) -> subprocess.CompletedProcess:
    return _command(
        [str(BIN), "--home", str(home), *args],
        timeout=90.0,
    )


def _install(home: Path, archive: Path, checksum: str) -> dict:
    result = _gateway_cli(
        home,
        "modules",
        "install",
        str(archive),
        "--sha256",
        checksum,
        "--activate",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


def _read_registry(home: Path) -> dict:
    return json.loads((home / "modules/registry.json").read_text(encoding="utf-8"))


def _wait_marker(home: Path, module_id: str, version: str) -> tuple[str, int]:
    marker = home / "data" / f"{module_id}.service"
    content = wait_for(
        lambda: (
            value
            if marker.is_file()
            and (value := marker.read_text(encoding="utf-8")).startswith(
                f"{version}:"
            )
            else None
        ),
        timeout=15.0,
        interval=0.1,
    )
    recorded_version, recorded_uid = content.strip().split(":", 1)
    return recorded_version, int(recorded_uid)


def _read_runtime_uid(home: Path, module_id: str) -> int:
    settings = json.loads((home / "config/bots.json").read_text(encoding="utf-8"))
    return settings["runtime_users"][module_id]["uid"]


def _stop_owned_unit(unit: str) -> list[str]:
    _systemctl("disable", "--now", unit, check=False)
    active = _systemctl("is-active", "--quiet", unit, check=False)
    enabled = _systemctl("is-enabled", "--quiet", unit, check=False)
    if active.returncode == 0 or enabled.returncode == 0:
        return [f"could not stop or disable test-owned unit {unit}"]
    return []


def _cleanup(
    *,
    unit_files: dict[Path, bytes],
    units: list[str],
    module_users: list[str],
    created_gateway_user: tuple[str, int] | None,
    created_group_gid: int | None,
) -> list[str]:
    errors: list[str] = []
    if unit_files:
        for unit in units:
            errors.extend(_stop_owned_unit(unit))
        try:
            _systemctl("daemon-reload")
        except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"systemd daemon-reload before unit cleanup failed: {error}")
        for path, original in unit_files.items():
            if not _present(path):
                continue
            if path.is_symlink() or not path.is_file() or path.read_bytes() != original:
                errors.append(f"left changed test unit untouched: {path}")
                continue
            path.unlink()
        try:
            _systemctl("daemon-reload")
        except (AssertionError, OSError, subprocess.TimeoutExpired) as error:
            errors.append(f"systemd daemon-reload after unit cleanup failed: {error}")

    for name in module_users:
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            continue
        result = _command(["/usr/sbin/userdel", name])
        if result.returncode != 0:
            errors.append(
                f"could not remove test runtime user {name}: {result.stderr.strip()}"
            )
        else:
            try:
                remaining = pwd.getpwnam(name)
            except KeyError:
                pass
            else:
                if remaining.pw_uid == account.pw_uid:
                    errors.append(f"test runtime user remains after userdel: {name}")

    if created_gateway_user is not None:
        name, uid = created_gateway_user
        try:
            account = pwd.getpwnam(name)
        except KeyError:
            account = None
        if account is not None and account.pw_uid == uid:
            result = _command(["/usr/sbin/userdel", name])
            if result.returncode != 0:
                errors.append(
                    f"could not remove test gateway user {name}: {result.stderr.strip()}"
                )
    if created_group_gid is not None:
        try:
            group = grp.getgrnam("pylon")
        except KeyError:
            group = None
        if group is not None and group.gr_gid == created_group_gid:
            result = _command(["/usr/sbin/groupdel", "pylon"])
            if result.returncode != 0:
                errors.append(f"could not remove test group pylon: {result.stderr.strip()}")
    return errors


def test_live_activation_and_rollback_restart_only_the_changed_module():
    _require_linux_root_systemd()

    suffix = uuid.uuid4().hex[:10]
    target_id = f"act{suffix}"
    bystander_id = f"ref{suffix}"
    topic_target = f"topic{suffix}a"
    topic_bystander = f"topic{suffix}b"
    target_unit = f"pylon-module@{target_id}--worker.service"
    bystander_unit = f"pylon-module@{bystander_id}--worker.service"
    units = [target_unit, bystander_unit, GATEWAY_UNIT]
    runtime_users = [_runtime_user_name(target_id), _runtime_user_name(bystander_id)]

    _refuse_existing_units([GATEWAY_UNIT, MODULE_TEMPLATE, target_unit, bystander_unit])
    _require_port_free(8793)
    for name in runtime_users:
        try:
            pwd.getpwnam(name)
        except KeyError:
            continue
        raise RuntimeError(f"refusing to use pre-existing test runtime account {name}")

    telegram = FakeTelegram()
    unit_files: dict[Path, bytes] = {}
    created_gateway_user: tuple[str, int] | None = None
    created_group_gid: int | None = None
    test_error: BaseException | None = None

    try:
        with tempfile.TemporaryDirectory(prefix="pyl-act-", dir="/run") as raw_root:
            fixture_root = Path(raw_root)
            fixture_root.chmod(0o755)
            try:
                group_gid, group_created = _ensure_contract_group()
                if group_created:
                    created_group_gid = group_gid
                gateway_uid, gateway_created = _ensure_gateway_user()
                if gateway_created:
                    created_gateway_user = ("pylon-gateway", gateway_uid)

                home = fixture_root / "home"
                seed_home(
                    home,
                    telegram,
                    [("bot_fixture", "101")],
                    [],
                    {topic_target: "bot_fixture", topic_bystander: "bot_fixture"},
                )
                settings_path = home / "config/bots.json"
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                settings["bots"]["bot_fixture"]["enabled"] = False
                settings_path.write_text(
                    json.dumps(settings, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (home / "bin").mkdir()
                binary_copy = home / "bin/pylon"
                shutil.copyfile(BIN, binary_copy)
                binary_copy.chmod(0o755)
                (home / "data").mkdir()
                (home / "run").mkdir()
                _chown_fixture_tree(home, gateway_uid, group_gid)
                home.chmod(0o750)
                (home / "gateway/secrets").chmod(0o700)
                (home / "data").chmod(0o770)
                (home / "run").chmod(0o770)

                rendered = fixture_root / "rendered"
                render_result = _gateway_cli(home, "modules", "render", str(rendered))
                assert render_result.returncode == 0, (
                    render_result.stderr or render_result.stdout
                )
                gateway_template = (rendered / GATEWAY_UNIT).read_text(encoding="utf-8")
                module_template = (rendered / MODULE_TEMPLATE).read_text(encoding="utf-8")
                assert (
                    "After=pylon-gateway.service\nRequires=pylon-gateway.service\n"
                    in module_template
                )

                readonly_path = "ReadWritePaths=/srv/pylon\n"
                assert gateway_template.count(readonly_path) == 1
                gateway_template = gateway_template.replace(
                    readonly_path, f"ReadWritePaths={home}\n", 1
                )
                service_section = "[Service]\n"
                assert gateway_template.count(service_section) == 1
                gateway_template = gateway_template.replace(
                    service_section,
                    service_section
                    + f"Environment=PYLON_TEST_TELEGRAM={telegram.url}\n",
                    1,
                )
                exec_start = f"ExecStart={binary_copy} --home {home} gateway\n"
                assert gateway_template.count(exec_start) == 1
                gateway_template = gateway_template.replace(
                    exec_start,
                    f"ExecStart={binary_copy} --home {home} --offline gateway\n",
                    1,
                )

                _write_new_unit(
                    SYSTEMD_DIR / GATEWAY_UNIT, gateway_template, unit_files
                )
                _write_new_unit(
                    SYSTEMD_DIR / MODULE_TEMPLATE, module_template, unit_files
                )
                _systemctl("daemon-reload")
                _systemctl("enable", "--now", GATEWAY_UNIT)
                gateway_pid = _wait_main_pid(GATEWAY_UNIT)
                wait_for(
                    lambda: _gateway_cli(home, "rpc", "health").returncode == 0,
                    timeout=20.0,
                    interval=0.1,
                )
                assert telegram.method_counts() == {}
                assert _binary_hash(binary_copy) == _binary_hash(BIN)

                source_hash_before = _source_hash()
                binary_hash_before = _binary_hash(BIN)
                deployed_binary_hash_before = _binary_hash(binary_copy)

                def assert_gateway_and_source_unchanged() -> None:
                    assert _main_pid(GATEWAY_UNIT) == gateway_pid
                    assert _source_hash() == source_hash_before
                    assert _binary_hash(BIN) == binary_hash_before
                    assert _binary_hash(binary_copy) == deployed_binary_hash_before

                def create_install(
                    module_id: str,
                    topic: str,
                    *,
                    version: str,
                    command_name: str,
                    web_path: str,
                    health_status: str = "ok",
                ) -> tuple[Path, str]:
                    source = fixture_root / f"{module_id}-{version}"
                    _write_shell_module(
                        source,
                        module_id,
                        topic,
                        version=version,
                        command_name=command_name,
                        web_path=web_path,
                        health_status=health_status,
                    )
                    archive = fixture_root / f"{module_id}-{version}.tar.gz"
                    checksum = _pack(home, source, archive)
                    return archive, checksum

                target_web_path = f"aweb{suffix}"
                bystander_web_path = f"rweb{suffix}"
                target_v1_archive, target_v1_sha = create_install(
                    target_id,
                    topic_target,
                    version="target-v1",
                    command_name=f"a{suffix[:6]}v1",
                    web_path=target_web_path,
                )
                target_v1 = _install(home, target_v1_archive, target_v1_sha)
                assert target_v1["id"] == target_id and target_v1["changed"] is True
                target_v1_pid = _wait_main_pid(target_unit)
                target_uid = _read_runtime_uid(home, target_id)
                assert target_uid > 0
                assert pwd.getpwuid(target_uid).pw_name == runtime_users[0]
                assert pwd.getpwuid(target_uid).pw_gid == group_gid
                assert _wait_marker(home, target_id, "target-v1") == (
                    "target-v1",
                    target_uid,
                )
                assert_gateway_and_source_unchanged()

                bystander_v1_archive, bystander_v1_sha = create_install(
                    bystander_id,
                    topic_bystander,
                    version="bystander-v1",
                    command_name=f"r{suffix[:6]}v1",
                    web_path=bystander_web_path,
                )
                bystander_v1 = _install(home, bystander_v1_archive, bystander_v1_sha)
                assert bystander_v1["id"] == bystander_id
                bystander_pid = _wait_main_pid(bystander_unit)
                bystander_uid = _read_runtime_uid(home, bystander_id)
                assert bystander_uid > 0 and bystander_uid != target_uid
                assert pwd.getpwuid(bystander_uid).pw_name == runtime_users[1]
                assert pwd.getpwuid(bystander_uid).pw_gid == group_gid
                assert _wait_marker(home, bystander_id, "bystander-v1") == (
                    "bystander-v1",
                    bystander_uid,
                )
                assert _main_pid(target_unit) == target_v1_pid
                assert_gateway_and_source_unchanged()

                target_v2_archive, target_v2_sha = create_install(
                    target_id,
                    topic_target,
                    version="target-v2",
                    command_name=f"a{suffix[:6]}v2",
                    web_path=target_web_path,
                )
                target_v2 = _install(home, target_v2_archive, target_v2_sha)
                assert target_v2["id"] == target_id and target_v2["changed"] is True
                target_v2_pid = _wait_main_pid(
                    target_unit, different_from=target_v1_pid
                )
                assert _wait_marker(home, target_id, "target-v2") == (
                    "target-v2",
                    target_uid,
                )
                assert _main_pid(bystander_unit) == bystander_pid
                assert_gateway_and_source_unchanged()

                registry_v2 = _read_registry(home)
                active_v2 = registry_v2["modules"][target_id]
                assert active_v2.endswith(target_v2["hash"])
                rejected_source = fixture_root / f"{target_id}-target-bad"
                _write_shell_module(
                    rejected_source,
                    target_id,
                    topic_target,
                    version="target-bad",
                    command_name=f"a{suffix[:6]}bad",
                    web_path=target_web_path,
                    health_status="failed",
                )
                rejected_archive = fixture_root / f"{target_id}-target-bad.tar.gz"
                rejected_sha = _pack(home, rejected_source, rejected_archive)
                rejected = _gateway_cli(
                    home,
                    "modules",
                    "install",
                    str(rejected_archive),
                    "--sha256",
                    rejected_sha,
                    "--activate",
                )
                assert rejected.returncode != 0
                assert "module health check rejected" in rejected.stderr
                assert _read_registry(home) == registry_v2
                assert _main_pid(target_unit) == target_v2_pid
                assert _wait_marker(home, target_id, "target-v2") == (
                    "target-v2",
                    target_uid,
                )
                assert _main_pid(bystander_unit) == bystander_pid
                assert_gateway_and_source_unchanged()

                rolled_back = _gateway_cli(
                    home, "modules", "rollback", target_id, "--activate"
                )
                assert rolled_back.returncode == 0, (
                    rolled_back.stderr or rolled_back.stdout
                )
                assert json.loads(rolled_back.stdout)["rolled_back"] is True
                target_rollback_pid = _wait_main_pid(
                    target_unit, different_from=target_v2_pid
                )
                assert _wait_marker(home, target_id, "target-v1") == (
                    "target-v1",
                    target_uid,
                )
                rollback_path = _read_registry(home)["modules"][target_id]
                assert rollback_path.endswith(target_v1["hash"])
                assert target_rollback_pid != target_v2_pid
                assert _main_pid(bystander_unit) == bystander_pid
                assert_gateway_and_source_unchanged()
                assert telegram.method_counts() == {}
            except BaseException as error:
                test_error = error
                raise
            finally:
                cleanup_errors = _cleanup(
                    unit_files=unit_files,
                    units=units,
                    module_users=runtime_users,
                    created_gateway_user=created_gateway_user,
                    created_group_gid=created_group_gid,
                )
                if cleanup_errors:
                    details = "; ".join(cleanup_errors)
                    if test_error is not None and hasattr(test_error, "add_note"):
                        test_error.add_note(
                            f"test fixture cleanup needs attention: {details}"
                        )
                    else:
                        raise AssertionError(
                            f"test fixture cleanup failed: {details}"
                        )
    finally:
        telegram.close()
