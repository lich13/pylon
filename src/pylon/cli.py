from __future__ import annotations

import argparse
import json
import os
import sys

from .config import home, jobs
from .outbox import Outbox


def main():
    parser = argparse.ArgumentParser(prog="pylon")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("daemon")
    sub.add_parser("status")
    sub.add_parser("outbox")
    sub.add_parser("reconcile")
    run = sub.add_parser("run")
    run.add_argument("job_id")
    for command in ["pause", "resume"]:
        p = sub.add_parser(command)
        p.add_argument("job_id")
    args = parser.parse_args()
    if args.command == "run":
        from .runner import run
        return run(args.job_id)
    if args.command == "daemon":
        from .bot import main as serve
        return serve()
    if args.command in {"pause", "resume"}:
        from .commands import control_task
        print(control_task(args.job_id, args.command == "pause"))
        return 0
    box = Outbox()
    if args.command in {"status", "outbox", "reconcile"}:
        with box.connect() as db:
            runs = [dict(row) for row in db.execute("SELECT job_id,status,started_at,finished_at,detail FROM runs WHERE rowid IN (SELECT max(rowid) FROM runs GROUP BY job_id) ORDER BY job_id")]
            unresolved = [dict(row) for row in db.execute("SELECT id,job_id,kind,status,attempts,error FROM events WHERE status!='delivered' ORDER BY created_at LIMIT 30")]
        print(json.dumps({"jobs": runs, "outbox": box.counts(), "unresolved": unresolved}, ensure_ascii=False))
        return int(args.command == "reconcile" and bool(unresolved))
    return 0


if __name__ == "__main__":
    sys.exit(main())
