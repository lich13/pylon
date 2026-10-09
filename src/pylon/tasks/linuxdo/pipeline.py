from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pylon.shared import paths, state_json
from . import http, judge, policy
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
    if not isinstance(config, dict) or set(config) != set(defaults) or config['version'] != 1:
        raise ValueError('invalid Linux.do source configuration')
    if not isinstance(config['channels'], list) or not config['channels'] or any(c not in CHANNELS for c in config['channels']):
        raise ValueError('unknown Linux.do source')
    for key, bounds in [('max_pages', (1, 20)), ('fetch_timeout_seconds', (1, 15)), ('source_budget_seconds', (10, 120))]:
        if type(config[key]) is not int or not bounds[0] <= config[key] <= bounds[1]:
            raise ValueError('invalid source budget')
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
    result = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(collect_source, store, channel, config, getter=getter) for channel in config['channels']]
        for future in as_completed(futures):
            result.append(future.result())
    store.sync_deliveries()
    return {'sources': sorted(result, key=lambda row: row['channel']), 'changed': sum(r['changed'] for r in result)}


def process_topic(store: Store, topic_id: str, config: dict, *, model: str,
                  request=judge.request, topic_fetch=http.topic_evidence,
                  official_fetch=http.official_evidence) -> dict:
    try:
        snapshot = store.snapshot(topic_id)
        if ((snapshot['expires_at'] and snapshot['expires_at'] <= time.time())
                or time.time() - snapshot['updated_at'] > 7 * 86400):
            store.finish(snapshot, {'decision': 'skip', 'expires_at': ''})
            return {'topic_id': topic_id, 'decision': 'expired'}
        # Expensive reads are retried separately from collection and never hold a write transaction.
        if time.time() - snapshot['body_attempted_at'] >= min(21600, 300 * 2 ** min(snapshot['attempts'], 6)):
            try:
                numbers = [e['post_number'] for e in snapshot['evidence'] if e['origin'] == 'telegram']
                evidence = topic_fetch(topic_id, numbers)
            except Exception:
                evidence = []
            store.add_evidence(topic_id, evidence)
            snapshot = store.snapshot(topic_id)
            store.add_evidence(topic_id, official_fetch(snapshot), attempted=False)
            snapshot = store.snapshot(topic_id)
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


def review(store: Store | None = None, *, config: dict | None = None, limit: int = 4,
           model: str | None = None, request=judge.request,
           topic_fetch=http.topic_evidence, official_fetch=http.official_evidence) -> dict:
    store = store or Store()
    config = config or policy.load()
    topic_ids = store.claim(limit=max(1, min(limit, 20)))
    if not topic_ids:
        store.sync_deliveries()
        return {'processed': 0, 'errors': 0, 'queued': 0}
    model = model or judge.model_name()
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(process_topic, store, topic_id, config, model=model, request=request,
                               topic_fetch=topic_fetch, official_fetch=official_fetch) for topic_id in topic_ids]
        for future in as_completed(futures):
            results.append(future.result())
    errors = sum(bool(result.get('error')) for result in results)
    store.health('judge', bool(errors))
    store.sync_deliveries()
    return {'processed': len(results), 'errors': errors, 'queued': sum(bool(r.get('event_id')) for r in results)}
