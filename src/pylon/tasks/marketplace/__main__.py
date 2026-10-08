"""Low-frequency maintenance CLI, deliberately absent from Telegram menus."""
import argparse
import json
from pathlib import Path
from pylon.config import home
from pylon.outbox import Outbox
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(prog='pylon marketplace')
    parser.add_argument('action',choices=['history','categories','import-existing','refill','reclassify'])
    parser.add_argument('--store',type=Path,required=True,help='Isolated analysis SQLite; never the live outbox for history')
    parser.add_argument('--source',choices=['v2ex','chiphell','nga'])
    parser.add_argument('--days',type=int,default=90)
    parser.add_argument('--max-pages',type=int,default=5000)
    parser.add_argument('--seconds',type=int,default=3600)
    parser.add_argument('--delay',type=float,default=2)
    parser.add_argument('--list-only',action='store_true')
    parser.add_argument('--limit',type=int,default=100)
    args = parser.parse_args(argv)
    if args.store.resolve()==(home()/'data/outbox.sqlite3').resolve():
        parser.error('history analysis requires an isolated store')
    store = Store(Outbox(args.store))
    if args.action=='categories':
        result = store.stats()
    elif args.action=='reclassify':
        result = {'classified':store.reclassify_history()}
    elif args.action=='import-existing':
        from .history import import_existing
        result = import_existing(store,home())
    elif args.action=='refill':
        from .collection import collect_bodies
        selected=(args.source,) if args.source else ('v2ex','chiphell','nga')
        collect_bodies(store,max_seconds=args.seconds,batch_size=args.limit,selected_sources=selected)
        result = store.stats()['coverage']
    else:
        if not args.source:
            parser.error('history requires --source')
        from .history import backfill
        result = backfill(store,args.source,days=args.days,max_pages=args.max_pages,
                          delay=max(1,args.delay),max_seconds=args.seconds,fetch_bodies=not args.list_only)
    print(json.dumps(result,ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
