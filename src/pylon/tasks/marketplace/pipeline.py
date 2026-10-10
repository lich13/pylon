"""One policy and one durable notification per source post."""
from __future__ import annotations

import json
import re
import time
import os
import sqlite3
from . import policy, wanted, control
from .classifier import VERSION as CLASSIFIER_VERSION, classify_listing
from .store import Store, key
from .taxonomy import CATEGORIES
from pylon import events

LABELS = {'v2ex':'V2EX','chiphell':'CHH','nga':'NGA','linuxdo':'Linux.do'}


def enabled() -> bool:
    return policy.load()['enabled'] or wanted.load()['enabled']


def is_v2ex_trade(post) -> bool:
    # Nodes are a discovery hint, never a way around the shared trade policy.
    title = getattr(post,'core_title','') or post.title
    clear=re.compile(r'出售|转让|出闲置|代充|出租|合租|拼车|车找人|人找车|车位|发车|求购|求收|\[收\]|【收】')
    return ('二手交易' in post.tags or getattr(post,'rough_type','') == 'secondary_market'
            or bool(clear.search(title)) or bool(getattr(post,'body_complete',False) and clear.search(post.body_full)))


def ingest(source: str, posts: list[dict]) -> int:
    count=Store().ingest(source, posts)
    if source=='v2ex':
        from pylon.tasks.intelligence.pipeline import capture_v2ex_trade
        for post in posts:capture_v2ex_trade(post)
    return count


def _quote_matches(items: list[dict], complete: bool, quotes: list) -> list[dict]:
    from pylon.tasks.quotes.matching import select_match
    from pylon.tasks.quotes.validation import valid_match
    if not complete:
        return []
    result = []
    for item in items:
        if (item['status']!='available' or item['category'].startswith('virtual.') or item['attributes'].get('brand_confirmed') is False
                or item['attributes'].get('currency','CNY')!='CNY'
                or item['attributes'].get('bundle') or item.get('price') is None):
            continue
        match = select_match(quotes, '', item['text'], assume_sale=True)
        if valid_match(match) and match['sale_price'] == item['price']:
            result.append(match)
    return result


