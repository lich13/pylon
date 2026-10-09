"""Shared evidence-backed AI prices for every marketplace adapter."""
from __future__ import annotations

import json
import re
import time
from decimal import Decimal
from pylon.config import profile_root
from pylon.tasks.linuxdo import trade
from . import control, classifier

SCHEMA='''CREATE TABLE IF NOT EXISTS market_ai_prices (
 fingerprint TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
 status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 started_at REAL, result TEXT, error TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS market_ai_receipts (
 offer_key TEXT NOT NULL, event_key TEXT PRIMARY KEY, event_id TEXT NOT NULL,
 comparable_price TEXT NOT NULL, change_value TEXT NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS market_ai_receipt_identity ON market_ai_receipts(offer_key,created_at);
'''
AI=re.compile(r'ChatGPT|(?<![A-Za-z])GPT|Claude|Grok|Gemini|Codex|Cursor|Kimi|AI.{0,6}(?:会员|账号|订阅)',re.I)


def config(db=None):
    active=control.current(db)
    value=active[1].get('ai_prices') if active else None
    return trade.validate_config(value) if value else trade.load(profile_root('webops')/'config/linuxdo_prices.json')


def snapshot(post):
    context=json.loads(post.get('source_context') or '{}')
    if context.get('snapshot'):return context['snapshot']
    ref=post['source']+':'+post['source_id']
    return {'topic_id':post['source_id'],'title':post['title'],'updated_at':post['updated_at'],
        'fingerprint':control.digest([post['title'],post['body']]),'previously_notified':False,
        'actors':{ref:context.get('actor') or ref},
        'evidence':[{'ref':ref,'origin':'topic','post_number':1,'title':post['title'],
            'body':post['body'],'complete':bool(post['body_complete']),'source_published_at':post['posted_at'],
            'source_edited_at':'','url':post['url']} ]}


def is_ai(item):
    return item['category']=='virtual.ai_membership' or (
        item['category'].startswith('virtual.') and AI.search(item.get('name') or item['attributes'].get('subject','')))


def fingerprint(post, prices):
    return control.digest([post['source'],post['source_id'],post['title'],post['body'],prices,trade.VERSION,'market-ai-1'])


def offers_to_items(post, extraction, prices):
    if extraction['intent']!='offer':return []
    snap=snapshot(post);items=[]
    for offer in extraction['offers']:
        if offer['product']=='other':continue
        description=offer['description']['value']
        parts=[description]+[v['quote'] for k,v in offer.items() if isinstance(v,dict) and v.get('quote')]
        text='\n'.join(dict.fromkeys(parts))
        # These are individually validated excerpts, not a claim that the
        # original forum body is complete. Keep the proof text for attributes;
        # the post's completeness flag still guards physical arbitrage.
        parsed=classifier.classify_listing(description,text,post['source'],True,single_item=True)
        if not parsed:continue
        item=parsed[0];result=trade.evaluate(offer,prices,snap)
        item.update(category='virtual.ai_membership',name=description[:160],index=len(items),
                    status='sold' if offer['availability']=='sold' else 'available',
                    price=float(offer['price']['value']) if offer['price']['value'] else None)
        item['attributes'].update(ai_price_result=result,price_status='identified' if item['price'] else 'missing',
            analysis_version='ai-price-1',analysis_fields=[],intent='sale',
            links=__import__('pylon.tasks.marketplace.enrich',fromlist=['links']).links(text))
        item['attributes']['facts'].update(product=description,platform=[offer['product']],plan=[offer['tier']['value']])
        items.append(item)
    return items


def prepare(db,post,items):
    """Return normal items immediately while price work runs in its own queue."""
    needed=[i for i in items if is_ai(i) and i['status']=='available' and not i['attributes'].get('trade_intent_pending')]
    if not needed:return items,False
    prices=config(db)
    # Linux.do already extracted these facts. Re-evaluate today's price limits
    # without another model request or losing the seller's evidence.
    if all(i['attributes'].get('ai_price_result',{}).get('offer') for i in needed):
        for i in needed:
            i['attributes']['ai_price_result']=trade.evaluate(i['attributes']['ai_price_result']['offer'],prices,snapshot(post))
        return items,False
    key=fingerprint(post,prices)
    row=db.execute('SELECT * FROM market_ai_prices WHERE fingerprint=?',(key,)).fetchone()
    if row and row['status']=='complete':
        enriched=json.loads(row['result'])
        for i in enriched:i['attributes']['ai_price_result']=trade.evaluate(i['attributes']['ai_price_result']['offer'],prices,snapshot(post))
        next_index=max((i['index'] for i in items),default=-1)+1
        for n,item in enumerate(enriched):item['index']=needed[n]['index'] if n<len(needed) else next_index+n
        return [i for i in items if i not in needed]+enriched,False
    if not row:
        db.execute("INSERT INTO market_ai_prices(fingerprint,source,source_id,status) VALUES(?,?,?,'pending')",
                   (key,post['source'],post['source_id']))
    from .control import review
    if row and row['status']=='error':
        review(db,post,-2,'AI 报价条件仍待核对：'+row['error'])
        for i in needed:i['attributes']['ai_price_result']={'decision':'hold','reason':'AI 报价条件仍待核对：'+row['error']}
        return items,False
    return [i for i in items if i not in needed],True


