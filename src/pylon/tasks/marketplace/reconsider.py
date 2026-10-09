"""Bounded migration recheck: recent undelivered posts require a fresh page."""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import control, sources
from .classifier import classify_listing
from .collection import client, INTERVALS
from .store import Store

CHECKPOINT='migration:market-v11:recent'


def stage(store, *, now=None):
    now=time.time() if now is None else now
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT value FROM marketplace_checkpoints WHERE name=?',(CHECKPOINT,)).fetchone()
        if old:return json.loads(old[0])
        changed=db.execute("""UPDATE marketplace_posts SET status='refresh_required'
            WHERE history=0 AND event_id IS NULL AND first_seen>=? AND first_seen<=?
              AND status IN ('ready','skipped','needs_review','pending_analysis','pending_body','unavailable')""",(now-86400,now)).rowcount
        result={'started_at':now,'cutoff':now-86400,'staged':changed}
        db.execute('INSERT INTO marketplace_checkpoints VALUES(?,?,?)',(CHECKPOINT,control.dumps(result),now))
    return result


def accept_fresh(store, old, fresh):
    if not fresh.get('title_refreshed') or not fresh.get('body_complete') or not fresh.get('body_full'):
        raise sources.ContentUnavailable('fresh_title_or_body_missing')
    items=classify_listing(fresh['title'],fresh['body_full'],old['source'],True,
                           platform_category=fresh.get('platform_category',''))
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        result=db.execute("""UPDATE marketplace_posts SET title=?,body=?,body_complete=1,items=?,
            platform_category=?,posted_at=?,updated_at=?,status='ready',error=''
            WHERE source=? AND source_id=? AND history=0 AND event_id IS NULL AND status='refresh_required'""",
            (fresh['title'],fresh['body_full'],control.dumps(items),fresh.get('platform_category',''),
             fresh.get('posted_at',old['posted_at']),time.time(),old['source'],old['source_id']))
        return bool(result.rowcount)


def drain_source(store,source):
    with store.box.connect() as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM marketplace_posts WHERE source=? AND history=0 AND event_id IS NULL AND status='refresh_required' ORDER BY first_seen",(source,))]
    stats={'source':source,'total':len(rows),'fresh':0,'review':0,'limited':0}
    if not rows:return stats
    last=0.0
    with client(source) as (ctx,page):
        for row in rows:
            delay=last+INTERVALS[source]-time.monotonic()
            if delay>0:time.sleep(delay)
            last=time.monotonic()
            try:
                fresh=sources.detail(ctx,source,{**row,'id':row['source_id']},page=page)
                stats['fresh']+=int(accept_fresh(store,row,fresh))
            except sources.SourceRateLimited:
                stats['limited']+=1
                break
            except Exception as exc:
                reason=str(exc) if isinstance(exc,sources.ContentUnavailable) else type(exc).__name__
                with store.box.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    db.execute("UPDATE marketplace_posts SET status='needs_review',error=? WHERE source=? AND source_id=? AND status='refresh_required' AND event_id IS NULL",('fresh_recheck:'+reason,row['source'],row['source_id']))
                    control.review(db,row,-1,'补发前无法重新确认原帖：'+reason)
                stats['review']+=1
    return stats


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--stage',action='store_true')
    args=parser.parse_args();store=Store()
    if args.stage:print(json.dumps(stage(store)));return
    if not store.checkpoint(CHECKPOINT):raise ValueError('stage the authorized 24-hour migration first')
    with ThreadPoolExecutor(max_workers=3) as pool:
        for result in pool.map(lambda source:drain_source(store,source),INTERVALS):
            print(json.dumps(result),flush=True)


if __name__=='__main__':main()
