import argparse
import json
from pylon import events
from .pipeline import review
from .policy import SOURCES

def main():
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['review']);parser.add_argument('--limit',type=int,default=2)
    parser.add_argument('--source',choices=SOURCES)
    args=parser.parse_args();result=review(limit=args.limit,source=args.source)
    events.result('ok' if any(r['queued'] for r in result) else 'no_change',json.dumps(result,ensure_ascii=False))
    print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