def delivery(db, post, items):
    """Reserve nothing until the caller persists its one outbox event."""
    from pylon.tasks.linuxdo.trade_store import identity
    snap=snapshot(post);keep=[];claims=[]
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for item in items:
        result=item['attributes'].get('ai_price_result')
        if not result:
            if not post.get('event_id'):keep.append(item)
            continue
        offer=result['offer'];offer_key=identity(snap,result)
        if not offer_key:
            item['attributes']['delivery_review']='交易作者身份尚不明确'
            control.review(db,post,item['index'],'交易作者身份尚不明确');continue
        prior=db.execute('''SELECT r.* FROM market_ai_receipts r JOIN events e ON e.id=r.event_id
            WHERE offer_key=? AND e.status<>'suppressed' ORDER BY created_at DESC LIMIT 1''',(offer_key,)).fetchone()
        if not prior and post['source']=='linuxdo' and 'linuxdo_trade_notifications' in tables:
            prior=db.execute('''SELECT n.* FROM linuxdo_trade_notifications n JOIN events e ON e.id=n.event_id
                WHERE offer_key=? AND e.status<>'suppressed' ORDER BY created_at DESC LIMIT 1''',(offer_key,)).fetchone()
        previous_change=''
        if prior:
            if 'change_value' in prior.keys():previous_change=prior['change_value']
            else:
                quote=db.execute('SELECT result FROM linuxdo_trade_quotes WHERE offer_key=? ORDER BY observed_at DESC LIMIT 1',(offer_key,)).fetchone()
                if quote:previous_change=json.loads(quote[0])['offer']['event']['value']
        change=offer['event'];fresh=bool(offer['change']!='initial' and change['value'] and change['value']!=previous_change and
            re.search(r'补货|恢复|重新开放|新增|增加|再次|更正|修正|纠正|restock|reopen|correct',change['quote'],re.I))
        if post.get('event_id') and not prior and not fresh:
            item['attributes']['delivery_excluded']='该帖已投递，后续分析只更新记录，不重发原帖'
            continue
        legacy=False
        if post['source']=='linuxdo' and 'linuxdo_legacy_topics' in tables:
            legacy=bool(db.execute('SELECT 1 FROM linuxdo_legacy_topics WHERE topic_id=?',(post['source_id'],)).fetchone() or
                db.execute('''SELECT 1 FROM linuxdo_notifications n JOIN events e ON e.id=n.event_id
                    WHERE topic_id=? AND e.status<>'suppressed' AND NOT EXISTS
                    (SELECT 1 FROM linuxdo_trade_notifications t WHERE t.event_id=n.event_id) LIMIT 1''',(post['source_id'],)).fetchone())
        comparable=result.get('unit_price') or result['monthly']
        event_key=control.digest([offer_key,comparable,result['total'],change['value'] if fresh else ''])
        duplicate=db.execute('SELECT 1 FROM market_ai_receipts WHERE event_key=?',(event_key,)).fetchone()
        if duplicate or (legacy and not prior and not fresh) or (prior and not fresh and Decimal(comparable)>Decimal(prior['comparable_price'])*Decimal('0.90')):
            item['attributes']['delivery_excluded']='同一卖家报价已通知，未发现足够降价或新权益'
            continue
        keep.append(item);claims.append((offer_key,event_key,comparable,change['value']))
    return keep,claims


def acknowledge(db,claims,event_id):
    for offer_key,event_key,price,change in claims:
        db.execute('INSERT OR IGNORE INTO market_ai_receipts VALUES(?,?,?,?,?,?)',
                   (offer_key,event_key,event_id,price,change,time.time()))


def run_pending(store,limit=1):
    count=0
    for _ in range(limit):
        with store.box.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE market_ai_prices SET status='pending',error='interrupted' WHERE status='running' AND started_at<?",(time.time()-180,))
            row=db.execute("SELECT * FROM market_ai_prices WHERE status='pending' AND next_attempt<=? AND attempts<3 ORDER BY next_attempt LIMIT 1",(time.time(),)).fetchone()
            if not row:break
            job=dict(row)
            post=dict(db.execute('SELECT * FROM marketplace_posts WHERE source=? AND source_id=?',(job['source'],job['source_id'])).fetchone())
            prices=config(db)
            db.execute("UPDATE market_ai_prices SET status='running',attempts=attempts+1,started_at=? WHERE fingerprint=?",(time.time(),job['fingerprint']))
        try:
            snap=snapshot(post);context=json.loads(post.get('source_context') or '{}')
            extraction=trade.validate(context.get('trade_extraction') or trade.request(snap),snap)
            items=offers_to_items(post,extraction,prices)
            if not items:raise trade.TradeError('未能确认 AI 商品及报价条件')
            with store.box.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current=dict(db.execute('SELECT * FROM marketplace_posts WHERE source=? AND source_id=?',(post['source'],post['source_id'])).fetchone())
                if fingerprint(current,config(db))!=job['fingerprint']:
                    db.execute("UPDATE market_ai_prices SET status='error',error='source_changed' WHERE fingerprint=?",(job['fingerprint'],))
                    continue
                db.execute("UPDATE market_ai_prices SET status='complete',result=?,error='' WHERE fingerprint=?",(control.dumps(items),job['fingerprint']))
                db.execute("UPDATE marketplace_posts SET status='ready' WHERE source=? AND source_id=? AND history=0 AND status IN ('pending_price','needs_review','queued')",
                           (post['source'],post['source_id']))
            count+=1
        except Exception as error:
            reason=str(error) if isinstance(error,trade.TradeError) else type(error).__name__
            terminal=job['attempts']+1 >= (2 if isinstance(error,trade.TradeError) else 3)
            with store.box.connect() as db:
                db.execute("UPDATE market_ai_prices SET status=?,error=?,next_attempt=? WHERE fingerprint=?",
                           ('error' if terminal else 'pending',reason[:160],time.time()+60*2**job['attempts'],job['fingerprint']))
                if terminal:
                    control.review(db,post,-2,'AI 报价分析未完成：'+reason[:120])
                    db.execute("UPDATE marketplace_posts SET status='needs_review' WHERE source=? AND source_id=? AND status='pending_price'",(post['source'],post['source_id']))
    return count
