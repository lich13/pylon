from __future__ import annotations

import ipaddress
import json
import os
import random
import re
import socket
import sqlite3
from pylon import events
import subprocess
import time
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


APPROVED_PROXY_URL = "socks5://127.0.0.1:18080"
APPROVED_CURL_PROXY_URL = "socks5h://127.0.0.1:18080"
CHIPHELL_FID = "328"
CHIPHELL_FORUM_URL = (
    "https://www.chiphell.com/forum.php?mod=forumdisplay"
    f"&fid={CHIPHELL_FID}&filter=author&orderby=dateline"
)
CHIPHELL_ALERT_SOURCE = f"Chiphell fid={CHIPHELL_FID} 新帖监控"
CHIPHELL_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
FAULT_ALERT_INTERVAL = 6 * 60 * 60
NAVIGATION_RETRY_DELAYS = (5, 15)
NAVIGATION_RETRY_TIMEOUT_MS = 15_000
PROXY_PROBE_ENDPOINTS = (
    "https://ip.3322.net",
    "https://checkip.amazonaws.com",
    "https://api-ipv4.ip.sb/ip",
)
_NETWORK_ERROR_PATTERN = re.compile(r"\bnet::ERR_[A-Z0-9_]+\b")
_PROXY_ENV_KEYS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}


def playwright_proxy_options(environ: Mapping[str, str]) -> dict[str, dict[str, str]]:
    if environ.get("CHIPHELL_REQUIRE_PROXY") != "1":
        raise RuntimeError("CHIPHELL_REQUIRE_PROXY=1 is required")
    proxy_url = environ.get("CHIPHELL_PROXY_URL", "")
    if not proxy_url:
        raise RuntimeError("CHIPHELL_PROXY_URL is required")
    if proxy_url != APPROVED_PROXY_URL:
        raise RuntimeError("CHIPHELL_PROXY_URL must use the approved endpoint")
    return {"proxy": {"server": proxy_url}}


def classify_page(page_text: str, title: str) -> str:
    combined = f"{title}\n{page_text}"
    if any(marker in combined for marker in ("Access Restricted", "567 Restricted Access", "EO_Bot_Ssid")):
        return "BLOCKED_567"
    if "尚未登录" in combined or "没有权限访问该版块" in combined:
        return "LOGIN_REQUIRED"
    return "OK"


