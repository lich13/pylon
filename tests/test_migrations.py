import json
import time

import pytest

from pylon.migrations import OLD_XIANBAO, NEW_XIANBAO, migrate_profile, rename_xianbao
from pylon.outbox import Outbox
from pylon.shared.retention import prune_runs


def test_task_rename_preserves_queue_receipts_and_run_links(tmp_path):
    box = Outbox(tmp_path / 'outbox.sqlite3')
    run_id = OLD_XIANBAO + ':invocation'
    with box.connect() as db:
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)', (run_id, OLD_XIANBAO, 1, 2, 'delivery_pending', ''))
    ids = []
    for state in ('pending', 'uncertain', 'delivered'):
        event = box.enqueue(profile='default', job_id=OLD_XIANBAO, kind='text', payload={'text': state},
                            key=state, run_id=run_id)
        box.finish(event, state, receipt={'message_id': 42} if state == 'delivered' else None)
        ids.append(event)
    box.put('failed:' + OLD_XIANBAO, True)
    rename_xianbao(box)
    rename_xianbao(box)
    with box.connect() as db:
        events = [dict(row) for row in db.execute('SELECT * FROM events ORDER BY created_at')]
        assert [row['id'] for row in events] == ids
        assert [row['status'] for row in events] == ['pending', 'uncertain', 'delivered']
        assert all(row['job_id'] == NEW_XIANBAO for row in events)
        assert json.loads(events[-1]['receipt']) == {'message_id': 42}
        assert db.execute('SELECT job_id FROM runs').fetchone()[0] == NEW_XIANBAO
    assert len(box.run_events(NEW_XIANBAO + ':invocation')) == 3
    assert box.get('failed:' + NEW_XIANBAO) is True


def test_profile_rename_preserves_rule_bytes_and_refuses_conflict(tmp_path):
    folder = tmp_path / 'config'
    folder.mkdir()
    original = b'{"custom_rules": ["keep me"]}\n'
    old = folder / 'xianbao-hermes-rules.json'
    new = folder / 'xianbao-rules.json'
    old.write_bytes(original)
    (folder / 'xianbao-filter.env').write_text('XIANBAO_RULES=/profiles/config/xianbao-hermes-rules.json\nKEEP=unchanged\n')
    migrate_profile(tmp_path)
    migrate_profile(tmp_path)
    assert new.read_bytes() == original and not old.exists()
    assert 'KEEP=unchanged' in (folder / 'xianbao-filter.env').read_text()
    old.write_text('different')
    with pytest.raises(ValueError, match='different contents'):
        migrate_profile(tmp_path)
    assert old.read_text() == 'different' and new.read_bytes() == original


def test_retention_keeps_latest_failures_pending_and_delivery_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv('PYLON_OUTBOX', str(tmp_path / 'outbox.sqlite3'))
    monkeypatch.setenv('PYLON_HOME', str(tmp_path))
    (tmp_path / 'jobs.json').write_text(json.dumps({'job': {'profile': 'webops'}}))
    box = Outbox()
    old = time.time() - 4 * 86400
    for index, status in enumerate(['no_change', 'ok', 'ok', 'failed', 'ok']):
        with box.connect() as db:
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?)', (f'job:{index}', 'job', old + index, old + index, status, ''))
    event = box.enqueue(profile='webops', job_id='job', kind='text', payload={'text': 'waiting'}, key='waiting', run_id='job:2')
    delivered = box.enqueue(profile='webops', job_id='job', kind='text', payload={'text': 'sent'}, key='sent', run_id='job:1')
    box.finish(delivered, 'delivered', receipt={'message_id': 1})
    assert prune_runs('webops', 2, dry_run=True) == 2
    assert prune_runs('webops', 2, dry_run=False) == 2
    with box.connect() as db:
        assert {row[0] for row in db.execute('SELECT id FROM runs')} == {'job:2', 'job:3', 'job:4'}
        assert {row[0] for row in db.execute('SELECT id FROM events')} == {event, delivered}
    assert box.enqueue(profile='webops', job_id='job', kind='text', payload={'text': 'sent'}, key='sent') == delivered
    assert box.counts() == {'delivered': 1, 'pending': 1}
