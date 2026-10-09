from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pylon.shared import paths, state_json
from . import http, judge, policy, trade, trade_store
from .common import CHANNELS, clean, timestamp
from .sources import Page, parse_page
from .store import Store

SOURCE_CONFIG = paths.PROFILE_ROOT / 'config/linuxdo_sources.json'
LEGACY_STATE = paths.CRON_STATE_DIR / 'linuxdo_notice.json'


def source_config(path: Path | None = None) -> dict:
    defaults = {'version': 1, 'channels': list(CHANNELS), 'max_pages': 6,
                'fetch_timeout_seconds': 8, 'source_budget_seconds': 40}
    path = path or SOURCE_CONFIG
    config = json.loads(path.read_text()) if path.exists() else defaults
    if isinstance(config, dict) and config.get('version') == 2:
        defaults = defaults | {'version': 2, 'mode': 'helper', 'page_size': 100, 'recheck_limit': 100}
    if not isinstance(config, dict) or set(config) != set(defaults) or config['version'] != defaults['version']:
        raise ValueError('invalid Linux.do source configuration')
    if not isinstance(config['channels'], list) or not config['channels'] or any(c not in CHANNELS for c in config['channels']):
        raise ValueError('unknown Linux.do source')
    for key, bounds in [('max_pages', (1, 20)), ('fetch_timeout_seconds', (1, 15)), ('source_budget_seconds', (10, 120))]:
        if type(config[key]) is not int or not bounds[0] <= config[key] <= bounds[1]:
            raise ValueError('invalid source budget')
    if config['version'] == 2:
        if config['mode'] not in {'helper', 'channels'}:
            raise ValueError('unknown Linux.do source mode')
        for key, bounds in [('page_size', (1, 100)), ('recheck_limit', (0, 100))]:
            if type(config[key]) is not int or not bounds[0] <= config[key] <= bounds[1]:
                raise ValueError('invalid Helper page limit')
    return config


def _eligible_page(store: Store, channel: str, page: Page, floor: int) -> Page:
    with store.box.connect() as db:
        known = {row[0] for row in db.execute('SELECT message_id FROM linuxdo_observations WHERE channel=? AND message_id>=?',
                                            (channel, min(page.message_ids)))}
    return Page([post for post in page.observations if post.message_id > floor or post.message_id in known],
                page.message_ids, page.unparsed)


def collect_source(store: Store, channel: str, config: dict, *, getter=http.fetch) -> dict:
    checkpoint = store.checkpoint(channel) or {}
    deadline = time.monotonic() + config['source_budget_seconds']
    pages, changed = 0, 0

    def read(before=None):
        nonlocal pages
        url = f'https://t.me/s/{channel}' + (f'?before={before}' if before else '')
        page = parse_page(getter(url, timeout=config['fetch_timeout_seconds']), channel)
        pages += 1
        if not page.message_ids or not page.observations:
            raise http.FetchError('channel preview unavailable or format changed')
        return page

    try:
        head = read()
        newest, oldest = max(head.message_ids), min(head.message_ids)
        cursor = checkpoint.get('cursor')
        if cursor is None:
            checkpoint = {'cursor': newest, 'baseline': True, 'backfill_before': None, 'backfill_until': None}
            changed += store.save_page(channel, head, checkpoint, baseline=True)
        else:
            floor = checkpoint.get('backfill_until')
            if floor is None:
                floor = cursor
            before = oldest if oldest > cursor else checkpoint.get('backfill_before')
            checkpoint = {'cursor': max(cursor, newest), 'baseline': True,
                          'backfill_before': before, 'backfill_until': floor if before else None}
            changed += store.save_page(channel, _eligible_page(store, channel, head, cursor), checkpoint)
            while checkpoint.get('backfill_before') and pages < config['max_pages']:
                if deadline - time.monotonic() < config['fetch_timeout_seconds']:
                    break
                before = checkpoint['backfill_before']
                page = read(before)
                low = min(page.message_ids)
                if low >= before:
                    raise http.FetchError('channel pagination did not advance')
                floor = checkpoint['backfill_until']
                next_checkpoint = dict(checkpoint)
                if low <= floor:
                    next_checkpoint.update(backfill_before=None, backfill_until=None)
                else:
                    next_checkpoint['backfill_before'] = low
                changed += store.save_page(channel, _eligible_page(store, channel, page, floor), next_checkpoint)
                checkpoint = next_checkpoint
        store.source_result(channel)
        return {'channel': channel, 'pages': pages, 'changed': changed,
                'caught_up': checkpoint.get('backfill_before') is None, 'error': ''}
    except Exception as exc:
        reason = str(exc) if isinstance(exc, http.FetchError) else type(exc).__name__
        store.source_result(channel, reason)
        return {'channel': channel, 'pages': pages, 'changed': changed, 'caught_up': False, 'error': reason}


