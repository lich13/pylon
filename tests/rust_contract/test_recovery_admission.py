"""Recovery admission is enforced where every process actually starts a job."""
from __future__ import annotations

import fcntl
import json
import subprocess
import sqlite3

from test_gateway_contract import BIN, ROOT, wait_for
from test_runtime_reliability import JOB, TOPIC, pytestmark, runtime_case


def test_direct_execute_resume_and_dispatch_share_recovery_limit(runtime_case):
    case = runtime_case
    settings_path = case.home / "config/bots.json"
    settings = json.loads(settings_path.read_text())
    settings["groups"][TOPIC]["processes"] = 8
    settings["admission"] = {"recovery_concurrency": 2}
    settings_path.write_text(json.dumps(settings))
    manifest_path = case.home / "modules/alpha/module.json"
    manifest = json.loads(manifest_path.read_text())
    template = manifest["jobs"][JOB]
    jobs = [f"job_{index}" for index in range(6)]
    manifest["jobs"] = {job: {**template, "lock": job} for job in jobs}
    manifest_path.write_text(json.dumps(manifest))
    case.write_job('while [ ! -e "$PYLON_HOME/data/release-business" ]; do sleep 0.05; done\n')
    case.gateway.start()
    freeze = case.home / "run/freeze"
    freeze.touch()

    def execute(job, invocation, *, wait=False):
        process = subprocess.Popen(
            [str(BIN), "--home", str(case.home), "--offline", "execute", job],
            cwd=ROOT, env=case._env(invocation), stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )
        case.processes.append(process)
        if wait:
            stdout, stderr = process.communicate(timeout=8)
            assert process.returncode == 0, stderr or stdout
        return process

    def requests(state=None):
        with sqlite3.connect(case.home / "supervisor/task-state.sqlite3", timeout=3) as db:
            db.row_factory = sqlite3.Row
            rows = [dict(row) for row in db.execute("SELECT id,job_id,state FROM requests")]
        return [row for row in rows if state is None or row["state"] == state]

    for job in jobs:
        execute(job, job, wait=True)
    assert len(requests("queued")) == len(jobs)
    assert case.count("business-count") == 0
    freeze.unlink()

    # Hold the shared launch gate so child processes exist before any running
    # state can be written. The old dispatcher-only count misses this window.
    with (case.home / "supervisor/admission.lock").open("a") as gate:
        fcntl.flock(gate, fcntl.LOCK_EX)
        direct = execute(jobs[0], jobs[0])
        request_id = next(row["id"] for row in requests() if row["job_id"] == jobs[1])
        resume = subprocess.Popen(
            [str(BIN), "--home", str(case.home), "--offline", "resume", request_id],
            cwd=ROOT, env=case._env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, start_new_session=True,
        )
        case.processes.append(resume)
        case.start_executor()
        assert direct.poll() is None
        assert resume.poll() is None
        assert case.count("business-count") == 0
        fcntl.flock(gate, fcntl.LOCK_UN)

    wait_for(lambda: case.count("business-count") >= 2, timeout=12)
    # All extra direct entrants finish as queued while the first two are gated.
    extras = [execute(job, job) for job in jobs[2:]]
    for process in extras:
        # A process whose request is already running returns without replaying it.
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 0, stderr or stdout
    assert case.count("business-count") == 2
    assert len(requests("running")) == 2
    (case.data / "release-business").touch()
    wait_for(lambda: len(requests("completed")) == 6, timeout=25)
    assert case.count("business-count") == 6

    def recovered():
        summary = json.loads((case.home / "run/task-summary.json").read_text())
        return summary["metrics"].get("recovery_active") is False

    wait_for(recovered, timeout=8)
    (case.data / "release-business").unlink()
    # Once drained, three gradually arriving ordinary jobs can use topic quota 8.
    for index, job in enumerate(jobs[:3], start=1):
        execute(job, "normal-" + job)
        wait_for(lambda: case.count("business-count") >= 6 + index, timeout=8)
    assert len(requests("running")) == 3
    (case.data / "release-business").touch()
    wait_for(lambda: len(requests("completed")) == 9, timeout=12)