def ensure_resume_ready(health: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    if health.get("status") != "OK":
        raise RuntimeError("health status is not OK")
    notified_ids = state.get("notified_ids")
    if not state.get("baseline_at") or not isinstance(notified_ids, list) or not notified_ids:
        raise RuntimeError("baseline is required before resume")


def ensure_cookie_pages_valid(home_status: str, fid_status: str, fid_rows: int) -> None:
    if home_status != "OK" or fid_status != "OK" or int(fid_rows) <= 0:
        raise RuntimeError(
            f"cookie validation failed home={home_status} fid{CHIPHELL_FID}={fid_status} rows={int(fid_rows)}"
        )


def observe_exit_code(health: Mapping[str, Any]) -> int:
    return 0 if health.get("status") == "OK" else 1


def network_backoff_seconds(attempt: int) -> int:
    values = (300, 900, 3600)
    return values[min(max(int(attempt), 0), len(values) - 1)]


@dataclass(frozen=True)
class NavigationFailure:
    attempt: int
    error_class: str
    error_code: str
    duration_ms: int
    next_retry_seconds: int | None


@dataclass(frozen=True)
class NavigationOutcome:
    ok: bool
    attempts: int
    duration_ms: int
    failures: tuple[NavigationFailure, ...]


def summarize_navigation_error(error: BaseException) -> tuple[str, str]:
    error_class = type(error).__name__
    match = _NETWORK_ERROR_PATTERN.search(str(error))
    if match:
        return error_class, match.group(0)
    if "Timeout" in error_class:
        return error_class, "NAVIGATION_TIMEOUT"
    return error_class, "PLAYWRIGHT_ERROR"


def navigate_with_retries(
    navigate: Callable[[int], None],
    *,
    first_timeout_ms: int,
    retryable_errors: tuple[type[BaseException], ...],
    retry_delays: Sequence[int] = NAVIGATION_RETRY_DELAYS,
    retry_timeout_ms: int = NAVIGATION_RETRY_TIMEOUT_MS,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_failure: Callable[[NavigationFailure], None] | None = None,
) -> NavigationOutcome:
    if not retryable_errors:
        raise ValueError("retryable_errors must not be empty")
    delays = tuple(max(0, int(value)) for value in retry_delays)
    timeouts = (max(1, int(first_timeout_ms)),) + (max(1, int(retry_timeout_ms)),) * len(delays)
    failures: list[NavigationFailure] = []
    started = monotonic()

    for index, timeout_ms in enumerate(timeouts):
        attempt_started = monotonic()
        try:
            navigate(timeout_ms)
        except retryable_errors as error:
            error_class, error_code = summarize_navigation_error(error)
            next_retry = delays[index] if index < len(delays) else None
            failure = NavigationFailure(
                attempt=index + 1,
                error_class=error_class,
                error_code=error_code,
                duration_ms=max(0, int((monotonic() - attempt_started) * 1000)),
                next_retry_seconds=next_retry,
            )
            failures.append(failure)
            if on_failure:
                on_failure(failure)
            if next_retry is not None:
                sleep(next_retry)
            continue
        return NavigationOutcome(
            ok=True,
            attempts=index + 1,
            duration_ms=max(0, int((monotonic() - started) * 1000)),
            failures=tuple(failures),
        )

    return NavigationOutcome(
        ok=False,
        attempts=len(timeouts),
        duration_ms=max(0, int((monotonic() - started) * 1000)),
        failures=tuple(failures),
    )


@dataclass(frozen=True)
class ProxyProbeResult:
    status: str
    error_code: str | None
    latency_ms: int
    endpoint: str | None = None


def classify_navigation_failure(probe: ProxyProbeResult) -> str:
    return "UPSTREAM_UNAVAILABLE" if probe.status == "OK" else "PROXY_UNAVAILABLE"


def build_proxy_probe_command(endpoint: str) -> list[str]:
    return [
        "curl",
        "-4",
        "--fail",
        "--silent",
        "--show-error",
        "--proxy",
        APPROVED_CURL_PROXY_URL,
        "--connect-timeout",
        "3",
        "--max-time",
        "6",
        str(endpoint),
    ]


def probe_proxy_path(
    *,
    connect: Callable[..., Any] = socket.create_connection,
    run: Callable[..., Any] = subprocess.run,
    environ: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ProxyProbeResult:
    started = monotonic()
    try:
        connection = connect(("127.0.0.1", 18080), timeout=1)
    except OSError:
        return ProxyProbeResult(
            status="LISTENER_DOWN",
            error_code="SOCKS_LISTENER_DOWN",
            latency_ms=max(0, int((monotonic() - started) * 1000)),
        )
    try:
        close = getattr(connection, "close", None)
        if close:
            close()
    except OSError:
        pass

    clean_env = dict(os.environ if environ is None else environ)
    for key in _PROXY_ENV_KEYS:
        clean_env.pop(key, None)

    for endpoint in PROXY_PROBE_ENDPOINTS:
        try:
            result = run(
                build_proxy_probe_command(endpoint),
                check=False,
                capture_output=True,
                text=True,
                timeout=7,
                env=clean_env,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if int(result.returncode) != 0:
            continue
        candidate = str(result.stdout).strip()
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version == 4:
            return ProxyProbeResult(
                status="OK",
                error_code=None,
                latency_ms=max(0, int((monotonic() - started) * 1000)),
                endpoint=endpoint,
            )

    return ProxyProbeResult(
        status="EGRESS_FAILED",
        error_code="SOCKS_EGRESS_FAILED",
        latency_ms=max(0, int((monotonic() - started) * 1000)),
    )


@dataclass(frozen=True)
class FaultEvent:
    alert: bool = False
    recovered: bool = False
    previous: str | None = None
    consecutive_count: int = 0
    alert_sent: bool = False
    changed: bool = False


def transition_fault(
    state: dict[str, Any],
    kind: str,
    *,
    now: int,
    alert_threshold: int = 1,
) -> tuple[dict[str, Any], FaultEvent]:
    threshold = max(1, int(alert_threshold))
    updated = deepcopy(state)
    current = updated.get("fault")
    if kind == "OK":
        if current:
            previous = str(current.get("kind") or "UNKNOWN")
            alert_sent = bool(current.get("alert_sent") or current.get("last_alert_at"))
            updated["fault"] = None
            return updated, FaultEvent(
                recovered=alert_sent,
                previous=previous,
                alert_sent=alert_sent,
                changed=True,
            )
        updated["fault"] = None
        return updated, FaultEvent()

    if not current or current.get("kind") != kind:
        alert = threshold <= 1
        updated["fault"] = {
            "kind": kind,
            "first_seen_at": int(now),
            "last_seen_at": int(now),
            "last_alert_at": int(now) if alert else 0,
            "consecutive_count": 1,
            "alert_sent": alert,
        }
        return updated, FaultEvent(
            alert=alert,
            consecutive_count=1,
            alert_sent=alert,
            changed=True,
        )

    current["last_seen_at"] = int(now)
    consecutive_count = max(1, int(current.get("consecutive_count") or 1)) + 1
    current["consecutive_count"] = consecutive_count
    last_alert = int(current.get("last_alert_at") or 0)
    alert_sent = bool(current.get("alert_sent") or last_alert)
    alert = False
    if not alert_sent and consecutive_count >= threshold:
        alert = True
        alert_sent = True
    elif alert_sent and int(now) - last_alert >= FAULT_ALERT_INTERVAL:
        alert = True
    if alert:
        current["last_alert_at"] = int(now)
    current["alert_sent"] = alert_sent
    return updated, FaultEvent(
        alert=alert,
        consecutive_count=consecutive_count,
        alert_sent=alert_sent,
    )


def choose_interval(randint: Callable[[int, int], int] = random.randint) -> int:
    return int(randint(60, 120))


class DeliveryQueue:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS delivery_queue (
                        dedupe_key TEXT PRIMARY KEY,
                        payload TEXT NOT NULL,
                        created_at INTEGER NOT NULL,
                        claimed_at INTEGER
                    )
                    """
                )
        os.chmod(self.path, 0o600)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30)

    def enqueue(self, dedupe_key: str, payload: str, *, now: int) -> bool:
        with closing(self._connect()) as connection:
            with connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO delivery_queue(dedupe_key, payload, created_at) VALUES (?, ?, ?)",
                    (str(dedupe_key), str(payload), int(now)),
                )
                return cursor.rowcount == 1

    def pending_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM delivery_queue WHERE claimed_at IS NULL"
            ).fetchone()
            return int(row[0])

    def claim(self, *, limit: int, now: int) -> list[str]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT dedupe_key, payload FROM delivery_queue WHERE claimed_at IS NULL ORDER BY created_at, dedupe_key LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
            if rows:
                for dedupe_key, payload in rows:
                    events.publish(str(payload), key="chiphell:" + str(dedupe_key))
                connection.executemany(
                    "UPDATE delivery_queue SET claimed_at = ? WHERE dedupe_key = ? AND claimed_at IS NULL",
                    [(int(now), row[0]) for row in rows],
                )
            connection.commit()
            return [str(row[1]) for row in rows]
        finally:
            connection.close()


def apply_baseline(state: dict[str, Any], visible_ids: list[str], *, max_ids: int = 500) -> dict[str, Any]:
    updated = deepcopy(state)
    ordered: list[str] = []
    for value in [*updated.get("notified_ids", []), *visible_ids]:
        item = str(value)
        if item and item not in ordered:
            ordered.append(item)
    updated["notified_ids"] = ordered[-max(1, int(max_ids)) :]
    return updated


def _is_chiphell_domain(domain: str) -> bool:
    normalized = str(domain).lstrip(".").lower()
    return normalized == "chiphell.com" or normalized.endswith(".chiphell.com")


def validate_cookie_payload(raw: str | bytes) -> list[dict[str, Any]]:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise ValueError("cookie payload must be valid JSON") from error
    cookies = payload.get("cookies") if isinstance(payload, dict) else None
    if not isinstance(cookies, list) or not 1 <= len(cookies) <= 256:
        raise ValueError("cookie payload must contain 1 to 256 cookies")
    validated: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or not _is_chiphell_domain(cookie.get("domain", "")):
            raise ValueError("cookie domain must be chiphell.com")
        if not isinstance(cookie.get("name"), str) or not cookie["name"]:
            raise ValueError("cookie name is required")
        if not isinstance(cookie.get("value"), str):
            raise ValueError("cookie value must be a string")
        if cookie.get("sameSite") not in {"Lax", "Strict", "None"}:
            raise ValueError("cookie sameSite is invalid")
        validated.append(dict(cookie))
    return validated