def collect(store: Store | None = None, *, config: dict | None = None, getter=http.fetch) -> dict:
    store = store or Store()
    store.import_legacy(state_json.load(LEGACY_STATE, default={}))
    config = config or source_config()
    if config.get('mode') == 'helper':
        from .helper_collect import collect as collect_helper
        return collect_helper(store, config)
    result = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(collect_source, store, channel, config, getter=getter) for channel in config['channels']]
        for future in as_completed(futures):
            result.append(future.result())
    store.sync_deliveries()
    return {'sources': sorted(result, key=lambda row: row['channel']), 'changed': sum(r['changed'] for r in result)}


def process_topic(store: Store, topic_id: str, config: dict, *, model: str,
                  request=judge.request, topic_fetch=http.topic_evidence,
                  official_fetch=http.official_evidence, trade_request=trade.request,
                  trade_config: dict | None = None) -> dict:
    try:
        snapshot = store.snapshot(topic_id)
        with store.box.connect() as db:
            fresh_required=bool(db.execute('SELECT 1 FROM linuxdo_meta WHERE key=?',('market_recheck:'+topic_id,)).fetchone())
        from .helper_store import covered_by_personal
        if covered_by_personal(snapshot):
            store.finish(snapshot, {'decision': 'skip', 'expires_at': ''})
            return {'topic_id': topic_id, 'decision': 'skip', 'event_id': None, 'error': ''}
        from pylon.tasks.marketplace.ai_prices import config as shared_prices
        trade_config = shared_prices() if trade_config is None else trade.validate_config(trade_config)
        if ((snapshot['expires_at'] and snapshot['expires_at'] <= time.time())
                or time.time() - snapshot['updated_at'] > 7 * 86400):
            store.finish(snapshot, {'decision': 'skip', 'expires_at': ''})
            return {'topic_id': topic_id, 'decision': 'expired'}
        # Expensive reads are retried separately from collection and never hold a write transaction.
        if fresh_required or time.time() - snapshot['body_attempted_at'] >= min(21600, 300 * 2 ** min(snapshot['attempts'], 6)):
            try:
                numbers = [e['post_number'] for e in snapshot['evidence'] if e['origin'] == 'telegram']
                evidence = topic_fetch(topic_id, numbers)
            except Exception:
                evidence = []
            if fresh_required:
                if not any(r.get('complete') and r.get('post_number')==1 for r in evidence):
                    store.defer(topic_id,'fresh_body_unavailable')
                    return {'topic_id':topic_id,'decision':'hold','error':'fresh_body_unavailable'}
                with store.box.connect() as db:db.execute('DELETE FROM linuxdo_meta WHERE key=?',('market_recheck:'+topic_id,))
            store.add_evidence(topic_id, evidence)
            snapshot = store.snapshot(topic_id)
            store.add_evidence(topic_id, official_fetch(snapshot), attempted=False)
            snapshot = store.snapshot(topic_id)
        from pylon.tasks.marketplace import pipeline as market
        from pylon.tasks.marketplace.linuxdo_bridge import owned_snapshot,transfer
        trade_snapshot=owned_snapshot(snapshot) if market.enabled() else snapshot
        trade_key = trade.cache_key(trade_snapshot, trade_config, model, focus=config)
        extracted = store.cached(trade_key)
        if extracted is None:
            extracted = trade.validate(trade_request(trade_snapshot), trade_snapshot)
            store.save_decision(trade_key, topic_id, extracted)
        else:
            extracted = trade.validate(extracted, trade_snapshot)
        if extracted['intent'] not in {'non_trade', 'cloud_offer'}:
            if market.enabled():
                return {'topic_id':topic_id} | transfer(store,snapshot,extracted)
            allowed = policy.allowed_categories(config)
            filtered = [offer for offer in extracted['offers']
                if ('B1' if offer['access'] == 'codex' else 'B2' if offer['access'] == 'claude_code' else 'A2') in allowed]
            if extracted['offers'] and not filtered:
                store.finish(snapshot, {'decision': 'skip', 'expires_at': ''})
                return {'topic_id': topic_id, 'decision': 'skip', 'event_id': None, 'error': ''}
            extracted = dict(extracted, offers=filtered)
            return {'topic_id': topic_id} | trade_store.finish(store, snapshot, extracted, trade_config,
                                                             run_id=os.environ.get('PYLON_RUN_ID', ''))
        key = judge.cache_key(snapshot, config, model)
        result = store.cached(key)
        if result is None:
            result = judge.validate_result(request(snapshot, config), snapshot, config)
            store.save_decision(key, topic_id, result)
        else:
            # Delivery may have completed since the first decision. Reapply all local gates.
            result = judge.validate_result(result, snapshot, config)
        message = judge.format_message(snapshot, result) if result['decision'] == 'push' else ''
        event_id = store.finish(snapshot, result, message=message, run_id=os.environ.get('PYLON_RUN_ID', ''))
        return {'topic_id': topic_id, 'decision': result['decision'], 'event_id': event_id, 'error': ''}
    except Exception as exc:
        store.defer(topic_id, type(exc).__name__)
        return {'topic_id': topic_id, 'decision': 'hold', 'error': type(exc).__name__}


