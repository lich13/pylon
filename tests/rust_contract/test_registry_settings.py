"""Black-box contracts for registry settings and optional workspace env files."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from test_gateway_contract import (
    FakeTelegram,
    _wait_stderr_line,
    rpc,
    rpc_value,
    running_gateway,
    run_cli,
    seed_home,
    write_module,
    write_registry,
)


@pytest.fixture
def workdir():
    with tempfile.TemporaryDirectory(prefix="pylon-regset-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    yield fake
    fake.close()


@pytest.mark.parametrize(
    ("field", "missing_value", "error_fragment"),
    [
        ("workspace", "missing_workspace", "task workspace is not configured"),
        ("topic", "missing_topic", "task topic is not configured"),
    ],
)
def test_hot_reload_rejects_unconfigured_job_reference_and_keeps_old_catalog(
    workdir, telegram, field, missing_value, error_fragment
):
    home = workdir / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )

    with running_gateway(home, telegram) as gateway:
        catalog_hash = rpc_value(home, "health")["catalog"]
        existing = rpc_value(
            home,
            "context.open",
            {"module": "alpha", "subject": "job:job_alpha"},
        )

        candidate = home / "modules/candidate"
        write_module(
            candidate,
            "candidate",
            "topic_alpha",
            job_id="job_candidate",
        )
        manifest_path = candidate / "module.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["jobs"]["job_candidate"][field] = missing_value
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        write_registry(home / "modules", ["alpha", "candidate"])

        check = run_cli(home, "check")
        assert check.returncode != 0
        assert error_fragment in check.stderr
        rejected_line = _wait_stderr_line(
            gateway.proc, "configuration rejected", timeout=8.0
        )
        assert rejected_line is not None
        assert rpc_value(home, "health")["catalog"] == catalog_hash

        still_active = rpc_value(
            home,
            "context.open",
            {"module": "alpha", "subject": "job:job_alpha"},
        )
        assert still_active["bot"] == existing["bot"] == "bot_one"
        unavailable = rpc(
            home,
            "context.open",
            {"module": "candidate", "subject": "job:job_candidate"},
        )
        assert unavailable["ok"] is False
        assert "unknown module" in unavailable["error"]


def test_fixture_job_runs_when_workspace_env_is_omitted(workdir, telegram):
    home = workdir / "home"
    seed_home(
        home,
        telegram,
        [("bot_one", "101")],
        [{"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"}],
        {"topic_alpha": "bot_one"},
    )
    settings_path = home / "config/bots.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["workspaces"]["default"].pop("env")
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (home / "profiles/default/.env").unlink()

    offline_env = os.environ.copy()
    offline_env["PYLON_TEST_TELEGRAM"] = telegram.url
    offline_env["INVOCATION_ID"] = "fixture-env-omitted"

    with running_gateway(home, telegram):
        result = run_cli(
            home,
            "--offline",
            "execute",
            "job_alpha",
            env=offline_env,
        )

    assert result.returncode == 0, result.stderr or result.stdout
    assert json.loads(result.stdout.strip()) == {"task": "job_alpha", "status": "ok"}
    assert (home / "data/model-runs").read_text(encoding="utf-8") == "started\n"
