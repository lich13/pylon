"""Black-box contracts for the v0.5.12 gateway reload and diagnostics optimizations."""

from __future__ import annotations

import json
from pathlib import Path
import stat
import tempfile
import time

import pytest

from test_gateway_contract import (
    FakeTelegram,
    Gateway,
    event_row,
    open_context,
    rpc_value,
    run_cli,
    seed_home,
    wait_for,
    write_module,
    write_registry,
)


OPTIMIZATION_KEYS = {
    "reload_full",
    "reload_skipped",
    "completion_scans",
    "delivery_empty_claims",
}


@pytest.fixture
def performance_root():
    with tempfile.TemporaryDirectory(prefix="pylon-performance-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def performance_telegram():
    telegram = FakeTelegram()
    try:
        yield telegram
    finally:
        telegram.close()


def _diagnostics(home: Path) -> dict:
    value = rpc_value(home, "diagnostics")
    assert value["version"] == "0.5.16"
    optimization = value["metrics"].get("optimization")
    assert isinstance(optimization, dict)
    assert OPTIMIZATION_KEYS <= optimization.keys()
    for key in OPTIMIZATION_KEYS:
        assert type(optimization[key]) is int
        assert optimization[key] >= 0
    return value


def _optimization(home: Path) -> dict[str, int]:
    return _diagnostics(home)["metrics"]["optimization"]


def _wait_metric(home: Path, key: str, minimum: int, timeout: float = 14.0) -> dict:
    return wait_for(
        lambda: (
            value
            if (value := _diagnostics(home))["metrics"]["optimization"][key] >= minimum
            else None
        ),
        timeout=timeout,
    )


def _wait_catalog(home: Path, previous: str, timeout: float = 14.0) -> dict:
    return wait_for(
        lambda: (
            value
            if (value := rpc_value(home, "health"))["catalog"] != previous
            else None
        ),
        timeout=timeout,
    )


def _assert_no_credentials(value: object, credentials: tuple[str, ...]) -> None:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True)
    for credential in credentials:
        assert credential not in encoded


def test_reload_optimization_tracks_skips_full_reloads_and_catalog_changes(
    performance_root, performance_telegram
):
    home = performance_root / "home"
    seed_home(
        home,
        performance_telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )

    with Gateway(home, performance_telegram):
        initial = _diagnostics(home)
        initial_catalog = rpc_value(home, "health")["catalog"]
        initial_metrics = initial["metrics"]["optimization"]

        # The supervisor periodically reloads the same files. An unchanged
        # fingerprint must be counted as a skipped reload.
        _wait_metric(
            home,
            "reload_skipped",
            initial_metrics["reload_skipped"] + 1,
        )

        handler = home / "modules/alpha/handler.sh"
        handler.write_text(
            handler.read_text(encoding="utf-8") + "# source fingerprint fixture\n",
            encoding="utf-8",
        )
        source_catalog = _wait_catalog(home, initial_catalog)
        source_metrics = _optimization(home)
        assert source_metrics["reload_full"] > initial_metrics["reload_full"]

        manifest_path = home / "modules/alpha/module.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["capabilities"] = ["fixture.metrics"]
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        manifest_catalog = _wait_catalog(home, source_catalog["catalog"])
        manifest_metrics = _optimization(home)
        assert manifest_metrics["reload_full"] > source_metrics["reload_full"]

        write_module(
            home / "modules/beta",
            "beta",
            "topic_alpha",
            job_id="job_beta",
        )
        write_registry(home / "modules", ["alpha", "beta"])
        registry_catalog = _wait_catalog(home, manifest_catalog["catalog"])
        registry_metrics = _optimization(home)
        assert registry_metrics["reload_full"] > manifest_metrics["reload_full"]
        beta_context = rpc_value(
            home,
            "context.open",
            {"module": "beta", "subject": "job:job_beta"},
        )
        assert beta_context["module_hash"]
        assert beta_context["bot"] == "bot_one"
        assert registry_catalog["catalog"] != initial_catalog

        before_forced = _optimization(home)
        assert rpc_value(home, "admin.reload") == {"ok": True}
        forced = _wait_metric(home, "reload_full", before_forced["reload_full"] + 1)
        assert forced["metrics"]["optimization"]["reload_full"] > before_forced["reload_full"]


