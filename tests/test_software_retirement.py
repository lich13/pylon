import time
from unittest.mock import Mock

import pytest

from pylon import bot, command_catalog, commands, software_updates
from pylon.outbox import Outbox
from pylon.software_retirement import RETIRED_MESSAGE, retire


def _review(box, review_id, *, status='pending', detail='legacy detail'):
    box.upsert_software_review({
        'id': review_id,
        'target': 'apt:sosreport',
        'package_name': 'sosreport',
        'installed_version': '4.8.0',
        'candidate_version': '4.8.1',
        'source': 'Ubuntu APT',
        'risk': 'service',
        'fingerprint': 'fingerprint-' + review_id,
        'profile': 'default',
        'status': status,
        'detail': detail,
    })
    return box.update_software_review(review_id, selected=1, available=1)


def _event(box, *, key, job_id, payload, status=None, receipt=None):
    event_id = box.enqueue(profile='default', job_id=job_id, kind='text', payload=payload, key=key)
    if status is not None or receipt is not None:
        box.finish(event_id, status or 'pending', receipt=receipt)
    return event_id


def test_retire_clears_review_selection_and_suppresses_only_unsent_legacy_events(tmp_path):
    box = Outbox(tmp_path / 'outbox.sqlite3')
    first = _review(box, 'legacy-review-1', status='failed')
    second = _review(box, 'legacy-review-2', status='uncertain', detail='待核对')
    box.put('software_inventory', {'checked_at': 123, 'rows': []})

    events = {
        'maintenance': _event(box, key='maintenance', job_id='software-maintenance', payload={'text': 'old'}),
        'review': _event(box, key='review', job_id='software-update-review', payload={'text': 'old'}),
        'card': _event(box, key='card', job_id='bot', payload={
            'text': 'old card',
            'reply_markup': {'inline_keyboard': [[{'text': '旧按钮', 'callback_data': 'su1|skip|legacy'}]]},
        }),
        'inventory': _event(box, key='inventory', job_id='bot', payload={'text': '腾讯云软件检查（北京时间 10-13 09:00）'}),
        'sent': _event(box, key='sent', job_id='software-maintenance', payload={'text': 'already delivered'},
                       status='pending', receipt=[{'message_id': 7, 'chat_id': '123'}]),
        'sending': _event(box, key='sending', job_id='software-update-review', payload={'text': 'in flight'}, status='sending'),
        'uncertain': _event(box, key='uncertain', job_id='software-update-review', payload={'text': 'uncertain'}, status='uncertain'),
        'business': _event(box, key='business', job_id='marketplace-review', payload={'text': 'keep'}),
    }

    with box.connect() as db:
        now = time.time()
        db.execute('INSERT INTO runs(id,job_id,started_at,status,detail) VALUES(?,?,?,?,?)',
                   ('historical-run', 'software-update-review', now, 'ok', 'keep history'))
        db.execute('INSERT INTO software_update_batches(id,profile,items,status,request_id,created_at,updated_at) '
                   'VALUES(?,?,?,?,?,?,?)',
                   ('historical-batch', 'default', '[]', 'updated', 'legacy-request', now, now))

    assert retire(box) == 4

    for review in (first, second):
        row = box.software_review(review['id'])
        assert row['available'] == 0 and row['selected'] == 0
        assert row['status'] == review['status'] and row['detail'] == review['detail']
    with box.connect() as db:
        rows = {row['id']: dict(row) for row in db.execute('SELECT id,status,receipt FROM events')}
        assert db.execute('SELECT id FROM runs WHERE id=?', ('historical-run',)).fetchone()
        assert db.execute('SELECT id FROM software_update_batches WHERE id=?', ('historical-batch',)).fetchone()
    assert rows[events['maintenance']]['status'] == 'suppressed'
    assert rows[events['review']]['status'] == 'suppressed'
    assert rows[events['card']]['status'] == 'suppressed'
    assert rows[events['inventory']]['status'] == 'suppressed'
    assert rows[events['sent']]['status'] == 'pending' and rows[events['sent']]['receipt']
    assert rows[events['sending']]['status'] == 'sending'
    assert rows[events['uncertain']]['status'] == 'uncertain'
    assert rows[events['business']]['status'] == 'pending'
    assert box.get('software_inventory') == {'checked_at': 123, 'rows': []}

    assert retire(box) == 0


def test_updates_menu_is_retired_but_native_update_entries_remain(monkeypatch):
    commands_list = {row['command'] for row in command_catalog.menu_commands()}
    assert 'updates' not in commands_list
    assert {'update_codex', 'update_grok'} <= commands_list
    inventory = Mock(side_effect=AssertionError('retired command must not scan inventory'))
    monkeypatch.setattr(software_updates, 'inventory_payload', inventory)
    assert commands.handle('default', '/updates') == RETIRED_MESSAGE
    assert commands.handle('default', '/updates all') == RETIRED_MESSAGE
    assert command_catalog.help_text('updates') == RETIRED_MESSAGE
    inventory.assert_not_called()


def test_retired_inventory_helpers_remain_noop_compatible():
    assert software_updates.inventory_payload('default') == {'text': RETIRED_MESSAGE, 'reply_markup': None}
    assert software_updates.inventory_text(full=True) == RETIRED_MESSAGE
    assert software_updates.publish_candidates({'apt': {'packages': [{'name': 'sosreport'}]}}) == 0


def test_retired_su_callbacks_remain_owner_only_and_are_side_effect_free(monkeypatch):
    monkeypatch.setattr(commands, 'profile_env', lambda _: {
        'TELEGRAM_HOME_CHANNEL': '123', 'TELEGRAM_BOT_TOKEN': 'fixture-token'})
    payload = {
        'id': 'callback-legacy',
        'data': 'su2|skip|legacy',
        'from': {'id': 123, 'is_bot': False},
        'message': {'message_id': 88, 'chat': {'id': 123, 'type': 'private'}},
    }
    assert commands.handle_callback('default', payload) == RETIRED_MESSAGE
    payload['from'] = {'id': 456, 'is_bot': False}
    with pytest.raises(ValueError, match='本人私聊'):
        commands.handle_callback('default', payload)


def _callback(update_id, data, *, sender=123, chat=123, date=1):
    return {
        'update_id': update_id,
        'callback_query': {
            'id': 'callback-' + str(update_id),
            'data': data,
            'from': {'id': sender, 'is_bot': False},
            'message': {'message_id': update_id, 'date': date,
                        'chat': {'id': chat, 'type': 'private'}},
        },
    }


def test_old_su_callbacks_can_be_queued_for_owner_even_before_cutoff(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.sqlite3')
    box.put('commands_not_before', 100)
    monkeypatch.setattr(commands, 'profile_env', lambda _: {'TELEGRAM_HOME_CHANNEL': '123'})
    bot.store_updates(box, 'default', [
        _callback(1, 'su1|skip|legacy', date=1),
        _callback(2, 'su2|approve|legacy', date=1, sender=456),
        _callback(3, 'st1|close', date=1),
    ])
    with box.connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM updates ORDER BY update_id')]
    assert [row['update_id'] for row in rows] == [1]
    assert rows[0]['kind'] == 'callback_query'


def test_signature_compatibility_is_stable(monkeypatch):
    monkeypatch.setattr(software_updates, 'profile_env', lambda _: {'TELEGRAM_BOT_TOKEN': 'fixture-token'})
    assert software_updates._signature('default', 'select', 'sur-0123456789abcdef0123') == '3d5cc148b915'
