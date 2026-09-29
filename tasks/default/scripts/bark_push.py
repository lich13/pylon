#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.webops.bark import VALID_LEVELS, send_push


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Reusable Bark push sender for private-profile monitors.")
    ap.add_argument("--title", required=True)
    ap.add_argument("--body", help="Push body. If omitted, reads stdin.")
    ap.add_argument("--url", default="")
    ap.add_argument("--group", default="webops")
    ap.add_argument("--level", choices=sorted(VALID_LEVELS), default="active")
    ap.add_argument("--volume", type=int, default=5)
    ap.add_argument("--critical", action="store_true", help="Shortcut for --level critical --volume 10.")
    ap.add_argument("--secret-name", help="Secret filename under /srv/pylon/profiles/default/secrets/.")
    ap.add_argument("--key-file", type=Path, help="Explicit Bark device-key file path.")
    ap.add_argument("--api-base", help="Override Bark API base; useful for mock tests.")
    ap.add_argument("--timeout-seconds", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    body = args.body if args.body is not None else sys.stdin.read()
    level = "critical" if args.critical else args.level
    volume = 10 if args.critical else args.volume
    result = send_push(
        title=args.title,
        body=body,
        url=args.url,
        group=args.group,
        level=level,
        volume=volume,
        secret_name=args.secret_name,
        key_file=args.key_file,
        api_base=args.api_base,
        timeout_seconds=args.timeout_seconds,
        dry_run=args.dry_run,
    )
    print(json.dumps({"ok": result.ok, "http_status": result.http_status, "code": result.code, "message": result.message}, ensure_ascii=False))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