def test_invalid_reload_and_credential_changes_keep_snapshot_and_recover(
    performance_root, performance_telegram
):
    home = performance_root / "home"
    tokens = seed_home(
        home,
        performance_telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    original_token = tokens["bot_one"]
    replacement_token = "987:fixture_replacement_token_0001"
    secret_path = home / "gateway/secrets/bot-bot_one"
    manifest_path = home / "modules/alpha/module.json"
    manifest_bytes = manifest_path.read_bytes()

    with Gateway(home, performance_telegram):
        original_catalog = rpc_value(home, "health")["catalog"]
        original_context = rpc_value(
            home,
            "context.open",
            {"module": "alpha", "subject": "job:job_alpha"},
        )

        manifest_path.write_text("{\"api_version\":2,\"id\":\"alpha\"", encoding="utf-8")
        rejected = wait_for(
            lambda: (
                value
                if (value := _diagnostics(home))["configuration_rejected"]
                else None
            ),
            timeout=14.0,
        )
        assert rejected["configuration_rejected"]
        assert rpc_value(home, "health")["catalog"] == original_catalog
        assert rpc_value(
            home,
            "context.open",
            {"module": "alpha", "subject": "job:job_alpha"},
        )["module_hash"] == original_context["module_hash"]

        manifest_path.write_bytes(manifest_bytes)
        wait_for(lambda: _diagnostics(home)["configuration_rejected"] is None, timeout=14.0)
        assert rpc_value(home, "health")["catalog"] == original_catalog

        # A changed credential with unsafe permissions must be rejected while
        # the previously validated snapshot continues serving context requests.
        secret_path.write_text(replacement_token, encoding="utf-8")
        secret_path.chmod(0o644)
        permission_rejected = wait_for(
            lambda: (
                value
                if (value := _diagnostics(home))["configuration_rejected"]
                else None
            ),
            timeout=14.0,
        )
        assert permission_rejected["configuration_rejected"]
        assert rpc_value(home, "health")["catalog"] == original_catalog
        assert rpc_value(
            home,
            "context.open",
            {"module": "alpha", "subject": "job:job_alpha"},
        )["module_hash"] == original_context["module_hash"]

        check = run_cli(home, "check")
        assert check.returncode != 0
        _assert_no_credentials(check.stdout + check.stderr, (original_token, replacement_token))
        _assert_no_credentials(_diagnostics(home), (original_token, replacement_token))

        # Restore the original file and mode. The rejected configuration must
        # clear after the next successful automatic reload.
        secret_path.write_text(original_token, encoding="utf-8")
        secret_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        wait_for(lambda: _diagnostics(home)["configuration_rejected"] is None, timeout=14.0)
        assert rpc_value(home, "health")["catalog"] == original_catalog


def test_delivery_lanes_backoff_spacing_deadlines_and_idle_shutdown(
    performance_root, performance_telegram
):
    home = performance_root / "home"
    seed_home(
        home,
        performance_telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    gateway = Gateway(home, performance_telegram)
    gateway.start()
    try:
        context = open_context(home, "alpha", "job_alpha")["token"]

        # Both delivery lanes should reach their two-second idle cap first.
        # Four seconds of stable idleness should therefore produce about two
        # empty claims per lane, rather than the former 0.5-second polling.
        time.sleep(4.0)
        empty_before = _optimization(home)["delivery_empty_claims"]
        time.sleep(4.0)
        empty_after = _optimization(home)["delivery_empty_claims"]
        empty_delta = empty_after - empty_before
        assert 3 <= empty_delta <= 5, (
            "idle delivery claim cadence changed: "
            f"before={empty_before} after={empty_after} delta={empty_delta}"
        )

        first_id = "optimization-delivery-first"
        first_text = "optimization delivery first"
        rpc_value(
            home,
            "delivery.submit",
            {
                "id": first_id,
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "payload": {"text": first_text},
            },
            context,
        )
        first_submitted = time.monotonic()
        first = wait_for(
            lambda: (
                row
                if (row := event_row(home, first_id))
                and row["status"] == "delivered"
                and row["attempts"] == 1
                and performance_telegram.send_count(first_text + "\n") == 1
                else None
            ),
            timeout=1.0,
        )
        assert first["status"] == "delivered"
        assert time.monotonic() - first_submitted < 1.0
        first_sent = time.monotonic()

        second_id = "optimization-delivery-second"
        second_text = "optimization delivery second"
        rpc_value(
            home,
            "delivery.submit",
            {
                "id": second_id,
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "payload": {"text": second_text},
            },
            context,
        )
        second_submitted = time.monotonic()
        time.sleep(0.7)
        second_early = event_row(home, second_id)
        assert second_early["status"] == "pending", second_early
        assert second_early["attempts"] == 0, second_early
        assert performance_telegram.send_count(second_text + "\n") == 0

        second = wait_for(
            lambda: (
                row
                if (row := event_row(home, second_id))
                and row["status"] == "delivered"
                and row["attempts"] == 1
                and performance_telegram.send_count(second_text + "\n") == 1
                else None
            ),
            timeout=2.0,
        )
        assert second["status"] == "delivered"
        assert time.monotonic() - first_sent >= 1.0
        assert time.monotonic() - second_submitted < 2.0

        # Let the notification lane return to its idle backoff, then verify a
        # near-future event is held until its deadline and delivered promptly.
        time.sleep(4.0)
        due = time.time() + 0.4
        delayed_id = "optimization-delivery-deadline"
        delayed_text = "optimization delivery deadline"
        rpc_value(
            home,
            "delivery.submit",
            {
                "id": delayed_id,
                "kind": "text",
                "job_id": "job_alpha",
                "topic": "topic_alpha",
                "next_attempt": due,
                "payload": {"text": delayed_text},
            },
            context,
        )
        time.sleep(max(0.0, due - time.time() - 0.05))
        delayed_early = event_row(home, delayed_id)
        assert delayed_early["status"] == "pending", delayed_early
        assert delayed_early["attempts"] == 0, delayed_early
        assert performance_telegram.send_count(delayed_text + "\n") == 0

        delayed = wait_for(
            lambda: (
                row
                if (row := event_row(home, delayed_id))
                and row["status"] == "delivered"
                and row["attempts"] == 1
                and performance_telegram.send_count(delayed_text + "\n") == 1
                else None
            ),
            timeout=1.0,
        )
        assert delayed["status"] == "delivered"

        # Confirm SIGTERM interrupts a lane sleeping in its idle backoff.
        time.sleep(2.2)
        started = time.monotonic()
        gateway.stop()
        assert time.monotonic() - started < 3.0
    finally:
        gateway.stop()