def format_message(post: dict, items: list[dict], matches: list[dict]) -> str:
    from pylon.tasks.quotes.arbitrage import format_contact_note
    from .enrich import links
    if not items:
        return ''
    label = LABELS[post['source']]
    title = re.sub(r'[\[\]\r\n]', ' ', post['title']).strip()
    lines = [f'## {label} 二手交易' + (' · 重点比价' if matches else ''), f'[{title}]({post["url"]})']
    if not post.get('body_complete'):
        from .content_quality import evidence_scope
        lines.append('依据 Helper 摘要；未说明的条件请查看原帖。' if evidence_scope(post)=='helper_excerpt' else '正文暂不可读，仅依据标题；价格与交易条件请查看原帖。')
    if any(item['attributes'].get('bundle_only') for item in items):
        totals = {item['attributes'].get('bundle_price') for item in items if item['attributes'].get('bundle_price') is not None}
        lines.append(f'仅打包出售，合计 {next(iter(totals))} 元。' if len(totals)==1 else '仅打包出售，合计价格未标。')
    shared_links=[]
    for item in items[:10]:
        attrs = item['attributes']
        currency='元' if attrs.get('currency','CNY')=='CNY' else attrs['currency']
        price = (f"{item['price']} {currency}"+('/'+attrs['billing_unit'] if attrs.get('billing_unit') else '') if item['price'] is not None else
                 '正文未标价，见原帖商品链接' if attrs.get('external_price') else
                 '单件价格未拆分' if attrs.get('price_status') == 'bundled' else
                 '售价有歧义，见原帖' if attrs.get('price_status') == 'ambiguous' else
                 '未给出售价（仅有参考价）' if attrs.get('price_reason') == 'reference_only' else '价格未标')
        ai=attrs.get('ai_price_result')
        if ai and ai.get('total'):
            offer=ai['offer']
            price=f"实付 {ai['total']} {offer['currency']} / {offer['term']['value']}{'个月' if offer['term_unit']=='month' else '天'}"
        trade = ('仅北京可用' if attrs.get('regional_service') and attrs['beijing'] else
                 '可邮寄' if attrs['shipping'] else '北京自提' if attrs['pickup_only'] and attrs['beijing'] else '交易方式见原帖')
        subject = re.sub(r'https?://\S+','',item.get('name') or item['text'].split('\n',1)[0])
        subject = re.sub(r'[\[\]`*_]','',subject).strip()[:100]
        if item['status']=='buy':
            price = f"求购出价 {item['price']} {currency}" if item['price'] is not None else '求购出价未标'
            lines.append(f"- 【求购】{subject or CATEGORIES[item['category']]}｜{price}｜{trade}")
        else:
            lines.append(f"- {subject or CATEGORIES[item['category']]}｜{price}｜{attrs['condition']}｜{trade}")
        lines.append(f"  分类：{CATEGORIES[item['category']]}")
        if ai and ai.get('comparison'):
            lines.append('  '+ai['comparison'])
            for field,label in (('personal_quota','个人额度'),('restrictions','限制')):
                if ai['offer'][field]['value']:lines.append('  '+label+'：'+ai['offer'][field]['value'])
        facts=attrs.get('facts',{})
        if facts.get('ram_gb') and facts.get('storage_gb'):
            lines.append(f"  规格：{facts['ram_gb']}+{facts['storage_gb']}G")
        for url in dict.fromkeys(attrs.get('links',[]) or links(item['text'])):
            lines.append(f'  商品链接：{url}')
        shared_links.extend(attrs.get('shared_links',[]))
    if len(items)>10:
        lines.append(f'另有 {len(items)-10} 件命中商品，详见原帖。')
    for match in matches:
        lines.append(f"比价：{match['product_name']}，售价 {match['sale_price']}，报价 {match['quote_price']}，差价 {match['profit']} 元。")
        lines.append(f"依据：{match['condition_evidence']}；{match['price_evidence']}")
    # Summary-document links are part of the original listing, not decoration.
    from urllib.parse import urlsplit
    for url in links(post['body']):
        if (urlsplit(url).hostname or '').lower() in {'docs.qq.com','www.kdocs.cn','kdocs.cn'}:
            shared_links.append(url)
    for url in dict.fromkeys(shared_links):
        if not any(url in line for line in lines[2:]):
            lines.append('详情／交易入口：'+url)
    contact = format_contact_note(post['title']+'\n'+post['body'])
    if contact:
        lines.append(contact)
    return '\n'.join(lines)


