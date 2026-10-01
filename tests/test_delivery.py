import hashlib
import json
from pathlib import Path

import httpx
import pytest

from pylon import transport
from pylon.bot import recover, store_updates
from pylon.commands import authorized
from pylon.events import silent
from pylon.outbox import Outbox


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv('PYLON_HOME', str(tmp_path))
    for profile in ['default', 'webops']:
        root = tmp_path / 'profiles' / profile
        root.mkdir(parents=True)
        (root / '.env').write_text('TELEGRAM_BOT_TOKEN=test-token\nTELEGRAM_HOME_CHANNEL=123\n')
    monkeypatch.setattr(transport.time, 'sleep', lambda _: None)
    return Outbox(tmp_path / 'data/outbox.sqlite3')


def enqueue(box, text='消息 [链接](https://example.com/a?q=1&b=2)', key='same'):
    return box.enqueue(profile='webops', job_id='test', kind='text', payload={'text': text}, key=key, run_id='run1')


def row(box, eid):
    with box.connect() as db:
        return dict(db.execute('SELECT * FROM events WHERE id=?', (eid,)).fetchone())


def test_durable_enqueue_dedup_and_recovery(box):
    eid = enqueue(box)
    assert enqueue(box) == eid
    restarted = Outbox(box.path)
    assert restarted.counts() == {'pending': 1}
    assert restarted.claim()['id'] == eid
    recover(restarted)
    assert row(box, eid)['status'] == 'uncertain'
    assert restarted.claim() is None
    assert len(restarted.run_events('run1')) == 1


def test_attachment_survives_source_replacement(box, tmp_path):
    source = tmp_path / 'report.html'
    source.write_text('<html>原报告</html>')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    eid = box.document(source, profile='webops', job_id='report', key='date', payload={'caption': '报告'})
    source.write_text('被覆盖')
    payload = json.loads(row(box, eid)['payload'])
    assert payload['sha256'] == digest
    assert Path(payload['path']).read_text() == '<html>原报告</html>'


def test_rate_limit_retries_only_unacknowledged_chunks(box):
    eid = enqueue(box, '开头\n' + '中😀' * 2500 + '\n末尾')
    requests = []
    def handler(request):
        requests.append(request.content)
        if len(requests) == 2:
            return httpx.Response(429, json={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 2}})
        return httpx.Response(200, json={'ok': True, 'result': {'message_id': len(requests), 'chat': {'id': 123}}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        transport.deliver_event(box, box.claim(), http)
        first = row(box, eid)
        assert first['status'] == 'pending'
        assert len(json.loads(first['receipt'])) == 1
        box.finish(eid, 'pending')
        transport.deliver_event(box, box.claim(), http)
    final = row(box, eid)
    assert final['status'] == 'delivered'
    assert len(json.loads(final['receipt'])) == 3
    assert requests[0] not in requests[2:]
    assert requests[1] == requests[2]


@pytest.mark.parametrize('error,status', [(httpx.ConnectTimeout, 'pending'), (httpx.ReadTimeout, 'uncertain'), (httpx.WriteError, 'uncertain')])
def test_transport_uncertainty_never_becomes_success(box, error, status):
    eid = enqueue(box)
    def handler(request):
        raise error('injected fault')
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        transport.deliver_event(box, box.claim(), http)
    assert row(box, eid)['status'] == status


def test_chunking_preserves_text_emoji_and_link():
    from html.parser import HTMLParser
    class Parser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.stack, self.text, self.links = [], '', []
        def handle_starttag(self, tag, attrs):
            self.stack.append(tag)
            if tag == 'a':
                self.links.append(dict(attrs)['href'])
        def handle_endtag(self, tag):
            assert self.stack.pop() == tag
        def handle_data(self, data):
            self.text += data
    chunks = transport.telegram_chunks('**' + '😀你好' * 3000 + '**\n[链接](https://example.com/)')
    text = ''
    links = []
    for chunk in chunks:
        parser = Parser()
        parser.feed(chunk)
        assert parser.stack == []
        assert len(parser.text.encode('utf-16-le')) // 2 <= 3500
        text += parser.text
        links += parser.links
    assert text == '😀你好' * 3000 + '\n链接'
    assert links == ['https://example.com/']


def test_monitor_removal_numbers_survive_telegram_formatting():
    message = '当前监控项目，回复数字移除:\n' + '\n'.join(
        f'{number}. 监控项目 {number}' for number in range(1, 11)
    )
    rendered = ''.join(transport.telegram_chunks(message))
    for number in range(1, 11):
        assert f'{number}. 监控项目 {number}' in rendered
    assert '•' not in rendered


def test_ordered_list_start_and_nested_bullets():
    rendered = ''.join(transport.telegram_chunks('7. 外层\n   - 子项\n8. 次项\n\n- 普通项目'))
    assert '7. 外层' in rendered
    assert '  • 子项' in rendered
    assert '8. 次项' in rendered
    assert '• 普通项目' in rendered


@pytest.mark.parametrize('text', ['', ' ', '[SILENT]', '{"wakeAgent":false}', 'NO_REPLY'])
def test_silent_outputs(text):
    assert silent(text)
    assert not silent('真实通知')


def test_owner_only_and_durable_update_offsets(box):
    def message(chat=123, sender=123, kind='private'):
        return {'chat': {'id': chat, 'type': kind}, 'from': {'id': sender}, 'text': '/status'}
    assert authorized('webops', message())
    assert not authorized('webops', message(sender=456))
    assert not authorized('webops', message(kind='group'))
    assert not authorized('webops', message(chat=456))
    updates = [{'update_id': 1, 'message': message()}, {'update_id': 2, 'message': message(sender=456)}]
    store_updates(box, 'webops', updates)
    store_updates(box, 'webops', updates)
    assert box.get('offset:webops') == 3
    with box.connect() as db:
        assert db.execute('SELECT count(*) FROM updates').fetchone()[0] == 1


def test_owner_callback_updates_keep_kind_and_message_binding(box):
    callback = {'id': 'cq-1', 'from': {'id': 123}, 'data': 'g2|open|gmail-nt-test|deadbeef',
                'message': {'message_id': 88, 'date': 100, 'chat': {'id': 123, 'type': 'private'}}}
    store_updates(box, 'webops', [{'update_id': 9, 'callback_query': callback}])
    with box.connect() as db:
        row = db.execute('SELECT kind,chat_id,message_id FROM updates WHERE update_id=9').fetchone()
    assert tuple(row) == ('callback_query', '123', 88)


def test_interrupted_commands_do_not_repeat_mutations(box):
    store_updates(box, 'webops', [{'update_id': 5, 'message': {'chat': {'id':123, 'type':'private'}, 'from': {'id':123}, 'text':'移除监控'}}])
    with box.connect() as db:
        db.execute("UPDATE updates SET status='running'")
    recover(box)
    with box.connect() as db:
        assert db.execute('SELECT status FROM updates').fetchone()[0] == 'uncertain'
        assert db.execute("SELECT count(*) FROM events WHERE job_id='bot'").fetchone()[0] == 1
    recover(box)
    assert box.counts() == {'pending': 1}
