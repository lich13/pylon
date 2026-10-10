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
    from pylon.tasks.intelligence.health import record
    from pylon.outbox import Outbox
    # The worker records real source progress. A cached fallback can exit zero
    # without a fresh response, so the wrapper must not clear that incident.
    if result.returncode:
        record(Outbox(),'xianbao:collection',backlog=1,error='worker failed')
    if result.returncode == 0:
        state.unlink(missing_ok=True)
        return 0
    print(result.stderr + result.stdout, file=sys.stderr, end="")
    previous = re.sub(r"\D", "", state.read_text())[:9] if state.exists() else ""
    count = int(previous or 0) + 1
    state.write_text(f"{count}\n")
    state.chmod(0o600)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
