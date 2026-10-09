"""One policy and one durable notification per source post."""
from __future__ import annotations

import json
import re
import time
import os
import sqlite3
from . import policy, wanted
from .classifier import VERSION as CLASSIFIER_VERSION, classify_listing
from .store import Store, key
from .taxonomy import CATEGORIES
from pylon import events

LABELS = {'v2ex':'V2EX','chiphell':'CHH','nga':'NGA'}


def enabled() -> bool:
    return policy.load()['enabled'] or wanted.load()['enabled']


def is_v2ex_trade(post) -> bool:
    # rough_type also includes subscription carpools/promotions outside this
    # node. Only move the actual secondhand node out of the original monitor.
    return '二手交易' in post.tags


def ingest(source: str, posts: list[dict]) -> int:
    return Store().ingest(source, posts)


def _quote_matches(items: list[dict], complete: bool, quotes: list) -> list[dict]:
    from pylon.tasks.quotes.matching import select_match
    from pylon.tasks.quotes.validation import valid_match
    if not complete:
        return []
    result = []
    for item in items:
        if (item['status']!='available' or item['category'].startswith('virtual.') or item['attributes'].get('brand_confirmed') is False
                or item['attributes'].get('bundle') or item.get('price') is None):
            continue
        match = select_match(quotes, '', item['text'], assume_sale=True)
        if valid_match(match) and match['sale_price'] == item['price']:
            result.append(match)
    return result


def format_message(post: dict, items: list[dict], matches: list[dict]) -> str:
    from pylon.tasks.quotes.arbitrage import format_contact_note
    if not items:
        return ''
    label = LABELS[post['source']]
    title = re.sub(r'[\[\]\r\n]', ' ', post['title']).strip()
    lines = [f'## {label} 二手交易' + (' · 重点比价' if matches else ''), f'[{title}]({post["url"]})']
    if not post.get('body_complete'):
        lines.append('正文暂不可读，仅依据标题；价格与交易条件请查看原帖。')
    if any(item['attributes'].get('bundle_only') for item in items):
        totals = {item['attributes'].get('bundle_price') for item in items if item['attributes'].get('bundle_price') is not None}
        lines.append(f'仅打包出售，合计 {next(iter(totals))} 元。' if len(totals)==1 else '仅打包出售，合计价格未标。')
    for item in items[:10]:
        price = f"{item['price']} 元" if item['price'] is not None else '价格未标'
        attrs = item['attributes']
        trade = ('仅北京可用' if attrs.get('regional_service') and attrs['beijing'] else
                 '可邮寄' if attrs['shipping'] else '北京自提' if attrs['pickup_only'] and attrs['beijing'] else '交易方式见原帖')
        subject = re.sub(r'https?://\S+','',item['text'].split('\n',1)[0])
        subject = re.sub(r'[\[\]`*_]','',subject).strip()[:100]
        if item['status']=='buy':
            price = f"求购出价 {item['price']} 元" if item['price'] is not None else '求购出价未标'
            lines.append(f"- 【求购】{subject or CATEGORIES[item['category']]}｜{price}｜{trade}")
        else:
            lines.append(f"- {subject or CATEGORIES[item['category']]}｜{price}｜{attrs['condition']}｜{trade}")
        lines.append(f"  分类：{CATEGORIES[item['category']]}")
    if len(items)>10:
        lines.append(f'另有 {len(items)-10} 件命中商品，详见原帖。')
    for match in matches:
        lines.append(f"比价：{match['product_name']}，售价 {match['sale_price']}，报价 {match['quote_price']}，差价 {match['profit']} 元。")
        lines.append(f"依据：{match['condition_evidence']}；{match['price_evidence']}")
    contact = format_contact_note(post['title']+'\n'+post['body'])
    if contact:
        lines.append(contact)
    return '\n'.join(lines)


def process(store: Store | None = None, *, current_policy: dict | None = None,
            wanted_policy: dict | None = None, limit: int = 30, source: str | None = None) -> int:
    from pylon.tasks.quotes import arbitrage, health
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
            parsed_items = json.loads(post['items'])
            if any(item['attributes'].get('classifier_version', CLASSIFIER_VERSION) != CLASSIFIER_VERSION
                   for item in parsed_items):
                parsed_items = classify_listing(post['title'], post['body'], post['source'], bool(post['body_complete']))
                db.execute('UPDATE marketplace_posts SET items=? WHERE source=? AND source_id=?',
                           (json.dumps(parsed_items, ensure_ascii=False), post['source'], post['source_id']))
            items = policy.evaluate(parsed_items,current_policy) + wanted.evaluate(parsed_items,wanted_policy)
            matches = _quote_matches(items,bool(post['body_complete']),quotes)
            text = format_message(post,items,matches)
            event_id = None
            if text:
                event_id = store.box.enqueue(profile='webops',job_id='marketplace-poll',kind='text',
                    payload={'text':text},key=key(post['source'],post['source_id']),run_id=run_id,connection=db)
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
            db.execute('UPDATE marketplace_posts SET status=?,event_id=?,policy_version=?,wanted_policy_version=?,updated_at=? WHERE source=? AND source_id=?',
                       ('queued' if event_id else 'skipped',event_id,current_policy['version'],wanted_policy['version'],time.time(),post['source'],post['source_id']))
    return count
