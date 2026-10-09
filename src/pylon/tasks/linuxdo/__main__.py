from __future__ import annotations

import argparse
import json
from pathlib import Path

from pylon import events
from pylon.config import home
from pylon.outbox import Outbox
from pylon.shared import state_json
from . import pipeline, policy
from .sources import Observation, Page
from .store import Store


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='Linux.do collection and value review')
    parser.add_argument('command', choices=['collect', 'review', 'status', 'replay', 'export-legacy'])
    parser.add_argument('--store', type=Path)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--limit', type=int, default=4)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'replay':
        import os
        production = Path(os.environ.get('PYLON_OUTBOX', str(home() / 'data/outbox.sqlite3'))).resolve()
        if not args.store or args.store.resolve() == production or not args.input:
            parser.error('replay requires --input and an isolated --store')
    store = Store(Outbox(args.store)) if args.store else Store()
    if args.command == 'collect':
        summary = pipeline.collect(store)
    elif args.command == 'review':
        summary = pipeline.review(store, limit=args.limit)
    elif args.command == 'status':
        print(json.dumps(store.stats(), ensure_ascii=False))
        return 0
    elif args.command == 'export-legacy':
        if not args.apply:
            parser.error('export-legacy requires --apply after stopping both new timers')
        original = state_json.load(pipeline.LEGACY_STATE, default={})
        state_json.save_atomic(pipeline.LEGACY_STATE, store.export_legacy(original))
        print(json.dumps({'exported': True}))
        return 0
    else:
        # The isolated database has no sender. Historical replay can evaluate and enqueue only there.
        data = json.loads(args.input.read_text())
        for row in data:
            post = Observation(**row)
            store.save_page(post.channel, Page([post], [post.message_id]), {'cursor': post.message_id})
        with store.box.connect() as db:
            db.execute('UPDATE linuxdo_topics SET next_attempt=0')
        summary = pipeline.review(store, config=policy.load(), limit=args.limit)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    events.result('ok', json.dumps(summary, ensure_ascii=False), changed=bool(summary.get('changed') or summary.get('queued')))
    # Notifications are already atomically persisted. Never let runner forward diagnostic JSON.
    print(json.dumps({'wakeAgent': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
