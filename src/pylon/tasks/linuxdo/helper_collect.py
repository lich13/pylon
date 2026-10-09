from __future__ import annotations

import asyncio
import time

from .common import digest
from . import following
from .helper import SOURCE, parse_message
from .helper_account import AuthorizationRequired, HelperAccount, RateLimited
from .helper_store import HelperStore
from .store import Store


async def collect_async(store: Store, config: dict, *, account_factory=HelperAccount) -> dict:
    followed, following_error = following.available()
    helper = HelperStore(store, followed=followed.usernames if followed else None)
    checkpoint = store.checkpoint(SOURCE) or {}
    result = {'source': SOURCE, 'changed': 0, 'queued': 0, 'pages': 0, 'error': '', 'caught_up': False}
    result['following'] = (followed.summary() if followed else {'configured': False}) | {'error': following_error}
    if checkpoint.get('retry_at', 0) > time.time():
        return result | {'deferred': True}
    deadline = time.monotonic() + config['source_budget_seconds']
    try:
        async with account_factory() as account:
            identity = digest({key: account.identity[key] for key in ('account_id', 'bot_id')})
            if checkpoint.get('identity') not in {None, identity}:
                raise AuthorizationRequired('Helper source identity changed')

            async def read(**kwargs):
                timeout = min(config['fetch_timeout_seconds'], deadline - time.monotonic())
                if timeout <= 0:
                    raise TimeoutError('Helper collection budget exhausted')
                return await asyncio.wait_for(account.page(**kwargs), timeout)

            if not checkpoint.get('baseline'):
                latest = await read(limit=1, recent=True)
                newest = max((message['message_id'] for message in latest), default=0)
                checkpoint = {'identity': identity, 'baseline': True, 'baseline_id': newest, 'cursor': newest, 'retry_at': 0}
                helper.save_page([], checkpoint)
                helper.result(checkpoint)
                return result | {'baseline': True, 'caught_up': True}
            reparsed = helper.reparse_pending()
            result['changed'] += reparsed['collected']
            result['queued'] += reparsed['queued']
            for _ in range(config['max_pages']):
                if deadline - time.monotonic() < 1:
                    break
                messages = await read(after=checkpoint['cursor'], limit=config['page_size'])
                result['pages'] += 1
                if not messages:
                    result['caught_up'] = True
                    break
                ids = [row['message_id'] for row in messages]
                if any(number <= checkpoint['cursor'] for number in ids) or ids != sorted(set(ids)):
                    raise ValueError('Helper page did not advance in order')
                checkpoint = dict(checkpoint, cursor=ids[-1], retry_at=0)
                counts = helper.save_page(messages, checkpoint)
                result['changed'] += counts['collected']
                result['queued'] += counts['queued']
                if len(messages) < config['page_size']:
                    result['caught_up'] = True
                    break
            # Only already committed messages may be revisited. This cannot skip a gap.
            if config['recheck_limit'] and deadline - time.monotonic() >= 1:
                messages = await read(limit=config['recheck_limit'], recent=True)
                messages = [row for row in messages if checkpoint['baseline_id'] < row['message_id'] <= checkpoint['cursor']]
                counts = helper.save_page(messages, checkpoint)
                result['changed'] += counts['collected']
                result['queued'] += counts['queued']
            helper.result(checkpoint)
    except Exception as exc:
        # Re-read the committed cursor; a failed transaction must not acknowledge its page.
        checkpoint = store.checkpoint(SOURCE) or {}
        with store.box.connect() as db:
            row = db.execute('SELECT failures FROM linuxdo_sources WHERE channel=?', (SOURCE,)).fetchone()
        delay = exc.seconds if isinstance(exc, RateLimited) else min(1800, 60 * 2 ** min(row[0] if row else 0, 5))
        helper.result(checkpoint, error=type(exc).__name__, retry_at=time.time() + delay,
                      authorization=isinstance(exc, AuthorizationRequired))
        result['error'] = type(exc).__name__
    store.sync_deliveries()
    return result


def collect(store: Store, config: dict, *, account_factory=HelperAccount) -> dict:
    return asyncio.run(collect_async(store, config, account_factory=account_factory))


async def inspect_account(*, limit: int = 100, account_factory=HelperAccount) -> dict:
    counts, signatures = {}, {}
    followed, following_error = following.available()
    async with account_factory() as account:
        before = await account.read_marker()
        messages = await account.page(limit=limit, recent=True)
        after = await account.read_marker()
    for raw in messages:
        notice = parse_message(raw, followed=followed.usernames if followed else None)
        key = notice.route + ':' + notice.kind
        counts[key] = counts.get(key, 0) + 1
        # Only counts and opaque hashes leave this function, never message contents.
        if notice.route == 'unknown':
            shape = (len(raw.get('links', [])), len(raw.get('buttons', [])), bool(notice.topic_id))
            signatures[str(shape)] = signatures.get(str(shape), 0) + 1
    return {'authorized': True, 'sample_count': len(messages), 'routes': counts, 'unknown_shapes': signatures,
            'following': (followed.summary() if followed else {'configured': False}) | {'error': following_error},
            'read_marker_unchanged': before == after}
