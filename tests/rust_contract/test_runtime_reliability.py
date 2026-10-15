"""Black-box contracts for durable task admission and completion reporting."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time

import pytest

from test_gateway_contract import (
    BIN,
    ROOT,
    FakeTelegram,
    Gateway,
    rpc,
    rpc_value,
    seed_home,
    wait_for,
)


JOB = "job_alpha"
TOPIC = "topic_alpha"
pytestmark = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="offline runtime fixtures execute business processes as the current non-root user",
)


def _process_is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat="],
        capture_output=True,
        text=True,
        timeout=2,
    )
    return result.returncode == 0 and not result.stdout.strip().startswith("Z")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    process.communicate()


class RuntimeCase:
    def __init__(self, home: Path, telegram: FakeTelegram):
        self.home = home
        self.telegram = telegram
        self.processes: list[subprocess.Popen] = []
        self.executors: list[subprocess.Popen] = []
        seed_home(
            home,
            telegram,
            [("bot_one", "101")],
            [{"id": "alpha", "topic": TOPIC, "job_id": JOB}],
            {TOPIC: "bot_one"},
        )
        self.data = home / "data"
        self.data.mkdir()
        settings_path = home / "config/bots.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        settings["groups"] = {TOPIC: {"processes": 1, "models": 1}}
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        self.gateway = Gateway(home, telegram)
        self.write_job("exit 0\n")
        self.write_handler()

    def configure_job(self, **changes) -> None:
        path = self.home / "modules/alpha/module.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["jobs"][JOB].update(changes)
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def write_job(self, body: str) -> None:
        (self.home / "modules/alpha/job.sh").write_text(
            '#!/bin/bash\nset -eu\n'
            'printf "%s\\n" "$$" >> "$PYLON_HOME/data/owned-groups"\n'
            'printf "started\\n" >> "$PYLON_HOME/data/business-count"\n'
            + body,
            encoding="utf-8",
        )

    def write_handler(self, status: str = "ok", *, gated: bool = False) -> None:
        gate = (
            'while [ ! -e "$PYLON_HOME/data/release-completion" ]; do sleep 0.05; done\n'
            if gated
            else ""
        )
        envelope = json.dumps({"api_version": 2, "value": {"status": status}})
        (self.home / "modules/alpha/handler.sh").write_text(
            '#!/bin/bash\nset -eu\n'
            'printf "%s\\n" "$$" >> "$PYLON_HOME/data/owned-groups"\n'
            'cat > "$PYLON_HOME/data/completion-input.json"\n'
            'printf "completed\\n" >> "$PYLON_HOME/data/completion-count"\n'
            + gate
            + f"printf '%s\\n' '{envelope}'\n",
            encoding="utf-8",
        )

    def count(self, name: str) -> int:
        path = self.data / name
        return len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0

    def _env(self, invocation: str | None = None) -> dict[str, str]:
        env = {
            key: os.environ[key]
            for key in ("PATH", "LANG", "LC_ALL", "LC_CTYPE")
            if key in os.environ
        }
        env["PYLON_TEST_TELEGRAM"] = self.telegram.url
        if invocation is not None:
            env["INVOCATION_ID"] = invocation
        return env

    def start_execute(self, invocation: str) -> subprocess.Popen:
        process = subprocess.Popen(
            [str(BIN), "--home", str(self.home), "--offline", "execute", JOB],
            cwd=ROOT,
            env=self._env(invocation),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.processes.append(process)
        return process

    def execute(self, invocation: str, timeout: float = 8) -> subprocess.CompletedProcess:
        process = self.start_execute(invocation)
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def start_executor(self) -> subprocess.Popen:
        process = subprocess.Popen(
            [str(BIN), "--home", str(self.home), "--offline", "executor"],
            cwd=ROOT,
            env=self._env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        self.executors.append(process)

        def ready():
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                raise AssertionError(f"executor exited at startup: {stderr or stdout}")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.2)
                connection.connect(str(self.home / "run/executor.sock"))
            return True

        wait_for(ready, timeout=8)
        return process

    def requests(self) -> list[dict]:
        path = self.home / "supervisor/task-state.sqlite3"
        if not path.is_file():
            return []
        with sqlite3.connect(path, timeout=3) as database:
            database.row_factory = sqlite3.Row
            rows = database.execute(
                "SELECT id,job_id,state,execution_status,completion_status,detail,report "
                "FROM requests WHERE job_id=? ORDER BY rowid",
                (JOB,),
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["report"] = json.loads(row["report"]) if row["report"] else None
        return result

    def requests_in(self, state: str) -> list[dict]:
        return [row for row in self.requests() if row["state"] == state]

    def runs(self) -> list[dict]:
        with sqlite3.connect(self.home / "gateway/state.sqlite3", timeout=3) as database:
            database.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in database.execute(
                    "SELECT id,status,detail,finished_at FROM runs WHERE job_id=? ORDER BY rowid",
                    (JOB,),
                )
            ]

    def kill_owned_groups(self) -> None:
        path = self.data / "owned-groups"
        if not path.is_file():
            return
        for item in path.read_text(encoding="utf-8").splitlines():
            try:
                os.killpg(int(item), signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self) -> None:
        for process in reversed(self.executors):
            _stop(process)
        for name in ("release-business", "release-completion"):
            (self.data / name).touch(exist_ok=True)
        for process in reversed(self.processes):
            _stop(process)
        self.kill_owned_groups()
        self.gateway.stop()


@pytest.fixture
def runtime_case():
    with tempfile.TemporaryDirectory(prefix="pylon-runtime-", dir="/tmp") as directory:
        telegram = FakeTelegram()
        case = RuntimeCase(Path(directory) / "home", telegram)
        try:
            yield case
        finally:
            try:
                case.close()
            finally:
                telegram.close()


@contextmanager
def _held_quota(case: RuntimeCase, *, model: bool):
    kind = "model" if model else "process"
    path = case.home / "run" / f"quota-{TOPIC}-{kind}-0.lock"
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def test_completion_failure_overrides_successful_business_exit(runtime_case):
    case = runtime_case
    case.write_handler("failed")
    case.gateway.start()

    result = case.execute("completion-failed")

    assert result.returncode == 1, result.stderr or result.stdout
    assert json.loads(result.stdout) == {"task": JOB, "status": "failed"}
    assert [row["status"] for row in case.runs()] == ["failed"]
    assert case.count("business-count") == case.count("completion-count") == 1
    request = json.loads((case.data / "completion-input.json").read_text(encoding="utf-8"))
    assert request["action"] == "runtime.complete"
    assert request["payload"]["status"] == "ok"

    recorded_runs = case.runs()
    result_key = f"run-result:{recorded_runs[0]['id']}"
    with sqlite3.connect(case.home / "gateway/state.sqlite3", timeout=3) as database:
        recorded_result = database.execute(
            "SELECT value FROM kv WHERE key=?", (result_key,)
        ).fetchone()[0]
    context = rpc_value(
        case.home, "context.open", {"module": "alpha", "subject": f"job:{JOB}"}
    )["token"]
    try:
        replacement = {
            "id": recorded_runs[0]["id"],
            "status": "ok",
            "execution_status": "failed",
            "completion_status": "failed",
            "notification_status": "sent",
            "detail": "duplicate fixture must not replace the recorded result",
        }
        rejected = rpc(case.home, "run.finish", replacement, context)
        assert rejected["ok"] is False
        replacement["status"] = "failed"
        rpc_value(case.home, "run.finish", replacement, context)
        assert case.runs() == recorded_runs
        with sqlite3.connect(case.home / "gateway/state.sqlite3", timeout=3) as database:
            assert database.execute(
                "SELECT value FROM kv WHERE key=?", (result_key,)
            ).fetchone()[0] == recorded_result
    finally:
        rpc_value(case.home, "context.close", context=context)


def test_timeout_covers_descendant_holding_output_after_shell_exit(runtime_case):
    case = runtime_case
    case.configure_job(timeout=1)
    case.write_job(
        'sleep 30 &\n'
        'printf "%s\\n" "$!" > "$PYLON_HOME/data/descendant-pid"\n'
        'exit 0\n'
    )
    case.gateway.start()

    started = time.monotonic()
    result = case.execute("descendant-timeout", timeout=7)
    elapsed = time.monotonic() - started

    assert result.returncode == 1, result.stderr or result.stdout
    assert json.loads(result.stdout) == {"task": JOB, "status": "uncertain"}
    assert elapsed < 7
    assert [row["status"] for row in case.runs()] == ["uncertain"]
    descendant = int((case.data / "descendant-pid").read_text(encoding="utf-8"))
    wait_for(lambda: not _process_is_live(descendant), timeout=2)
    assert case.count("business-count") == 1


@pytest.mark.parametrize("model", [False, True], ids=["process-quota", "model-quota"])
def test_quota_admission_survives_executor_restart_and_runs_after_release(runtime_case, model):
    case = runtime_case
    case.configure_job(model=model)
    case.gateway.start()

    with _held_quota(case, model=model):
        result = case.execute("quota-pending")
        assert result.returncode == 0, result.stderr or result.stdout
        assert json.loads(result.stdout)["status"] == "queued"
        queued = case.requests_in("queued")
        assert len(queued) == 1
        assert case.count("business-count") == 0

        executor = case.start_executor()
        _stop(executor)
        case.start_executor()
        assert case.requests_in("queued") == queued
        assert case.count("business-count") == 0

    wait_for(lambda: case.requests_in("completed"), timeout=10)
    assert [row["status"] for row in case.runs()] == ["ok"]
    assert case.count("business-count") == case.count("completion-count") == 1


def test_periodic_triggers_merge_into_one_queued_run(runtime_case):
    case = runtime_case
    case.write_job(
        'while [ ! -e "$PYLON_HOME/data/release-business" ]; do sleep 0.05; done\n'
    )
    case.gateway.start()
    case.start_executor()
    first = case.start_execute("periodic-first")
    wait_for(lambda: case.count("business-count") == 1)

    for invocation in ("periodic-first", "periodic-second", "periodic-third"):
        result = case.execute(invocation)
        assert result.returncode == 0, result.stderr or result.stdout

    assert len(case.requests_in("running")) == 1
    assert len(case.requests_in("queued")) == 1
    assert case.count("business-count") == 1
    (case.data / "release-business").touch()
    stdout, stderr = first.communicate(timeout=8)
    assert first.returncode == 0, stderr or stdout
    wait_for(lambda: len(case.requests_in("completed")) == 2, timeout=10)

    assert case.count("business-count") == case.count("completion-count") == 2
    assert not case.requests_in("queued")
    assert [row["status"] for row in case.runs()] == ["ok", "ok"]
    repeated = case.execute("periodic-first")
    assert repeated.returncode == 0, repeated.stderr or repeated.stdout
    assert case.count("business-count") == 2


def test_interrupted_started_request_becomes_uncertain_without_business_replay(runtime_case):
    case = runtime_case
    case.write_job(
        'while [ ! -e "$PYLON_HOME/data/release-business" ]; do sleep 0.05; done\n'
    )
    case.gateway.start()
    first = case.start_execute("interrupted-started")
    wait_for(lambda: case.count("business-count") == 1)
    assert len(case.requests_in("running")) == 1

    first.kill()
    first.communicate(timeout=3)
    case.kill_owned_groups()
    case.gateway.stop()
    case.start_executor()

    pending = wait_for(lambda: case.requests_in("reporting"), timeout=10)
    assert pending[0]["execution_status"] == "uncertain"
    assert pending[0]["report"]["status"] == "uncertain"
    repeated = case.execute("interrupted-started")
    assert repeated.returncode in (0, 1), repeated.stderr or repeated.stdout
    assert len(case.requests()) == 1
    assert case.count("business-count") == 1
    assert case.count("completion-count") == 0

    case.gateway.start()
    wait_for(lambda: case.requests_in("completed"), timeout=10)
    assert [row["status"] for row in case.runs()] == ["uncertain"]
    assert case.requests()[0]["execution_status"] == "uncertain"
    assert case.count("business-count") == 1


def test_legacy_invocation_marker_is_retained_without_business_replay(runtime_case):
    case = runtime_case
    case.gateway.start()
    invocation = "legacy-started"
    marker = (
        case.home / "run/invocations"
        / hashlib.sha256(f"{JOB}:{invocation}".encode()).hexdigest()
    )
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"legacy invocation fixture\n")

    result = case.execute(invocation)
    assert result.returncode == 0, result.stderr or result.stdout
    assert len(case.requests_in("uncertain")) == 1
    case.start_executor()
    repeated = case.execute(invocation)
    assert repeated.returncode == 0, repeated.stderr or repeated.stdout
    assert len(case.requests()) == 1
    assert case.requests()[0]["state"] == "uncertain"
    assert not case.runs()
    assert case.count("business-count") == case.count("completion-count") == 0
    assert marker.read_bytes() == b"legacy invocation fixture\n"


def test_completion_report_retries_after_disconnect_without_rerunning_work(runtime_case):
    case = runtime_case
    case.write_handler("failed", gated=True)
    case.gateway.start()
    execution = case.start_execute("report-disconnected")
    wait_for(lambda: case.count("completion-count") == 1)

    case.gateway.stop()
    (case.data / "release-completion").touch()
    execution.communicate(timeout=8)
    assert execution.returncode in (0, 1)
    pending = case.requests_in("reporting")
    assert len(pending) == 1
    assert pending[0]["execution_status"] == "ok"
    assert pending[0]["completion_status"] == "ok"
    assert pending[0]["report"]["status"] == "failed"

    case.gateway.start()
    case.start_executor()
    wait_for(lambda: case.requests_in("completed"), timeout=10)
    assert [row["status"] for row in case.runs()] == ["failed"]
    assert case.count("business-count") == case.count("completion-count") == 1

    repeated = case.execute("report-disconnected")
    assert repeated.returncode in (0, 1), repeated.stderr or repeated.stdout
    assert len(case.requests()) == 1
    assert case.count("business-count") == case.count("completion-count") == 1


def test_supervisor_directory_symlink_is_rejected_without_creating_database(runtime_case):
    case = runtime_case
    target = case.home.parent / "supervisor-target"
    target.mkdir()
    (case.home / "supervisor").symlink_to(target, target_is_directory=True)

    result = case.execute("supervisor-symlink")

    assert result.returncode == 1
    assert "unsafe supervisor state directory" in result.stderr
    assert list(target.iterdir()) == []
    assert not (target / "task-state.sqlite3").exists()


def test_queue_database_symlink_is_rejected_without_changing_target(runtime_case):
    case = runtime_case
    supervisor = case.home / "supervisor"
    supervisor.mkdir(mode=0o700)
    target = case.home.parent / "database-target"
    original = b"fixture database target must remain unchanged\n"
    target.write_bytes(original)
    (supervisor / "task-state.sqlite3").symlink_to(target)

    result = case.execute("database-symlink")

    assert result.returncode == 1
    assert "unsafe supervisor state file" in result.stderr
    assert (supervisor / "task-state.sqlite3").is_symlink()
    assert target.read_bytes() == original


def test_result_file_symlink_is_not_read_or_exposed_to_completion_handler(runtime_case):
    case = runtime_case
    case.gateway.start()
    target = case.data / "result-fixture.json"
    original = b'{"status":"ok","fixture_secret":"must not be exposed"}\n'
    target.write_bytes(original)
    case.write_job('ln -s "$PYLON_HOME/data/result-fixture.json" "$PYLON_RESULT_FILE"\n')

    result = case.execute("result-symlink")

    assert result.returncode == 1, result.stderr or result.stdout
    assert json.loads(result.stdout) == {"task": JOB, "status": "uncertain"}
    completion = json.loads((case.data / "completion-input.json").read_text(encoding="utf-8"))
    assert completion["payload"]["result"]["status"] == "uncertain"
    assert "fixture_secret" not in completion["payload"]["result"]
    assert target.read_bytes() == original
