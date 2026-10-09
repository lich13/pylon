"""Explicit one-time policy migration; never requeues historic notifications."""
from __future__ import annotations

import argparse
import json
import time
from . import control
from .store import Store

CHECKPOINT='migration:market-v12'


def apply(store,prices):
    from pylon.tasks.linuxdo.trade import validate_config
    validate_config(prices)
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        done=db.execute('SELECT value FROM marketplace_checkpoints WHERE name=?',(CHECKPOINT,)).fetchone()
        if done:return json.loads(done[0])
        version,value=control.baseline(db)
        content=json.loads(control.dumps(value));sale=content['sale']
        blocked=sale.setdefault('blocked_categories',[])
        if 'wearable.child_watch' not in blocked:blocked.append('wearable.child_watch')
        rules=sale.setdefault('filters',{}).setdefault('virtual',{})
        for field,values in (('product',['88VIP','Apple Music','苹果音乐']),('origin',['88VIP'])):
            excluded=rules.setdefault(field,{}).setdefault('exclude',[])
            for term in values:
                if term not in excluded:excluded.append(term)
        content['ai_prices']=prices
        new=control.activate(db,content,version,'market-v12-approved-migration')
        db.execute('DELETE FROM market_inputs')
        db.execute("UPDATE market_drafts SET status='superseded' WHERE status='draft'")
        # Unsent old configuration cards must not reappear after cutover.
        suppressed=0
        for row in db.execute("SELECT id,payload FROM events WHERE profile='webops' AND status IN ('pending','failed')").fetchall():
            payload=json.loads(row['payload'])
            buttons=[b for line in payload.get('reply_markup',{}).get('inline_keyboard',[]) for b in line]
            if any(str(b.get('callback_data','')).startswith(('mk1|','ch1|','ch2|')) for b in buttons):
                suppressed+=db.execute("UPDATE events SET status='suppressed',error='retired_market_controls',updated_at=? WHERE id=?",(time.time(),row['id'])).rowcount
        result={'previous_version':version,'version':new,'price_rules':len(prices['rules']),'suppressed_cards':suppressed}
        db.execute('INSERT INTO marketplace_checkpoints VALUES(?,?,?)',(CHECKPOINT,control.dumps(result),time.time()))
        return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--apply',action='store_true',required=True)
    parser.parse_args()
    from pylon.config import profile_root,home
    from pylon.outbox import Outbox
    from pylon.tasks.linuxdo.trade import load
    store=Store();result=apply(store,load(profile_root('webops')/'config/linuxdo_prices.json'))
    stores=[store];archive=home()/'data/marketplace-review.sqlite3'
    if archive.exists():
        stores.append(Store(Outbox(archive)))
        result['linuxdo_history']=import_linuxdo_history(store,stores[-1])
    result['history_indexes']=[refresh_history(s) for s in stores]
    result['reviews']=refresh_reviews(store)
    print(json.dumps(result))


def refresh_history(store):
    checkpoint=CHECKPOINT+':history-index'
    done=store.checkpoint(checkpoint)
    if done:return done
    from .content_quality import contamination
    invalid=0
    with store.box.connect() as db:
        for row in db.execute('SELECT * FROM marketplace_posts WHERE body_complete=1').fetchall():
            reason=contamination(row['source'],row['title'],row['body'])
            if not reason:continue
            invalid+=1
            db.execute('UPDATE marketplace_posts SET body_complete=0,error=? WHERE source=? AND source_id=?',
                       (reason,row['source'],row['source_id']))
            if not row['history'] and not row['event_id']:control.review(db,dict(row),-1,reason)
    result={'reclassified':store.reclassify_history(),'invalidated_body':invalid}
    store.checkpoint(checkpoint,result)
    return result


def refresh_reviews(store):
    """Reassess retained review records without making any post runnable."""
    checkpoint=CHECKPOINT+':reviews'
    done=store.checkpoint(checkpoint)
    if done:return done
    from . import classifier,enrich,policy,wanted
    reviewed=resolved=0
    with store.box.connect() as db:
        version,content=control.baseline(db)
        rows=db.execute("""SELECT p.* FROM marketplace_posts p WHERE p.history=0 AND EXISTS
            (SELECT 1 FROM market_reviews r WHERE r.source=p.source AND r.source_id=p.source_id AND r.status='pending')""").fetchall()
        for raw in rows:
            post=dict(raw);reviewed+=1
            items=classifier.classify_listing(post['title'],post['body'],post['source'],bool(post['body_complete']),platform_category=post['platform_category'])
            decisions=[]
            for item in items:
                state,reasons=policy.decision(item,content['sale'])
                if item['status']=='buy':state,reasons=('match',[]) if wanted.evaluate([item],content['wanted']) else ('exclude',['不符合独立求购目标'])
                from .ai_prices import is_ai
                if state=='match' and item['status']!='buy' and is_ai(item):state,reasons='review',['AI 报价条件需要按统一上限核验']
                decisions.append({'index':item['index'],'category':item['category'],'state':state,'reasons':reasons})
            pending=any(d['state']=='review' for d in decisions) or enrich.needs_analysis(post,items)
            db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=?',(control.dumps(items),post['source'],post['source_id']))
            if not pending:
                resolved+=1
                db.execute("UPDATE market_reviews SET status='resolved',updated_at=? WHERE source=? AND source_id=?",(time.time(),post['source'],post['source_id']))
            for decision in decisions:decision['reasons'].append('历史复核不自动补发；近期漏推仍需新鲜读取')
            control.audit(db,post,decisions,status=post['status'],event_id=post['event_id'],policy_version=version)
    result={'reviewed_posts':reviewed,'resolved_posts':resolved}
    store.checkpoint(checkpoint,result)
    return result


def import_linuxdo_history(live,archive):
    checkpoint=CHECKPOINT+':linuxdo-history'
    done=archive.checkpoint(checkpoint)
    if done:return done
    with live.box.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='linuxdo_topics'").fetchone():return {'imported':0}
        ids=[r[0] for r in db.execute('SELECT topic_id FROM linuxdo_topics')]
    from pylon.tasks.linuxdo.store import Store as LinuxStore
    from .linuxdo_bridge import owned_snapshot
    linux=LinuxStore(live.box);records=[]
    for identifier in ids:
        snap=owned_snapshot(linux.snapshot(identifier));evidence=snap['evidence']
        first=next((r for r in evidence if r['post_number']==1),None)
        if not first:continue
        body='\n\n'.join(dict.fromkeys(r['body'] for r in evidence))
        complete=any(r['post_number']==1 and r.get('complete') for r in evidence)
        records.append({'id':identifier,'title':snap['title'],'url':'https://linux.do/t/topic/'+identifier,
            'body_full':body,'body_complete':complete,'posted_at':first.get('source_published_at',''),
            'body_provenance':{'version':1,'identity_verified':complete,'container':'post_stream' if complete else 'telegram_excerpt'}})
    result={'imported':archive.ingest('linuxdo',records,history=True),'retained_first_posts':len(records)}
    archive.checkpoint(checkpoint,result)
    return result


if __name__=='__main__':main()
