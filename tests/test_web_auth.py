"""Shared web login contracts using only isolated SQLite and synthetic owners."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from pylon.sdk import Outbox, web_auth


NOW = 1_800_000_000.0
WORKSPACES = ("market", "intel", "review", "gmail")
LEGACY_COOKIE = "fixture-legacy-session-" + "a" * 32
LEGACY_TOKEN = "fixture-legacy-token-" + "b" * 32
LEGACY_CSRF = "fixture-legacy-csrf-" + "c" * 32
LEGACY_SCHEMA = """
CREATE TABLE market_web_tokens (
    hash TEXT PRIMARY KEY, profile TEXT NOT NULL,
    expires_at REAL NOT NULL, used_at REAL
);
CREATE TABLE market_web_sessions (
    hash TEXT PRIMARY KEY, profile TEXT NOT NULL,
    csrf TEXT NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE market_web_applies (draft TEXT PRIMARY KEY, version INTEGER NOT NULL);
"""


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_HOME", str(tmp_path))
    monkeypatch.setattr(web_auth, "time", SimpleNamespace(time=lambda: NOW))


@pytest.fixture
def owners(monkeypatch):
    profiles = {
        "default": {"TELEGRAM_HOME_CHANNEL": "fixture-owner-a"},
        "webops": {"TELEGRAM_HOME_CHANNEL": "fixture-owner-a"},
    }
    monkeypatch.setattr(web_auth, "profile_env", lambda profile: profiles[profile])
    return profiles


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    web_auth.initialize(connection)
    yield connection
    connection.close()


def _legacy_session(db, profile="webops", *, expires_at=NOW + 3600):
    db.execute(
        "INSERT INTO market_web_sessions VALUES (?, ?, ?, ?)",
        (_hash(LEGACY_COOKIE), profile, LEGACY_CSRF, expires_at),
    )
    return LEGACY_COOKIE


def test_initialize_preserves_legacy_tables_tokens_and_cookie_contract(owners):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(LEGACY_SCHEMA)
        _legacy_session(connection)
        connection.execute(
            "INSERT INTO market_web_tokens VALUES (?, 'webops', ?, NULL)",
            (_hash(LEGACY_TOKEN), NOW + 300),
        )
        connection.execute("INSERT INTO market_web_applies VALUES ('fixture-draft', 7)")

        web_auth.initialize(connection)
        web_auth.initialize(connection)

        assert web_auth.COOKIE == "pylon_market"
        assert web_auth.COOKIE_PATH == "/pylon/"
        assert {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )} == {"market_web_tokens", "market_web_sessions", "market_web_applies"}
        assert tuple(connection.execute("SELECT * FROM market_web_applies").fetchone()) == (
            "fixture-draft", 7
        )
        for workspace in WORKSPACES:
            row = web_auth.session(connection, LEGACY_COOKIE, workspace)
            assert row["csrf"] == LEGACY_CSRF
            assert row["expires_at"] == NOW + 3600
        cookie, csrf = web_auth.redeem(connection, LEGACY_TOKEN)
        assert web_auth.session(connection, cookie, "gmail")["csrf"] == csrf
    finally:
        connection.close()


def test_login_stores_hashes_and_consumes_a_token_only_once(db, owners):
    token = web_auth.issue_login(db)
    token_row = dict(db.execute("SELECT * FROM market_web_tokens").fetchone())
    assert token_row == {
        "hash": _hash(token), "profile": "webops", "expires_at": NOW + 600, "used_at": None
    }
    assert token not in str(token_row)

    cookie, csrf = web_auth.redeem(db, token)
    assert len({token, cookie, csrf}) == 3
    row = dict(db.execute("SELECT * FROM market_web_sessions").fetchone())
    assert row == {
        "hash": _hash(cookie), "profile": "webops", "csrf": csrf,
        "expires_at": NOW + 7 * 86400,
    }
    assert cookie not in str(row)
    with pytest.raises(ValueError):
        web_auth.redeem(db, token)
    assert db.execute("SELECT COUNT(*) FROM market_web_sessions").fetchone()[0] == 1
    assert db.execute("SELECT used_at FROM market_web_tokens").fetchone()[0] == NOW


@pytest.mark.parametrize("token", [None, 123, [], {}, "", "x" * 31, "x" * 101, "x" * 43])
def test_invalid_and_unknown_tokens_do_not_create_sessions(db, token):
    with pytest.raises(ValueError):
        web_auth.redeem(db, token)
    assert db.execute("SELECT COUNT(*) FROM market_web_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("expires_at", [NOW - 1, NOW])
def test_expired_token_cannot_be_redeemed_at_or_after_expiry(db, owners, expires_at):
    token = web_auth.issue_login(db)
    db.execute("UPDATE market_web_tokens SET expires_at=?", (expires_at,))
    with pytest.raises(ValueError):
        web_auth.redeem(db, token)
    assert db.execute("SELECT used_at FROM market_web_tokens").fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM market_web_sessions").fetchone()[0] == 0


@pytest.mark.parametrize("workspace", WORKSPACES)
@pytest.mark.parametrize("expires_at", [NOW - 1, NOW])
def test_expired_legacy_sessions_are_rejected(db, owners, workspace, expires_at):
    cookie = _legacy_session(db, expires_at=expires_at)
    assert web_auth.session(db, cookie, workspace) is None


@pytest.mark.parametrize("cookie", [None, "", "fixture-unknown-cookie"])
def test_missing_or_unknown_session_cookie_is_rejected(db, cookie):
    assert web_auth.session(db, cookie, "gmail") is None


def test_redeem_rollback_preserves_the_token_for_a_later_transaction(db, owners):
    token = web_auth.issue_login(db)
    db.commit()
    with pytest.raises(RuntimeError, match="fixture rollback"):
        with db:
            web_auth.redeem(db, token)
            raise RuntimeError("fixture rollback")
    assert db.execute("SELECT used_at FROM market_web_tokens").fetchone()[0] is None
    assert db.execute("SELECT COUNT(*) FROM market_web_sessions").fetchone()[0] == 0
    with db:
        cookie, csrf = web_auth.redeem(db, token)
    assert web_auth.session(db, cookie)["csrf"] == csrf


@pytest.mark.parametrize("serialized", [False, True], ids=["competing-readers", "begin-immediate"])
def test_concurrent_redeem_creates_exactly_one_session(tmp_path, owners, monkeypatch, serialized):
    box = Outbox(tmp_path / "concurrent.sqlite3")
    with box.connect() as connection:
        token = web_auth.issue_login(connection)

    start = threading.Barrier(2)
    read = threading.Barrier(2)
    clock_state = threading.local()

    def concurrent_clock():
        # Both contenders must observe the unused token before either update.
        # BEGIN IMMEDIATE callers instead serialize before they read the token.
        if not serialized and not getattr(clock_state, "observed", False):
            clock_state.observed = True
            read.wait(timeout=5)
        return NOW

    monkeypatch.setattr(web_auth, "time", SimpleNamespace(time=concurrent_clock))

    def redeem_once():
        with box.connect() as connection:
            start.wait(timeout=5)
            if serialized:
                connection.execute("BEGIN IMMEDIATE")
            try:
                return "redeemed", web_auth.redeem(connection, token)
            except ValueError:
                return "rejected", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(redeem_once) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sorted(result[0] for result in results) == ["redeemed", "rejected"]
    with box.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_web_sessions").fetchone()[0] == 1
        assert connection.execute("SELECT used_at FROM market_web_tokens").fetchone()[0] == NOW
        cookie, csrf = next(value for status, value in results if status == "redeemed")
        row = connection.execute("SELECT * FROM market_web_sessions").fetchone()
        assert row["hash"] == _hash(cookie)
        assert row["csrf"] == csrf


@pytest.mark.parametrize("profile,workspace", [
    ("default", "gmail"), ("webops", "gmail"),
    ("webops", "market"), ("webops", "intel"), ("webops", "review"),
])
def test_same_owner_login_shares_one_session_across_workspaces(db, owners, profile, workspace):
    token = web_auth.issue_login(db, profile, workspace=workspace)
    cookie, csrf = web_auth.redeem(db, token)
    for target in WORKSPACES:
        row = web_auth.session(db, cookie, target)
        assert row is not None
        assert row["csrf"] == csrf
    assert db.execute("SELECT COUNT(*) FROM market_web_sessions").fetchone()[0] == 1


@pytest.mark.parametrize("default_owner,webops_owner", [
    ("fixture-owner-a", "fixture-owner-b"), ("", ""),
    (None, None), ("fixture-owner-a", None), (None, "fixture-owner-a"),
])
def test_unmatched_owners_cannot_use_webops_for_default_gmail(
    db, owners, default_owner, webops_owner
):
    owners["default"] = {} if default_owner is None else {"TELEGRAM_HOME_CHANNEL": default_owner}
    owners["webops"] = {} if webops_owner is None else {"TELEGRAM_HOME_CHANNEL": webops_owner}
    assert not web_auth.same_owner()
    cookie = _legacy_session(db)
    for workspace in ("market", "intel", "review"):
        assert web_auth.session(db, cookie, workspace)["csrf"] == LEGACY_CSRF
    assert web_auth.session(db, cookie, "gmail") is None
    with pytest.raises(ValueError):
        web_auth.issue_login(db, "webops", workspace="gmail")
    assert db.execute("SELECT COUNT(*) FROM market_web_tokens").fetchone()[0] == 0

    token = web_auth.issue_login(db, "default", workspace="gmail")
    gmail_cookie, _ = web_auth.redeem(db, token)
    assert web_auth.session(db, gmail_cookie, "gmail")["profile"] == "default"
    for workspace in ("market", "intel", "review"):
        assert web_auth.session(db, gmail_cookie, workspace) is None


def test_owner_change_revokes_gmail_access_for_an_existing_webops_session(db, owners):
    cookie = _legacy_session(db)
    assert web_auth.session(db, cookie, "gmail") is not None
    owners["webops"]["TELEGRAM_HOME_CHANNEL"] = "fixture-owner-b"
    assert web_auth.session(db, cookie, "gmail") is None
    assert web_auth.session(db, cookie, "market") is not None


@pytest.mark.parametrize("workspace", ["market", "intel", "review"])
@pytest.mark.parametrize("shared_owner", [True, False])
def test_default_bot_can_issue_only_gmail_links(db, owners, workspace, shared_owner):
    if not shared_owner:
        owners["webops"]["TELEGRAM_HOME_CHANNEL"] = "fixture-owner-b"
    with pytest.raises(ValueError):
        web_auth.issue_login(db, "default", workspace=workspace)
    assert db.execute("SELECT COUNT(*) FROM market_web_tokens").fetchone()[0] == 0


@pytest.mark.parametrize("profile,workspace", [
    ("unknown", "gmail"), ("", "market"), ("webops", "unknown"),
    ("default", "../market"), ("webops", "gmail?admin=1"),
])
def test_unknown_profiles_or_workspaces_cannot_issue_login_links(db, owners, profile, workspace):
    with pytest.raises(ValueError):
        web_auth.issue_login(db, profile, workspace=workspace)
    assert db.execute("SELECT COUNT(*) FROM market_web_tokens").fetchone()[0] == 0


@pytest.mark.parametrize("profile", ["unknown", "", "admin"])
def test_unknown_legacy_session_profile_cannot_access_any_workspace(db, owners, profile):
    cookie = _legacy_session(db, profile)
    assert all(web_auth.session(db, cookie, workspace) is None for workspace in WORKSPACES)


@pytest.mark.parametrize("csrf,origin,valid", [
    (LEGACY_CSRF, "https://example.invalid", True),
    (None, "https://example.invalid", False),
    ("", "https://example.invalid", False),
    ("fixture-wrong-csrf", "https://example.invalid", False),
    (LEGACY_CSRF, None, False),
    (LEGACY_CSRF, "", False),
    (LEGACY_CSRF, "null", False),
    (LEGACY_CSRF, "http://example.invalid", False),
    (LEGACY_CSRF, "https://example.invalid:8443", False),
    (LEGACY_CSRF, "https://other.example.invalid", False),
    (LEGACY_CSRF, "https://example.invalid/", False),
])
def test_write_requires_matching_csrf_and_exact_origin(db, csrf, origin, valid):
    cookie = _legacy_session(db)
    row = web_auth.session(db, cookie)
    assert web_auth.valid_write(row, csrf, origin, "https://example.invalid") is valid


@pytest.mark.parametrize("workspace", WORKSPACES)
@pytest.mark.parametrize("configured,origin", [
    ("https://example.invalid", "https://example.invalid"),
    ("https://example.invalid:8443/legacy/path", "https://example.invalid:8443"),
    ("https://127.0.0.1:8443/pylon/market/", "https://127.0.0.1:8443"),
    ("https://[::1]:8443/legacy/", "https://[::1]:8443"),
])
def test_public_url_uses_only_the_configured_origin_and_a_fixed_workspace_path(
    tmp_path, workspace, configured, origin
):
    config = tmp_path / "config"
    config.mkdir()
    (config / "market-web.json").write_text(json.dumps({"public_url": configured}))
    assert web_auth.public_url(workspace) == f"{origin}/pylon/{workspace}/"


@pytest.mark.parametrize("configured", [
    "", "http://example.invalid", "//example.invalid", "/pylon/market/",
    "https:///pylon/market/", "javascript:alert(1)",
    "https://fixture-user@example.invalid", "https://fixture-user:fixture-value@example.invalid",
    "https://example.invalid/?login=fixture", "https://example.invalid/#fixture",
    "https://example.invalid:not-a-port", "https://example.invalid:70000",
    "https://exa mple.invalid", "https://example.invalid\\malformed",
    "https://example.invalid/\nmalformed", None, 123,
])
def test_public_url_rejects_invalid_configuration(tmp_path, configured):
    config = tmp_path / "config"
    config.mkdir()
    (config / "market-web.json").write_text(json.dumps({"public_url": configured}))
    with pytest.raises(ValueError):
        web_auth.public_url("gmail")


@pytest.mark.parametrize("configuration", [None, [], "fixture", 123, True])
def test_public_url_rejects_non_object_config_files(tmp_path, configuration):
    config = tmp_path / "config"
    config.mkdir()
    (config / "market-web.json").write_text(json.dumps(configuration))
    with pytest.raises(ValueError):
        web_auth.public_url("gmail")


def test_public_url_rejects_missing_and_malformed_config(tmp_path):
    with pytest.raises(ValueError):
        web_auth.public_url("gmail")
    config = tmp_path / "config"
    config.mkdir()
    path = config / "market-web.json"
    path.write_text("{malformed fixture")
    with pytest.raises(ValueError):
        web_auth.public_url("gmail")
    path.write_text("{}")
    with pytest.raises(ValueError):
        web_auth.public_url("gmail")


@pytest.mark.parametrize("workspace", ["", "unknown", "../market", "gmail?admin=1", "gmail/#fixture"])
def test_public_url_rejects_unknown_and_unsafe_workspace_paths(workspace):
    with pytest.raises(ValueError):
        web_auth.public_url(workspace)
