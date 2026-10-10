"""One-time cutover of public Helper state without changing delivery identity."""
from __future__ import annotations

import json
import time
from .store import Store,active
from .pipeline import capture_linuxdo
from pylon.tasks.linuxdo.common import timestamp
from pylon.tasks.linuxdo.helper import SOURCE

KEY='migration:intelligence-v1'


def migrate(box=None, *, now=None):
    from pylon.tasks.linuxdo.store import Store as LinuxStore
    from pylon.tasks.marketplace.store import Store as MarketStore
    from pylon.tasks.marketplace import reconsider
    now=time.time() if now is None else now
    store=Store(box);legacy=LinuxStore(store.box);market_store=MarketStore(store.box)
    with store.box.connect() as db:
        if db.execute('SELECT 1 FROM kv WHERE key=?',(KEY,)).fetchone():return {'already_applied':True}
        active(db)
        ids=[r[0] for r in db.execute('SELECT DISTINCT topic_id FROM linuxdo_observations WHERE channel=?',(SOURCE,))]
    recent_market=reconsider.stage(market_store,now=now,checkpoint=reconsider.INTELLIGENCE_CHECKPOINT,include_linuxdo=False)
    counts={'historical':0,'recent':0,'protected':0,'cleared_markers':0,'suppressed_alerts':0,
            'market_refresh_staged':recent_market['staged']}
    for topic_id in ids:
        snapshot=legacy.snapshot(topic_id)
        stamps=[timestamp(e.get('source_edited_at') or e.get('source_published_at','')) for e in snapshot['evidence']
                if e['ref'].startswith('tg:'+SOURCE+':')]
        published=max((s for s in stamps if s is not None),default=snapshot['first_seen'])
        with store.box.connect() as db:
            market=db.execute("SELECT event_id FROM marketplace_posts WHERE source='linuxdo' AND source_id=?",(topic_id,)).fetchone()
        protected=snapshot['previously_notified'] or bool(market and market[0])
        recent=(now-86400<=published<=now+300 and not snapshot['historical'] and not protected
                and not (snapshot['expires_at'] and snapshot['expires_at']<=now))
        snapshot['historical']=not recent
        capture_linuxdo(legacy,snapshot)
        with store.box.connect() as db:
            # Historical outcomes are visible in preview; they never enter claim.
            old=db.execute("SELECT result FROM linuxdo_decisions WHERE topic_id=? AND json_type(result,'$.facts')='array' ORDER BY created_at DESC LIMIT 1",(topic_id,)).fetchone()
            if old and not recent:
                db.execute("UPDATE intel_items SET result=?,status='historical' WHERE source='linuxdo' AND source_id=?",(old[0],topic_id))
            db.execute("UPDATE linuxdo_topics SET status='intelligence',lease_until=0,error='' WHERE topic_id=? AND status IN ('pending','hold','skip','marketplace')",(topic_id,))
        counts['recent' if recent else 'historical']+=1;counts['protected']+=int(protected)
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        counts['cleared_markers']=db.execute("DELETE FROM linuxdo_meta WHERE key LIKE 'market_recheck:%'").rowcount
        counts['suppressed_alerts']=db.execute("""UPDATE events SET status='suppressed',error='retired transient monitor notice',updated_at=?
            WHERE status='pending' AND attempts=0 AND kind='text' AND (
              json_extract(payload,'$.text') LIKE 'Linux.do 价值判断连续失败%' OR
              json_extract(payload,'$.text') LIKE 'Linux.do 价值判断已恢复%' OR
              json_extract(payload,'$.text') LIKE 'Linux.do Helper 连续采集失败%' OR
              json_extract(payload,'$.text') LIKE 'Linux.do Helper 采集已恢复%' OR
              json_extract(payload,'$.text') LIKE 'Linux.do 来源 %连续采集失败%' OR
              json_extract(payload,'$.text') LIKE 'Linux.do 来源 %已恢复%')""",(now,)).rowcount
        # Limited Helper excerpts are an evidence scope, not a body-fetch failure.
        db.execute("UPDATE market_reviews SET status='resolved',updated_at=? WHERE source='linuxdo' AND item_index=-3 AND status='pending'",(now,))
        db.execute('INSERT INTO kv VALUES(?,?)',(KEY,json.dumps(counts)))
    return counts


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    if not args.apply:parser.error('stop affected tasks first, then use --apply')
    print(json.dumps(migrate()))
