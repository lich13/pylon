import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylon import inline, ssh_travel, scheduler
from pylon.admin import control, ssh_travel as root
from pylon.outbox import Outbox


@pytest.fixture
def firewall(tmp_path, monkeypatch):
    for name in ('STATE', 'JOURNAL', 'LOCK'):
        monkeypatch.setattr(root, name, tmp_path / name)
    state = {'open': False, 'foreign': False, 'active': True, 'commands': []}
    def run(args):
        state['commands'].append(args)
        if args == ['status']:
            return ('Status: active\n' if state['active'] else 'Status: inactive\n') + (
                '22/tcp ALLOW IN Anywhere # pylon-ssh-travel\n' if state['open'] else '') + (
                '22/tcp ALLOW IN Anywhere # foreign fixture\n' if state['foreign'] else '')
        assert args in (root.RULE, ['--force', 'delete', *root.RULE])
        state['open'] = args == root.RULE
        return 'Rules updated'
    monkeypatch.setattr(root, 'run', run)
    monkeypatch.setattr(root.os, 'geteuid', lambda: 0)
    return state


def test_open_close_persistent_idempotence_and_restart(firewall):
    assert root.change(True)['enabled']
    assert root.read()['enabled']
    assert not root.change(True)['changed']
    firewall['open'] = False
    assert root.main(['restore']) == 0
    assert firewall['open']
    assert root.change(False)['enabled'] is False
    assert root.main(['restore']) == 0
    assert not firewall['open']
    assert firewall['commands'].count(root.RULE) == 2
    assert firewall['commands'].count(['--force', 'delete', *root.RULE]) == 1
    assert root.STATE.stat().st_mode & 0o777 == 0o600
    assert not root.JOURNAL.exists()


def test_commit_failure_restores_previous_firewall(firewall, monkeypatch):
    write = root.write
    def fail(path, value):
        if path == root.STATE and value['enabled']:
            raise OSError('fixture disk full')
        write(path, value)
    monkeypatch.setattr(root, 'write', fail)
    with pytest.raises(OSError):
        root.change(True)
    assert not firewall['open']
    assert not root.read()['enabled']
    assert not root.JOURNAL.exists()


def test_interrupted_change_recovers_journal(firewall):
    previous = root.read()
    root.write(root.JOURNAL, {'previous': previous, 'was_open': False})
    firewall['open'] = True
    root.main(['restore'])
    assert not firewall['open']
    assert not root.JOURNAL.exists()


def test_foreign_rule_and_inactive_ufw_are_never_modified(firewall):
    firewall['foreign'] = True
    with pytest.raises(ValueError, match='其他'):
        root.change(True)
    assert firewall['commands'] == [['status']]
    firewall.update(foreign=False, active=False)
    with pytest.raises(ValueError, match='未启用'):
        root.change(True)


@pytest.mark.parametrize('args', [[], ['open', '22'], ['open', '0.0.0.0/0'], ['execute'], ['restore', 'anything']])
def test_root_rejects_caller_parameters(args, firewall):
    with pytest.raises(ValueError):
        root.main(args)
    assert not firewall['commands']


def test_root_control_limits_restore_to_boot_service(monkeypatch):
    monkeypatch.setattr(control.os, 'geteuid', lambda: 0)
    call = Mock(return_value=0); monkeypatch.setattr(root, 'main', call)
    for args in [['ssh', 'travel', 'open', '22'], ['ssh', 'travel', 'restore'], ['ssh', 'travel']]:
        with pytest.raises(ValueError):
            control.main(args)
    assert control.main(['ssh', 'travel', 'close']) == 0
    call.assert_called_once_with(['close'])


def test_bot_signed_callback_owned_and_duplicate(monkeypatch, tmp_path):
    box = Outbox(tmp_path / 'outbox.sqlite3')
    monkeypatch.setattr(ssh_travel, 'Outbox', lambda: box)
    monkeypatch.setattr(ssh_travel, 'owns_callback', lambda *a: True)
    calls = []
    def invoke(*args):
        calls.append(args)
        return {'enabled': True, 'consistent': True, 'rule_present': True}
    monkeypatch.setattr(scheduler, 'ssh', invoke)
    payload = {'id': 'fixture', 'data': ssh_travel.callback_data('default', 'open')}
    ssh_travel.callback('default', payload)
    ssh_travel.callback('default', payload)
    assert calls == [('travel', 'open'), ('travel', 'status')]
    monkeypatch.setattr(ssh_travel, 'owns_callback', lambda *a: False)
    with pytest.raises(ValueError, match='本人会话'):
        ssh_travel.callback('default', payload)
    with pytest.raises(ValueError, match='用法'):
        ssh_travel.command('default', 'open;id')


def test_card_ownership_requires_receipt_and_exact_button(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.sqlite3')
    monkeypatch.setattr(inline, 'profile_env', lambda _: {'TELEGRAM_HOME_CHANNEL': '123'})
    token = 'fixture-action'
    event = box.enqueue(profile='default', job_id='bot', kind='text', key='fixture',
                        payload={'text': 'fixture', 'reply_markup': {'inline_keyboard': [[{'text': 'open', 'callback_data': token}]]}})
    box.finish(event, 'delivered', receipt=[{'message_id': 99, 'chat_id': 123}])
    payload = {'data': token, 'from': {'id': 123}, 'message': {'message_id': 99, 'chat': {'id': 123, 'type': 'private'}}}
    assert inline.owns_callback('default', payload, box=box)
    assert not inline.owns_callback('webops', payload, box=box)
    payload['data'] = 'other'
    assert not inline.owns_callback('default', payload, box=box)
