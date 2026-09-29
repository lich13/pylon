#!/srv/pylon/venv/bin/python -I
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

SCRIPT_DIR = Path("/srv/pylon/profiles/webops/scripts")
DATA_ROOT = Path("/srv/pylon/profiles/webops/data/chiphell_fid26")
PROFILE_DIR = DATA_ROOT / "browser-profile"
CANDIDATE_PROFILE_DIR = DATA_ROOT / ".browser-profile.import"
ROLLBACK_PROFILE_DIR = DATA_ROOT / ".browser-profile.rollback"
IMPORT_LOCK_PATH = DATA_ROOT / ".cookie-import.lock"
IMPORT_TRANSACTION_PATH = DATA_ROOT / ".cookie-import-transaction.json"
IMPORT_TRANSACTION_TMP_PATH = DATA_ROOT / ".cookie-import-transaction.json.tmp"
HEALTH_PATH = DATA_ROOT / "proxy-health.json"
QUEUE_PATH = DATA_ROOT / "delivery.sqlite3"
STATE_PATH = DATA_ROOT / "state.json"
POLLER = SCRIPT_DIR / "chiphell_fid26_proxy_poller.py"
SERVICE = "chiphell-fid26-poller.service"
CRON_ID = "63c6ab6c4af3"
HERMES_PYTHON = "/srv/pylon/venv/bin/python"
POLL_RECOVERY_TIMEOUT_SECONDS = 120

sys.path.insert(0, '/usr/local/lib/pylon')
from chiphell_proxy_runtime import (  # noqa: E402
    CHIPHELL_FID,
    CHIPHELL_FORUM_URL,
    CHIPHELL_USER_AGENT,
    DeliveryQueue,
    classify_page,
    ensure_resume_ready,
    observe_exit_code,
    playwright_proxy_options,
    validate_cookie_payload,
)


class CookieImportFailure(RuntimeError):
    def __init__(self, code: str, **fields: Any):
        super().__init__(code)
        self.code = code
        self.fields = fields

    def render(self) -> str:
        values = [self.code]
        for key, value in self.fields.items():
            if value is None:
                continue
            safe = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(value))
            values.append(f"{key}={safe}")
        return " ".join(values)


