"""Native systemd scheduling; no resident scheduler or HTTP control server."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import jobs, redact

ADMIN = "/usr/local/sbin/pylon-admin"
CHIPHELL = "63c6ab6c4af3"


class AdminError(RuntimeError):
    def __init__(self, result):
        self.returncode = result.returncode
        self.stdout = redact(result.stdout)
        super().__init__(redact(result.stderr or result.stdout)[-1600:])


def unit(job_id: str, kind: str = "timer") -> str:
    if job_id not in jobs():
        raise ValueError("任务不存在")
    return f"pylon-job-{job_id}.timer" if kind == "timer" else f"pylon-{kind}@{job_id}.service"


def admin(*args: str, timeout: int = 160) -> str:
    result = subprocess.run(["sudo", "-n", ADMIN, *args], capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise AdminError(result)
    return result.stdout.strip()


def states() -> dict[str, dict]:
    names = [unit(key, kind) for key in jobs() for kind in ("timer", "task", "manual")]
    result = subprocess.run(["systemctl", "show", *names, "--no-pager",
        "--property=Id,ActiveState,UnitFileState,NextElapseUSecRealtime,NextElapseUSecMonotonic"],
        capture_output=True, text=True, timeout=15, check=True)
    records = {}
    for block in result.stdout.strip().split("\n\n"):
        row = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        records[row.get("Id", "")] = row
    output = {}
    schedule = subprocess.run(['systemctl', 'list-timers', '--all', '--output=json', '--no-pager', 'pylon-job-*.timer'],
                              capture_output=True, text=True, timeout=15, check=True)
    next_times = {item['unit']: item.get('next', 0) for item in json.loads(schedule.stdout)}
    for key in jobs():
        timer = records.get(unit(key), {})
        next_time = next_times.get(unit(key), 0)
        output[key] = {"paused": timer.get("ActiveState") != "active",
            "enabled": timer.get("UnitFileState") == "enabled",
            "running": any(records.get(unit(key, kind), {}).get("ActiveState") in {"active", "activating"}
                           for kind in ("task", "manual")),
            "next": datetime.fromtimestamp(next_time / 1e6, ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M:%S') if next_time and next_time < 1e18 else ''}
    return output


def paused(job_id: str) -> bool:
    result = subprocess.run(['systemctl', 'show', unit(job_id), '-p', 'ActiveState', '--value'],
                            capture_output=True, text=True, timeout=15, check=True)
    return result.stdout.strip() != 'active'


def suspend(job_id: str, paused: bool):
    return admin("pause" if paused else "resume", job_id)


def start(job_id: str) -> str:
    unit(job_id)
    return admin("run", job_id)


def logs(job_id: str) -> str:
    unit(job_id)
    return redact(admin("logs", job_id)) or "暂无日志"


def ssh(action: str, value: str | None = None) -> dict:
    return json.loads(admin("ssh", action, *([value] if value else []), timeout=900))