def process(store: Store | None = None, *, current_policy: dict | None = None,
            wanted_policy: dict | None = None, limit: int = 30, source: str | None = None) -> int:
    from pylon.tasks.quotes import arbitrage, health
    from .taxonomy import refresh_extensions
    refresh_extensions()
    current_policy = current_policy or policy.load()
    wanted_policy = wanted.load() if wanted_policy is None else wanted_policy
    if not current_policy['enabled'] and not wanted_policy['enabled']:
        return 0
    if source is not None and source not in LABELS:
        raise ValueError('unknown marketplace source')
    store = store or Store()
    run_id = events.context()['run_id']
    quotes = []
    if health.inspect(arbitrage.DB_PATH)['available']:
        try:
            with arbitrage.connect() as db:
                quotes = arbitrage.load_quotes(db)
        except (sqlite3.Error,OSError):
            # Availability may change between the health read and this read.
            # Category notifications remain independent of the price database.
            quotes = []
    count = 0
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for row in db.execute("SELECT * FROM marketplace_posts WHERE history=0 AND status='ready' AND (? IS NULL OR source=?) ORDER BY first_seen LIMIT ?",(source,source,limit)).fetchall():
            post = dict(row)
            from .content_quality import contamination
            bad_body=contamination(post['source'],post['title'],post['body'])
            if bad_body:
                post['body_complete']=0
                db.execute('UPDATE marketplace_posts SET body_complete=0,error=? WHERE source=? AND source_id=?',
                           (bad_body,post['source'],post['source_id']))
            parsed_items = json.loads(post['items'])
            if bad_body or any(item['attributes'].get('classifier_version', CLASSIFIER_VERSION) != CLASSIFIER_VERSION
                   for item in parsed_items):
                from .content_quality import evidence_scope
                parsed_items = classify_listing(post['title'], post['body'], post['source'], bool(post['body_complete']),platform_category=post.get('platform_category',''),evidence_scope=evidence_scope(post))
                db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=?',
                           (json.dumps(parsed_items, ensure_ascii=False), post['source'], post['source_id']))
            from . import ai_prices
            price_originals = list(parsed_items)
            parsed_items,price_waiting=ai_prices.prepare(db,post,parsed_items)
            pending_price_items = [i for i in price_originals if ai_prices.is_ai(i)] if price_waiting else []
            if price_waiting and not parsed_items:
                db.execute("UPDATE marketplace_posts SET status='pending_price' WHERE source=? AND source_id=?",
                           (post['source'],post['source_id']))
                control.audit(db,post,[],status='pending_price',policy_version=current_policy['version'])
                continue
            for index,item in enumerate(parsed_items):item['index']=index
            decisions=[]
            for item in parsed_items:
                decision,reasons=policy.decision(item,current_policy)
                if item['status']=='buy':
                    eligible=wanted.evaluate([item],{**wanted_policy,'filters':{}})
                    decision,reasons=policy.attribute_decision(item,wanted_policy) if eligible else ('exclude',['不符合独立求购目标'])
                decisions.append({'index':item['index'],'category':item['category'],'state':decision,'reasons':reasons})
                if decision=='review':
                    item['attributes']['required_missing']=[r for r in reasons if r in __import__('pylon.tasks.marketplace.subjects',fromlist=['FIELDS']).FIELDS]
                    control.review(db,post,item['index'],'、'.join(reasons))
            db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=?',(json.dumps(parsed_items,ensure_ascii=False),post['source'],post['source_id']))
            post['items']=json.dumps(parsed_items,ensure_ascii=False)
            from .enrich import prepare
            parsed_items = prepare(db,post,parsed_items)
            if parsed_items is None:
                status=db.execute('SELECT status FROM marketplace_posts WHERE source=? AND source_id=?',(post['source'],post['source_id'])).fetchone()[0]
                control.audit(db,post,decisions,status=status,policy_version=current_policy['version'])
                continue
            # A semantic correction may discover an AI subscription that the
            # local classifier did not recognize. It still needs the same
            # evidence-backed price gate as a locally recognized offer.
            corrected_items = list(parsed_items)
            parsed_items, corrected_waiting = ai_prices.prepare(db,post,parsed_items)
            if corrected_waiting:
                pending_price_items = [i for i in corrected_items if ai_prices.is_ai(i)]
                price_waiting = True
            for item in pending_price_items:
                item['attributes']['ai_price_result'] = {'decision':'hold','reason':'AI 报价条件正在独立核验'}
            original_indices={d['index'] for d in decisions}
            # Keep unresolved source items while a partial notification proceeds.
            existing=json.loads(post['items'])
            by_index={i['index']:i for i in existing}
            by_index.update({i['index']:i for i in parsed_items})
            persisted=list(by_index.values()) if len(original_indices)>len(parsed_items) else parsed_items
            persisted = [i for i in persisted if not (price_waiting and ai_prices.is_ai(i))] + pending_price_items
            for index,item in enumerate(persisted):item['index']=index
            db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=?',
                       (json.dumps(persisted,ensure_ascii=False),post['source'],post['source_id']))
            decisions=[]
            for item in persisted:
                state,reasons=policy.decision(item,current_policy)
                if item['status']=='buy':
                    eligible=wanted.evaluate([item],{**wanted_policy,'filters':{}})
                    state,reasons=policy.attribute_decision(item,wanted_policy) if eligible else ('exclude',['不符合独立求购目标'])
                decisions.append({'index':item['index'],'category':item['category'],'state':state,'reasons':reasons})
                if state=='review':control.review(db,post,item['index'],'、'.join(reasons))
                else:db.execute("UPDATE market_reviews SET status='resolved' WHERE source=? AND source_id=? AND item_index=?",(post['source'],post['source_id'],item['index']))
            items = policy.evaluate(parsed_items,current_policy) + wanted.evaluate(parsed_items,wanted_policy)
            items,price_claims=ai_prices.delivery(db,post,items)
            for item in persisted:
                reason=item['attributes'].get('delivery_excluded') or item['attributes'].get('delivery_review')
                if reason:
                    for decision in decisions:
                        if decision['index']==item['index']:
                            decision.update(state='review' if item['attributes'].get('delivery_review') else 'exclude',reasons=[reason])
            db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=?',
                       (json.dumps(persisted,ensure_ascii=False),post['source'],post['source_id']))
            matches = _quote_matches(items,bool(post['body_complete']),quotes)
            text = format_message(post,items,matches)
            event_id = None
            if text:
                event_key=key(post['source'],post['source_id'])
                if price_claims:event_key+=':'+control.digest([c[1] for c in price_claims])[:24]
                if post['source']=='linuxdo':
                    context=json.loads(post.get('source_context') or '{}')
                    event_key+=':helper:'+control.digest([post['title'],post['body'],context.get('snapshot',{}).get('fingerprint','')])[:24]
                payload={'text':text}
                deadlines=[i['attributes']['ai_price_result'].get('expires_at') for i in items if i['attributes'].get('ai_price_result')]
                deadlines=[v for v in deadlines if v]
                if price_claims:deadlines.append(post['first_seen']+7*86400)
                if deadlines:payload['expires_at']=min(deadlines)
                event_id = store.box.enqueue(profile='webops',job_id='marketplace-poll',kind='text',
                    payload=payload,key=event_key,run_id=run_id,connection=db)
                ai_prices.acknowledge(db,price_claims,event_id)
                if matches:
                    # Reuse the existing Bark target, persist within the same transaction.
                    from pylon.shared.bark import load_device_key, build_payload, DEFAULT_API_BASE
                    device = load_device_key()
                    if device:
                        match = max(matches,key=lambda m:m['profit'])
                        burst_count, interval = arbitrage.bark_burst_settings()
                        for number in range(burst_count):
                            body = build_payload(title=f"重点比价：差价 {match['profit']} 元",body=text,
                                                 url=post['url'],level='critical',volume=10,group='electronics-arbitrage',device_key=device)
                            store.box.enqueue(profile='webops',job_id='marketplace-poll',kind='bark',
                                payload={'body':body,'api_base':os.environ.get('BARK_API_BASE',DEFAULT_API_BASE)},
                                key=key(post['source'],post['source_id'])+f':{number}',delay=number*interval,run_id=run_id,connection=db)
                count += 1
            state='queued' if event_id else 'pending_price' if price_waiting else 'needs_review' if any(d['state']=='review' for d in decisions) else 'skipped'
            retained_event=event_id or post.get('event_id')
            control.audit(db,post,decisions,status=state,event_id=retained_event,policy_version=current_policy['version'])
            db.execute('UPDATE marketplace_posts SET status=?,event_id=?,policy_version=?,wanted_policy_version=?,updated_at=? WHERE source=? AND source_id=?',
                       (state,retained_event,current_policy['version'],wanted_policy['version'],time.time(),post['source'],post['source_id']))
    return count