def run(command: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HERMES_HOME"] = "/srv/pylon/profiles/webops"
    env["PYLON_PROFILE_ROOT"] = "/srv/pylon/profiles/webops"
    env["PLAYWRIGHT_BROWSERS_PATH"] = "/srv/pylon/playwright-browsers"
    env["PYTHONPATH"] = "/srv/pylon/profiles/webops:/srv/pylon/profiles/webops/scripts"
    env["CHIPHELL_PROXY_URL"] = "socks5://127.0.0.1:18080"
    env["CHIPHELL_REQUIRE_PROXY"] = "1"
    if os.geteuid() == 0 and command and command[0] == HERMES_PYTHON:
        command = ["runuser", "-u", "pylon", "--", *command]
    return subprocess.run(command, check=check, text=True, env=env, capture_output=capture)


def service_active() -> bool:
    return run(["systemctl", "is-active", "--quiet", SERVICE], check=False).returncode == 0


def cron(action: str) -> None:
    if action not in {"pause", "resume"}:
        raise ValueError("unsupported scheduler action")
    run(['/usr/bin/systemctl', 'disable' if action == 'pause' else 'enable', '--now', f'pylon-job-{CRON_ID}.timer'])


def command_status() -> int:
    active = service_active()
    health = {}
    if HEALTH_PATH.exists():
        health = json.loads(HEALTH_PATH.read_text())
    pending = DeliveryQueue(QUEUE_PATH).pending_count()
    print(json.dumps({"poller_active": active, "pending": pending, "health": health}, ensure_ascii=False, sort_keys=True))
    run(["docker", "inspect", "frps-chiphell", "--format", "frps={{.State.Status}} restarts={{.RestartCount}}"], check=False)
    return 0 if active else 1


def command_test() -> int:
    run([HERMES_PYTHON, str(POLLER), "--once", "--observe"])
    health = json.loads(HEALTH_PATH.read_text()) if HEALTH_PATH.exists() else {}
    return observe_exit_code(health)


def command_pause() -> int:
    cron("pause")
    run(["systemctl", "stop", SERVICE])
    print("CHIPHELL_PAUSED")
    return 0


def command_baseline() -> int:
    was_active = service_active()
    run(["systemctl", "stop", SERVICE], check=False)
    try:
        run([HERMES_PYTHON, str(POLLER), "--baseline"])
    finally:
        if was_active:
            run(["systemctl", "start", SERVICE])
    return 0


def command_resume() -> int:
    health = json.loads(HEALTH_PATH.read_text()) if HEALTH_PATH.exists() else {}
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    ensure_resume_ready(health, state)
    run(["systemctl", "start", SERVICE])
    cron("resume")
    print("CHIPHELL_RESUMED")
    return 0


@contextlib.contextmanager
def cookie_import_lock() -> Iterator[None]:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(IMPORT_LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(descriptor, 0o600)
    acquired = False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError as error:
            raise CookieImportFailure("REMOTE_VALIDATION_FAILED", stage="LOCK", status="BUSY") from error
        yield
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def recover_interrupted_profile_import(
    profile_dir: Path | None = None,
    candidate_dir: Path | None = None,
    rollback_dir: Path | None = None,
) -> None:
    profile_dir = profile_dir or PROFILE_DIR
    candidate_dir = candidate_dir or CANDIDATE_PROFILE_DIR
    rollback_dir = rollback_dir or ROLLBACK_PROFILE_DIR
    if rollback_dir.exists():
        if profile_dir.exists():
            shutil.rmtree(candidate_dir, ignore_errors=True)
            os.replace(profile_dir, candidate_dir)
        os.replace(rollback_dir, profile_dir)
    shutil.rmtree(candidate_dir, ignore_errors=True)


def install_candidate_profile(
    profile_dir: Path | None = None,
    candidate_dir: Path | None = None,
    rollback_dir: Path | None = None,
) -> None:
    profile_dir = profile_dir or PROFILE_DIR
    candidate_dir = candidate_dir or CANDIDATE_PROFILE_DIR
    rollback_dir = rollback_dir or ROLLBACK_PROFILE_DIR
    if not candidate_dir.is_dir() or rollback_dir.exists():
        raise CookieImportFailure("REMOTE_VALIDATION_FAILED", stage="PROFILE_PREPARE", status="INVALID_TRANSACTION")
    if profile_dir.exists():
        os.replace(profile_dir, rollback_dir)
    try:
        os.replace(candidate_dir, profile_dir)
    except Exception:
        if rollback_dir.exists() and not profile_dir.exists():
            os.replace(rollback_dir, profile_dir)
        raise


def restore_previous_profile(
    profile_dir: Path | None = None,
    candidate_dir: Path | None = None,
    rollback_dir: Path | None = None,
) -> None:
    profile_dir = profile_dir or PROFILE_DIR
    candidate_dir = candidate_dir or CANDIDATE_PROFILE_DIR
    rollback_dir = rollback_dir or ROLLBACK_PROFILE_DIR
    shutil.rmtree(candidate_dir, ignore_errors=True)
    if profile_dir.exists():
        os.replace(profile_dir, candidate_dir)
    if rollback_dir.exists():
        os.replace(rollback_dir, profile_dir)
    shutil.rmtree(candidate_dir, ignore_errors=True)


def commit_profile_import(
    candidate_dir: Path | None = None,
    rollback_dir: Path | None = None,
) -> None:
    candidate_dir = candidate_dir or CANDIDATE_PROFILE_DIR
    rollback_dir = rollback_dir or ROLLBACK_PROFILE_DIR
    shutil.rmtree(candidate_dir, ignore_errors=True)
    shutil.rmtree(rollback_dir, ignore_errors=True)


def write_import_transaction(*, was_active: bool, stage: str, started_at: int) -> None:
    payload = json.dumps({
        "version": 1,
        "poller_was_active": bool(was_active),
        "stage": str(stage),
        "started_at": int(started_at),
    }, sort_keys=True).encode()
    descriptor = os.open(
        IMPORT_TRANSACTION_TMP_PATH,
        os.O_CREAT | os.O_TRUNC | os.O_WRONLY,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    os.replace(IMPORT_TRANSACTION_TMP_PATH, IMPORT_TRANSACTION_PATH)


def clear_import_transaction() -> None:
    IMPORT_TRANSACTION_PATH.unlink(missing_ok=True)
    IMPORT_TRANSACTION_TMP_PATH.unlink(missing_ok=True)


def recover_interrupted_import() -> bool | None:
    transaction: dict[str, Any] | None = None
    if IMPORT_TRANSACTION_PATH.exists():
        try:
            loaded = json.loads(IMPORT_TRANSACTION_PATH.read_text())
            if not isinstance(loaded, dict) or loaded.get("version") != 1:
                raise ValueError("unsupported transaction")
            transaction = loaded
        except (OSError, ValueError) as error:
            was_active = service_active()
            run(["systemctl", "stop", SERVICE], check=False)
            recover_interrupted_profile_import()
            _restore_service(was_active)
            clear_import_transaction()
            raise CookieImportFailure(
                "REMOTE_VALIDATION_FAILED",
                stage="RECOVERY",
                status="INVALID_TRANSACTION",
            ) from error

    has_residue = CANDIDATE_PROFILE_DIR.exists() or ROLLBACK_PROFILE_DIR.exists()
    if transaction is None and not has_residue:
        IMPORT_TRANSACTION_TMP_PATH.unlink(missing_ok=True)
        return None

    current_active = service_active()
    desired_active = bool(transaction.get("poller_was_active")) if transaction else current_active
    run(["systemctl", "stop", SERVICE], check=False)
    if transaction and transaction.get("stage") == "COMMITTING":
        commit_profile_import()
    else:
        recover_interrupted_profile_import()
    clear_import_transaction()
    return desired_active


def _playwright_error_code(error: Exception) -> str | None:
    match = re.search(r"net::([A-Z0-9_]+)", str(error))
    return match.group(1) if match else None


def validate_candidate_profile(cookies: list[dict[str, Any]]) -> dict[str, Any]:
    import chiphell_fid26_monitor as base

    base.playwright_env.ensure()
    from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

    CANDIDATE_PROFILE_DIR.mkdir(mode=0o700, parents=True, exist_ok=False)
    CANDIDATE_PROFILE_DIR.chmod(0o700)
    proxy_env = dict(os.environ)
    proxy_env["CHIPHELL_PROXY_URL"] = "socks5://127.0.0.1:18080"
    proxy_env["CHIPHELL_REQUIRE_PROXY"] = "1"
    options = playwright_proxy_options(proxy_env)
    stage = "BROWSER_LAUNCH"
    context = None
    try:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(CANDIDATE_PROFILE_DIR),
                headless=True,
                viewport={"width": 1400, "height": 1000},
                user_agent=CHIPHELL_USER_AGENT,
                **options,
            )
            stage = "COOKIE_ADD"
            context.add_cookies(cookies)
            page = context.new_page()

            stage = "HOME"
            home_response = page.goto("https://www.chiphell.com/", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            home_status = classify_page(page.locator("body").inner_text(timeout=5000), page.title())

            stage = "FORUM"
            fid_response = page.goto(CHIPHELL_FORUM_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)
            fid_status = classify_page(page.locator("body").inner_text(timeout=5000), page.title())
            rows = page.locator(base.THREAD_ROW_SELECTOR).count()
            context.close()
            context = None
    except (PlaywrightError, PlaywrightTimeoutError) as error:
        raise CookieImportFailure(
            "REMOTE_VALIDATION_FAILED",
            stage=stage,
            error_class=type(error).__name__,
            error_code=_playwright_error_code(error),
        ) from error
    finally:
        if context is not None:
            with contextlib.suppress(Exception):
                context.close()

    fid_key = f"fid{CHIPHELL_FID}"
    if "LOGIN_REQUIRED" in {home_status, fid_status}:
        raise CookieImportFailure(
            "REMOTE_LOGIN_REQUIRED",
            stage="REAL_PAGE",
            home=home_status,
            **{fid_key: fid_status},
            rows=rows,
        )
    if home_status != "OK" or fid_status != "OK" or rows <= 0:
        raise CookieImportFailure(
            "REMOTE_VALIDATION_FAILED",
            stage="REAL_PAGE",
            home=home_status,
            **{fid_key: fid_status},
            rows=rows,
        )
    if os.geteuid() == 0:
        import pwd
        owner = pwd.getpwnam("pylon")
        for parent, dirs, files in os.walk(CANDIDATE_PROFILE_DIR, followlinks=False):
            for item in [Path(parent), *(Path(parent) / name for name in dirs + files)]:
                os.chown(item, owner.pw_uid, owner.pw_gid, follow_symlinks=False)
    return {
        "count": len(cookies),
        "home": home_status,
        "home_http": home_response.status if home_response else None,
        "fid": fid_status,
        "fid_http": fid_response.status if fid_response else None,
        "rows": rows,
    }


def health_signature() -> tuple[int, int, int] | None:
    try:
        stat = HEALTH_PATH.stat()
    except OSError:
        return None
    return stat.st_ino, stat.st_mtime_ns, stat.st_size


def wait_for_fresh_health(
    started_at: int,
    previous_signature: tuple[int, int, int] | None,
    timeout_seconds: int = POLL_RECOVERY_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        current_signature = health_signature()
        if current_signature is not None and current_signature != previous_signature:
            try:
                health = json.loads(HEALTH_PATH.read_text())
            except (OSError, ValueError):
                health = {}
            if int(health.get("last_poll_at") or 0) >= started_at:
                return health
        time.sleep(1)
    return None


def _restore_service(was_active: bool) -> None:
    if was_active and not service_active():
        run(["systemctl", "start", SERVICE], check=False)
    if not was_active and service_active():
        run(["systemctl", "stop", SERVICE], check=False)


def command_import_cookies() -> int:
    try:
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("payload too large")
        cookies = validate_cookie_payload(raw)
    except (TypeError, ValueError) as error:
        print(CookieImportFailure(
            "REMOTE_VALIDATION_FAILED",
            stage="PAYLOAD",
            error_class=type(error).__name__,
        ).render(), file=sys.stderr)
        return 1

    try:
        with cookie_import_lock():
            recovered_active = recover_interrupted_import()
            was_active = service_active() if recovered_active is None else recovered_active
            transaction_started_at = int(time.time())
            write_import_transaction(
                was_active=was_active,
                stage="PREPARE",
                started_at=transaction_started_at,
            )
            run(["systemctl", "stop", SERVICE], check=False)
            swapped = False
            try:
                write_import_transaction(
                    was_active=was_active,
                    stage="VALIDATING",
                    started_at=transaction_started_at,
                )
                validation = validate_candidate_profile(cookies)
                install_candidate_profile()
                swapped = True
                write_import_transaction(
                    was_active=was_active,
                    stage="SWAPPED",
                    started_at=transaction_started_at,
                )

                poller_status = "INACTIVE"
                result_code = "COOKIE_IMPORT_OK_RECOVERY_PENDING"
                if was_active:
                    started_at = int(time.time())
                    previous_signature = health_signature()
                    run(["systemctl", "start", SERVICE])
                    health = wait_for_fresh_health(started_at, previous_signature)
                    poller_status = str((health or {}).get("status") or "TIMEOUT")
                    if poller_status == "OK":
                        result_code = "COOKIE_IMPORT_OK"
                    elif poller_status in {"LOGIN_REQUIRED", "BLOCKED_567", "NO_THREAD_LIST", "BROWSER_LAUNCH_FAILED"}:
                        run(["systemctl", "stop", SERVICE], check=False)
                        restore_previous_profile()
                        swapped = False
                        _restore_service(was_active)
                        raise CookieImportFailure(
                            "REMOTE_LOGIN_REQUIRED" if poller_status == "LOGIN_REQUIRED" else "REMOTE_VALIDATION_FAILED",
                            stage="POST_SWAP",
                            status=poller_status,
                        )

                write_import_transaction(
                    was_active=was_active,
                    stage="COMMITTING",
                    started_at=transaction_started_at,
                )
                commit_profile_import()
                swapped = False
                clear_import_transaction()
                print(" ".join([
                    result_code,
                    f"count={validation['count']}",
                    f"home={validation['home']}",
                    f"home_http={validation['home_http']}",
                    f"fid{CHIPHELL_FID}={validation['fid']}",
                    f"fid{CHIPHELL_FID}_http={validation['fid_http']}",
                    f"rows={validation['rows']}",
                    f"poller={poller_status}",
                ]))
                return 0
            except Exception:
                if swapped:
                    run(["systemctl", "stop", SERVICE], check=False)
                    restore_previous_profile()
                shutil.rmtree(CANDIDATE_PROFILE_DIR, ignore_errors=True)
                _restore_service(was_active)
                clear_import_transaction()
                raise
    except CookieImportFailure as error:
        print(error.render(), file=sys.stderr)
        return 1
    except Exception as error:
        print(CookieImportFailure(
            "REMOTE_VALIDATION_FAILED",
            stage="UNEXPECTED",
            error_class=type(error).__name__,
        ).render(), file=sys.stderr)
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["status", "test", "baseline", "pause", "resume", "import-cookies"])
    return parser


def main(argv: list[str] | None = None) -> int:
    command = build_parser().parse_args(argv).command
    handlers = {
        "status": command_status,
        "test": command_test,
        "baseline": command_baseline,
        "pause": command_pause,
        "resume": command_resume,
        "import-cookies": command_import_cookies,
    }
    return handlers[command]()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"CHIPHELL_CTL_FAILED: {type(error).__name__}", file=sys.stderr)
        raise SystemExit(1)
