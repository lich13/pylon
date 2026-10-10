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
    # Only the authenticated Helper feed is an acquisition source. Old channel
    # configurations remain readable, but cannot reactivate a second collector.
    store = store or Store()
    store.import_legacy(state_json.load(LEGACY_STATE, default={}))
    config = {'page_size':100,'recheck_limit':100,**(config or source_config()), 'mode': 'helper'}
    from .helper_collect import collect as collect_helper
    return collect_helper(store, config)


def process_topic(store: Store, topic_id: str, config: dict | None = None, **unused) -> dict:
    from pylon.tasks.intelligence.pipeline import capture_linuxdo
    from .helper_store import covered_by_personal
    snapshot = store.snapshot(topic_id)
    captured = capture_linuxdo(store, snapshot)
    if covered_by_personal(snapshot):
        store.finish(snapshot, {'decision':'skip','expires_at':''})
        with store.box.connect() as db:
            db.execute("""UPDATE intel_items SET status='excluded',lease_until=0,error='',phase='personal',result=?
                WHERE source='linuxdo' AND source_id=?""",
                (json.dumps({'decision':'skip','categories':[],'facts':[],'reason':'同一帖子已通过个人提醒通知'},ensure_ascii=False),topic_id))
        return {'topic_id':topic_id,'decision':'skip','error':''}
    with store.box.connect() as db:
        db.execute("DELETE FROM linuxdo_meta WHERE key=?", ('market_recheck:'+topic_id,))
        db.execute("UPDATE linuxdo_topics SET status='intelligence',lease_until=0,error='',next_attempt=0 WHERE topic_id=? AND version=?",
                   (topic_id,snapshot['version']))
    return {'topic_id':topic_id,'decision':'intelligence','captured':captured,'error':''}


def review(store: Store | None = None, *, limit: int = 100, **unused) -> dict:
    store = store or Store()
    from .helper import SOURCE
    from .public_review import stage
    public = stage(store, limit=100)
    topic_ids = store.claim(limit=max(1,min(100,limit)),source=SOURCE)
    results = [process_topic(store,topic_id) for topic_id in topic_ids]
    store.sync_deliveries()
    return {'processed':len(results)+public,'errors':0,'queued':0,
            'captured':sum(bool(r.get('captured')) for r in results)}
