"""Collection incidents depend on real progress, not individual page errors."""
import json

import pytest

from pylon.outbox import Outbox
from pylon.tasks.marketplace import poll
from pylon.tasks.marketplace.store import Store


@pytest.fixture
def incident(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(poll.time, 'time', lambda: clock[0])
    return Store(Outbox(tmp_path / 'outbox.sqlite3')), clock


def alerts(store):
    with store.box.connect() as db:
        return [json.loads(row[0])['text'] for row in db.execute('SELECT payload FROM events')]


def post(store, number, complete=False):
    store.ingest('v2ex', [{'id': str(number), 'url': f'https://example.invalid/t/{number}',
        'title': '出售 iPhone 15 Pro', 'body_full': '售价 3000 元。' if complete else '',
        'body_complete': complete}])


def test_hour_boundary_restart_and_silent_recovery(incident):
    store, clock = incident
    post(store, 1)
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    clock[0] = 4599
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    assert alerts(store) == []
    clock[0] = 4600
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    assert len(alerts(store)) == 1
    assert '60 分钟' in alerts(store)[0]
    restarted = Store(Outbox(store.box.path))
    clock[0] = 8200
    poll.record_health(restarted, 'v2ex', ['fixture_timeout'])
    assert len(alerts(store)) == 1
    clock[0] += 1
    post(restarted, 1, complete=True)
    poll.record_health(restarted, 'v2ex', [])
    assert len(alerts(store)) == 1
    with store.box.connect() as db:
        row = db.execute('SELECT open,blocked_since FROM intel_health').fetchone()
        assert tuple(row) == (0, None)


@pytest.mark.parametrize('kind', ['new_post', 'body'])
def test_partial_collection_progress_does_not_alert_for_other_failed_pages(incident, kind):
    store, clock = incident
    post(store, 1)
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    clock[0] = 4600
    before = poll.collection_progress(store, 'v2ex')
    post(store, 2 if kind == 'new_post' else 1, complete=kind == 'body')
    poll.record_health(store, 'v2ex', ['another_page_timeout'], before=before)
    assert alerts(store) == []
    # A new incident starts only when subsequent collection stops progressing.
    clock[0] = 4700
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    clock[0] = 8299
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    assert alerts(store) == []
    clock[0] = 8300
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    assert len(alerts(store)) == 1


def test_analysis_timestamp_does_not_hide_a_stalled_collector(incident):
    store, clock = incident
    post(store, 1)
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    clock[0] = 4600
    with store.box.connect() as db:
        db.execute('UPDATE marketplace_posts SET updated_at=?', (clock[0],))
    poll.record_health(store, 'v2ex', ['fixture_timeout'])
    assert len(alerts(store)) == 1


def test_successful_empty_poll_is_silent_and_resets_outage(incident):
    store, clock = incident
    poll.record_health(store, 'v2ex', ['fixture_listing_timeout'])
    clock[0] = 4600
    poll.record_health(store, 'v2ex', [])
    clock[0] = 8200
    poll.record_health(store, 'v2ex', [])
    assert alerts(store) == []


def test_failed_listing_is_unfinished_work_before_any_post_is_collected(incident):
    store, clock = incident
    poll.record_health(store, 'v2ex', ['fixture_listing_timeout'])
    clock[0] = 4600
    poll.record_health(store, 'v2ex', ['fixture_listing_timeout'])
    assert len(alerts(store)) == 1
