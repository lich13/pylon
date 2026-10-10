"""Reclassify an isolated history copy, with notifications structurally disabled."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from pylon.outbox import Outbox
from . import classifier,policy,wanted,enrich
from .store import Store


def replay(source: Path,target: Path):
    source=source.resolve();target=target.resolve()
    if source==target or target.exists():raise ValueError('replay requires a new isolated destination')
    target.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(target) as dst:src.backup(dst)
    target.chmod(0o600)
    store=Store(Outbox(target));sale=policy.load();buy=wanted.load()
    counts=Counter();examples={'new_match':[],'new_exclusion':[]};coverage=Counter()
    with store.box.connect() as db:
        if db.execute('SELECT COUNT(*) FROM marketplace_posts WHERE history=0 OR event_id IS NOT NULL').fetchone()[0]:
            raise ValueError('archive contains live notification associations')
        if db.execute('SELECT COUNT(*) FROM events').fetchone()[0]:raise ValueError('archive contains notifications')
        for row in db.execute('SELECT * FROM marketplace_posts').fetchall():
            old=json.loads(row['items']);old_match=bool(policy.evaluate(old,sale)+wanted.evaluate(old,buy))
            from .content_quality import evidence_scope
            new=classifier.classify_listing(row['title'],row['body'],row['source'],bool(row['body_complete']),platform_category=row['platform_category'],evidence_scope=evidence_scope(dict(row)))
            new_match=bool(policy.evaluate(new,sale)+wanted.evaluate(new,buy))
            analysis=enrich.needs_analysis(dict(row),new)
            counts['needs_analysis']+=analysis
            counts['direct_match']+=bool(new_match and not analysis)
            counts['posts']+=1;counts['old_match']+=old_match;counts['new_match_total']+=new_match
            counts['unknown_items']+=sum(i['category']=='unknown' for i in new)
            counts['item_count']+=len(new)
            coverage[row['source']+':complete' if row['body_complete'] else row['source']+':missing']+=1
            if old_match!=new_match:
                change='new_match' if new_match else 'new_exclusion';counts[change]+=1
                if len(examples[change])<20:
                    examples[change].append({'source':row['source'],'id':row['source_id'],
                        'old':list({i['category'] for i in old}),'new':list({i['category'] for i in new}),
                        'requires_analysis':analysis})
            db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=? AND history=1',
                       (json.dumps(new,ensure_ascii=False),row['source'],row['source_id']))
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    return {'counts':dict(counts),'coverage':dict(coverage),'examples':examples,'notifications':0}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('source',type=Path);parser.add_argument('target',type=Path)
    args=parser.parse_args();print(json.dumps(replay(args.source,args.target),ensure_ascii=False))


if __name__=='__main__':main()
