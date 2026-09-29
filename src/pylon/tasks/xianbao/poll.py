"""One poll with persistent consecutive-failure accounting."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

from pylon.config import read_env


def main() -> int:
    root = Path(os.environ.get("PYLON_PROFILE_ROOT", "/srv/pylon/profiles/default"))
    env = os.environ.copy()
    env.update(read_env(root / "config/xianbao-filter.env"))
    state = root / "data/xianbao_filter/cron_poll_failures"
    state.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run([sys.executable, "-m", "pylon.tasks.xianbao.worker", "--once"],
                            env=env, capture_output=True, text=True)
    if result.returncode == 0:
        state.unlink(missing_ok=True)
        return 0
    print(result.stderr + result.stdout, file=sys.stderr, end="")
    previous = re.sub(r"\D", "", state.read_text())[:9] if state.exists() else ""
    count = int(previous or 0) + 1
    state.write_text(f"{count}\n")
    state.chmod(0o600)
    threshold = env.get("XIANBAO_ALERT_CONSECUTIVE_ERRORS", "3")
    threshold = int(threshold) if threshold.isdigit() else 3
    if threshold > 0 and count >= threshold:
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()
        message = f"worker loop has {count} consecutive poll errors; latest error: {detail[-1][:600] if detail else ''}"
        subprocess.run([sys.executable, "-m", "pylon.tasks.xianbao.alert", "--kind", "runtime-error",
                        "--unit", "xianbao-poll", "--journal-lines", "0", "--message", message],
                       env=env, capture_output=True, text=True, timeout=25)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
