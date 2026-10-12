"""Root-owned validation and delivery schema used by restricted control."""
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

CHIPHELL_FID = "328"

CHIPHELL_FORUM_URL = (
    "https://www.chiphell.com/forum.php?mod=forumdisplay"
    f"&fid={CHIPHELL_FID}&filter=author&orderby=dateline"
)

CHIPHELL_USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"

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

def observe_exit_code(health: Mapping[str, Any]) -> int:
    return 0 if health.get("status") == "OK" else 1

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