def review(store: Store | None = None, *, config: dict | None = None, limit: int = 2,
           model: str | None = None, request=judge.request,
           topic_fetch=http.topic_evidence, official_fetch=http.official_evidence,
           sources: dict | None = None, trade_request=trade.request,
           trade_config: dict | None = None) -> dict:
    store = store or Store()
    sources = sources if sources is not None else source_config()
    from .helper import SOURCE
    with store.box.connect() as db:
        has_public = sources.get('mode') == 'helper' and db.execute('''SELECT 1
            FROM linuxdo_public_reviews r JOIN linuxdo_helper_messages m USING(message_id)
            WHERE m.route='following' AND r.status IN ('pending','hold') LIMIT 1''').fetchone()
        has_topics = db.execute('''SELECT 1 FROM linuxdo_topics t
            WHERE historical=0 AND status IN ('pending','hold') AND (?='' OR EXISTS(
                SELECT 1 FROM linuxdo_observations o WHERE o.topic_id=t.topic_id AND o.channel=?)) LIMIT 1''',
            (SOURCE if sources.get('mode') == 'helper' else '', SOURCE)).fetchone()
    if not has_public and not has_topics:
        store.sync_deliveries()
        return {'processed': 0, 'errors': 0, 'queued': 0}
    config = config or policy.load()
    from pylon.tasks.marketplace.ai_prices import config as shared_prices
    trade_config = shared_prices() if trade_config is None else trade.validate_config(trade_config)
    model = model or judge.model_name()
    trade_store.refresh_policy(store, trade_config, config, model)
    # Public-author intent and board value reviews share two model slots. Neither
    # queue must wait for the other queue's model request to finish before starting.
    topic_ids = store.claim(limit=1 if has_public else max(1, min(limit, 2)),
                            source=SOURCE if sources.get('mode') == 'helper' else '')
    if not topic_ids and not has_public:
        store.sync_deliveries()
        return {'processed': 0, 'errors': 0, 'queued': 0}
    summary = {'processed': 0, 'errors': 0, 'queued': 0}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(process_topic, store, topic_id, config, model=model, request=request,
                               topic_fetch=topic_fetch, official_fetch=official_fetch,
                               trade_request=trade_request, trade_config=trade_config): 'topic' for topic_id in topic_ids}
        if has_public:
            from . import public_review
            for _ in range(2 - len(topic_ids)):
                futures[pool.submit(public_review.review, store, limit=1, request=trade_request,
                                    config=trade_config, model=model, focus=config)] = 'public'
        for future in as_completed(futures):
            result = future.result()
            if futures[future] == 'topic':
                result = {'processed': 1, 'errors': int(bool(result.get('error'))),
                          'queued': int(bool(result.get('event_id')))}
            for key in summary:
                summary[key] += result[key]
    store.health('judge', bool(summary['errors']))
    store.sync_deliveries()
    return summary
