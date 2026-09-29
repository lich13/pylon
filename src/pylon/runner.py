from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

from .config import home, jobs, profile_env, profile_root, redact
from .events import silent, text_key
from .outbox import Outbox


def task_environment(profile: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update(profile_env(profile))
    root = profile_root(profile)
    env.update(PYLON_PROFILE=profile, PYLON_PROFILE_ROOT=str(root), HERMES_HOME=str(root),
               PYLON_HOME=str(home()), HOME=str(home() / "home"),
               PYTHONPATH=os.pathsep.join([str(home() / "src"), str(root), str(root / "scripts")]),
               PLAYWRIGHT_BROWSERS_PATH=str(home() / "playwright-browsers"),
               PYTHONUNBUFFERED="1", TZ="Asia/Shanghai")
    env["PATH"] = str(home() / "venv/bin") + ":/usr/local/bin:/usr/bin:/bin"
    return env


def run(job_id: str) -> int:
    job = jobs()[job_id]
    box = Outbox()
    lockdir = home() / "run"
    lockdir.mkdir(parents=True, exist_ok=True)
    lock = (lockdir / (job.get("lock", job_id) + ".lock")).open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("已有任务运行，跳过本次触发")
        lock.close()
        return 0
    run_id = os.environ.get("DAG_RUN_ID") or os.environ.get("DAGU_RUN_ID") or uuid.uuid4().hex
    run_id = f"{job_id}:{run_id}"
    with box.connect() as db:
        previous = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if previous:
            print("本次运行已记录；业务动作不会重复执行。状态：" + previous[0])
            lock.close()
            return 0 if previous[0] in {"ok", "no_change"} else 1
        db.execute("INSERT INTO runs(id,job_id,started_at,status) VALUES(?,?,?,'running')", (run_id, job_id, time.time()))
    result_file = lockdir / (uuid.uuid4().hex + ".json")
    env = task_environment(job["profile"])
    env.update(PYLON_JOB_ID=job_id, PYLON_RUN_ID=run_id, PYLON_OUTBOX=str(box.path), PYLON_RESULT_FILE=str(result_file))
    script = profile_root(job["profile"]) / "scripts" / job["script"]
    interpreter = "/bin/bash" if script.suffix == ".sh" else str(home() / "venv/bin/python")
    started = time.time()
    process = None
    old_handlers = {}

    def interrupted(signum, _frame):
        raise RuntimeError(f"任务被信号 {signum} 中断")

    for signum in (signal.SIGTERM, signal.SIGINT):
        old_handlers[signum] = signal.signal(signum, interrupted)
    stdout, stderr, status, detail = "", "", "ok", ""
    try:
        process = subprocess.Popen([interpreter, str(script)], cwd=profile_root(job["profile"]), env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=job["timeout"])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                stdout, stderr = process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                stdout, stderr = process.communicate()
            raise RuntimeError("任务执行超时")
        result = json.loads(result_file.read_text()) if result_file.exists() else {}
        if process.returncode != 0 or result.get("status") == "failed":
            status = "failed"
            detail = result.get("detail") or redact(stderr[-1200:] or stdout[-1200:] or f"exit {process.returncode}")
        if not silent(stdout) and job.get("deliver") and not box.run_events(run_id, "text"):
            box.enqueue(profile=job["profile"], job_id=job_id, kind="text", payload={"text": stdout.strip()}, key=text_key(stdout.strip(), job_id), run_id=run_id)
        if status == "failed" and job.get("failure_notify", True) and not box.run_events(run_id, "text"):
            box.enqueue(profile=job["profile"], job_id=job_id, kind="text", payload={"text": f"{job['name']}失败\n{redact(detail)}"}, key=f"failure:{job_id}:{int(time.time()) // 21600}:{redact(detail)[:160]}", run_id=run_id)
        events = box.run_events(run_id)
        if not events and status == "ok":
            status = "no_change"
        elif events:
            deadline = time.time() + 55
            while time.time() < deadline:
                events = box.run_events(run_id)
                if all(row["status"] == "delivered" for row in events):
                    break
                if any(row["status"] in {"uncertain", "failed"} for row in events):
                    break
                time.sleep(1)
            if status == "ok" and any(row["status"] != "delivered" for row in events):
                status, detail = "delivery_pending", "通知已保存，等待补发或核对回执"
    except Exception as exc:
        status, detail = "failed", redact(f"{type(exc).__name__}: {exc}")
        if job.get("failure_notify", True):
            box.enqueue(profile=job["profile"], job_id=job_id, kind="text", payload={"text": f"{job['name']}失败\n{detail}"},
                        key=f"failure:{job_id}:{int(time.time()) // 21600}:{detail[:160]}", run_id=run_id)
    finally:
        for signum, handler in old_handlers.items():
            signal.signal(signum, handler)
        if process is not None and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        with box.connect() as db:
            incomplete = db.execute("SELECT id FROM operations WHERE id=? AND status='started'", (run_id + ':gmail-delete',)).fetchone()
        if incomplete:
            box.enqueue(profile='default', job_id=job_id, kind='text',
                        payload={'text':'Gmail 清理中断，删除结果待核对；通知重试不会再次删除邮件。'}, key='operation:' + incomplete[0], run_id=run_id)
        result_file.unlink(missing_ok=True)
        with box.connect() as db:
            db.execute("UPDATE runs SET finished_at=?,status=?,detail=? WHERE id=?", (time.time(), status, detail, run_id))
        lock.close()
    print(json.dumps({"task": job_id, "status": status, "duration_seconds": round(time.time() - started, 1), "detail": detail, "notifications": {s: sum(r['status'] == s for r in box.run_events(run_id)) for s in ['delivered', 'pending', 'sending', 'uncertain', 'failed']}}, ensure_ascii=False))
    if stderr.strip():
        print(redact(stderr[-3000:]))
    return 0 if status in {"ok", "no_change"} else 1
