"""Transfer public Linux.do trades into the shared durable marketplace."""
from __future__ import annotations

import json
import time
from . import control, classifier


def owned_snapshot(snapshot):
    """First post and evidenced same-author supplements, never other replies."""
    evidence=snapshot['evidence'];actors=snapshot.get('actors',{})
    owners={actors[row['ref']] for row in evidence if row.get('post_number')==1 and actors.get(row['ref'])}
    rows=[row for row in evidence if row.get('origin')!='official' and (
        row.get('post_number')==1 or len(owners)==1 and actors.get(row['ref']) in owners)]
    # Prefer complete topic text over the same forwarded excerpt.
    rows=[row for row in rows if row.get('complete') or not any(
        other.get('complete') and other['post_number']==row['post_number'] for other in rows)]
    rows=[{**row,'body':row.get('owned_body',row['body'])} for row in rows]
    return {**snapshot,'evidence':rows,'fingerprint':control.digest(rows),
            'actors':{r['ref']:actors[r['ref']] for r in rows if r['ref'] in actors}}


def transfer(store,snapshot,extraction):
    from .store import Store
    from . import pipeline
    market=Store(store.box)
    owned=owned_snapshot(snapshot)
    rows=owned['evidence']
    primary=next((r for r in rows if r.get('post_number')==1 and r.get('complete')),None)
    body='\n\n'.join(dict.fromkeys(r['body'] for r in rows))
    title=primary['title'] if primary else snapshot['title']
    complete=bool(primary and primary['body'])
    record={'source':'linuxdo','source_id':snapshot['topic_id'],'title':title,'body':body,
            'url':'https://linux.do/t/topic/'+snapshot['topic_id'],'body_complete':int(complete),
            'source_context':control.dumps({'snapshot':owned,'trade_extraction':extraction}),
            'posted_at':next((r.get('source_published_at','') for r in rows if r.get('source_published_at')),'')}
    items=classifier.classify_listing(title,body,'linuxdo',complete)
    # Validated AI offers already own their evidence, so retain them even when
    # a local lexicon has never seen this tier or its spelling.
    from .ai_prices import offers_to_items,config
    ai=offers_to_items(record,extraction,config())
    if ai:
        items=[i for i in items if not i['category'].startswith('virtual.') or not __import__('pylon.tasks.marketplace.ai_prices',fromlist=['is_ai']).is_ai(i)]+ai
    if extraction['intent']=='unknown':
        for item in items:item['attributes']['trade_intent_pending']=True
    for i,item in enumerate(items):item['index']=i
    now=time.time()
    with market.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT * FROM linuxdo_topics WHERE topic_id=?',(snapshot['topic_id'],)).fetchone()
        if current['version']!=snapshot['version']:
            db.execute("UPDATE linuxdo_topics SET status='pending',lease_until=0 WHERE topic_id=?",(snapshot['topic_id'],))
            return {'decision':'hold','event_id':None,'error':''}
        old=db.execute("SELECT * FROM marketplace_posts WHERE source='linuxdo' AND source_id=?",(snapshot['topic_id'],)).fetchone()
        status='historical' if current['historical'] else 'ready'
        if not old:
            db.execute('''INSERT INTO marketplace_posts
                (source,source_id,url,title,body,body_complete,posted_at,first_seen,updated_at,items,status,history,source_context,body_provenance)
                VALUES('linuxdo',?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (snapshot['topic_id'],record['url'],title,body,int(complete),record['posted_at'],now,now,
                 control.dumps(items),status,current['historical'],record['source_context'],
                 control.dumps({'version':1,'topic_id':snapshot['topic_id'],'identity_verified':complete,'container':'post_stream'})))
        elif not old['history'] and old['source_context']!=record['source_context'] and (old['status']!='queued' or ai):
            db.execute("""UPDATE marketplace_posts SET title=?,body=?,body_complete=?,items=?,source_context=?,status='ready',updated_at=?
                WHERE source='linuxdo' AND source_id=?""",(title,body,int(complete),control.dumps(items),record['source_context'],now,snapshot['topic_id']))
        db.execute("UPDATE linuxdo_topics SET status=?,lease_until=0,error='',next_attempt=?,attempts=attempts+1 WHERE topic_id=?",
                   ('marketplace' if complete or current['attempts']>=3 else 'hold',now+600,snapshot['topic_id']))
        if not complete and not current['historical']:
            control.review(db,record,-3,'首楼正文尚不可读，目前只有频道摘要；已分开记录为采集问题')
    pipeline.process(market,source='linuxdo')
    return {'decision':'marketplace','event_id':None,'error':''}
