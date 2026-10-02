"""Durable Grok translation and value extraction worker for software updates."""

from __future__ import annotations

import argparse
import json

from . import monitor


def run(*, max_items: int = monitor.GROK_DIGEST_MAX_ITEMS_PER_RUN,
        dry_run: bool = False) -> dict:
    return monitor.process_pending_enrichment(max_items=max_items, dry_run=dry_run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Translate and distill pending software update items with Grok.')
    parser.add_argument('--max-items', type=int, default=monitor.GROK_DIGEST_MAX_ITEMS_PER_RUN)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    summary = run(max_items=max(1, args.max_items), dry_run=args.dry_run)
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if not summary.get('errors') else 1


if __name__ == '__main__':
    raise SystemExit(main())
