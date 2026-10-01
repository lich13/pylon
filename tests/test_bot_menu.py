import json
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from pylon import bot_menu, commands, scheduler
from pylon.command_catalog import COMMANDS, help_text, menu_commands
from pylon.outbox import Outbox
from pylon.tasks.software import commands as software
from pylon.transport import DeliveryError


def test_catalog_is_valid_chinese_and_has_detailed_help():
    assert len(COMMANDS) == 36
    assert len({row['command'] for row in menu_commands()}) == len(COMMANDS)
    for item in COMMANDS.values():
        assert re.fullmatch('[a-z0-9_]{1,32}', item.name)
        assert 1 <= len(item.description) <= 256
        assert re.search('[\u4e00-\u9fff]', item.description)
        text = help_text(item.name)
        assert '用法：' in text and '示例：' in text and item.detail in text
        assert 'Hermes' not in text
    assert commands.handle('default', '/start') == help_text()
    assert commands.handle('webops', '/help@back13bot run') == help_text('run')


@pytest.mark.parametrize('name', [name for name, c in COMMANDS.items() if c.argument and not c.argument.startswith('[') and c.route != 'help'])
def test_missing_arguments_only_return_specific_help(name, monkeypatch):
    for attr in ('start', 'suspend', 'admin', 'ssh', 'logs'):
        monkeypatch.setattr(scheduler, attr, Mock(side_effect=AssertionError('must not execute')))
    monkeypatch.setattr(commands, 'locked_command', Mock(side_effect=AssertionError('must not execute')))
    monkeypatch.setattr(commands, 'monitor_command', Mock(side_effect=AssertionError('must not execute')))
    assert commands.handle('default', '/' + name) == help_text(name)
    if COMMANDS[name].route == 'task':
        assert '任务编号：' in commands.handle('webops', '/' + name)


@pytest.mark.parametrize('name', COMMANDS)
def test_every_menu_entry_reaches_its_existing_operation(name, monkeypatch):
    item = COMMANDS[name]
    from pylon import software_updates
    inventory, update = Mock(return_value='清单'), Mock(return_value='已提交')
    monkeypatch.setattr(software_updates, 'inventory_text', inventory)
    monkeypatch.setattr(software_updates, 'request_update', update)
    from pylon import agent_tasks
    agent = Mock(return_value='Agent 已提交')
    monkeypatch.setattr(agent_tasks, 'command', agent)
    calls = {}
    for method in ('status_text', 'outbox_text', 'control_task', 'ssh_text', 'chiphell_text', 'gmail_text', 'monitor_command'):
        calls[method] = Mock(return_value=method)
        monkeypatch.setattr(commands, method, calls[method])
    monkeypatch.setattr(commands, 'jobs', lambda: {'safe': {'name': '任务'}})
    monkeypatch.setattr(commands, 'profile_env', lambda _: {'TELEGRAM_HOME_CHANNEL': '123'})
    start, logs = Mock(return_value='已提交'), Mock(return_value='日志')
    monkeypatch.setattr(scheduler, 'start', start)
    monkeypatch.setattr(scheduler, 'logs', logs)
    argument = {'task': '1', 'gmail': 'gmail-nt-test', 'ssh': '1.2.3.4', 'monitor': 'owner/repo', 'help': 'status', 'agent': '0123456789ab 检查版本'}.get(item.route, '') if item.argument else ''
    result = commands.handle('default', '/' + name + (' ' + argument if argument else ''))
    if item.route == 'task':
        if item.action in {'pause', 'resume'}:
            calls['control_task'].assert_called_once_with('safe', item.action == 'pause')
        else:
            (logs if item.action == 'logs' else start).assert_called_once_with('safe')
    elif item.route in {'gmail', 'ssh'}:
        calls[item.route + '_text'].assert_called_once_with(item.action, argument)
    elif item.route == 'chiphell':
        calls['chiphell_text'].assert_called_once_with(item.action)
    elif item.route == 'monitor':
        value = {'list': '/monitors', 'add': '增加监控 owner/repo', 'remove': '移除监控'}[item.action]
        calls['monitor_command'].assert_called_once_with(value, '123')
    elif item.route == 'updates':
        inventory.assert_called_once_with(full=False)
    elif item.route == 'software_update':
        update.assert_called_once_with(item.action, 'default')
    elif item.route == 'agent':
        agent.assert_called_once_with(item.action, argument, 'default')
    elif item.route == 'help':
        assert result == help_text('status')
    else:
        calls[item.route + '_text'].assert_called_once_with()


@pytest.mark.parametrize('old,new', [('/gmail', '/gmail_candidates'), ('/gmail list', '/gmail_candidates'),
    ('/gmail accept gmail-nt-a', '/gmail_accept gmail-nt-a'), ('/gmail ignore gmail-nt-a', '/gmail_ignore gmail-nt-a'),
    ('/ssh', '/ssh_status'), ('/ssh check 1.2.3.4', '/ssh_check 1.2.3.4'), ('/ssh sync', '/ssh_sync'),
    ('/ssh refresh', '/ssh_refresh'), ('/chiphell', '/chiphell_status'), ('/chiphell test', '/chiphell_test'),
    ('/chiphell pause', '/chiphell_pause'), ('/chiphell resume', '/chiphell_resume')])
def test_legacy_and_flat_commands_share_handlers(old, new, monkeypatch):
    for name in ('gmail_text', 'ssh_text', 'chiphell_text'):
        monkeypatch.setattr(commands, name, lambda *args: args)
    assert commands.handle('default', old) == commands.handle('default', new)


def test_retired_commands_and_extra_arguments_do_not_execute(monkeypatch):
    monkeypatch.setattr(scheduler, 'start', Mock(side_effect=AssertionError('must not execute')))
    for name in ('model', 'new', 'sessions', 'agents', 'approve', 'yolo'):
        assert '没有这个命令' in commands.handle('default', '/' + name)
    for text in ('/ssh_refresh arbitrary', '/chiphell_pause now', '/remove_monitor 1', '/gmail_candidates gmail-nt-x'):
        with pytest.raises(ValueError, match='不接受参数'):
            commands.handle('default', text)


def test_monitors_does_not_arm_or_change_numbered_removal(tmp_path, monkeypatch):
    monkeypatch.setattr(software, 'PENDING_PATH', tmp_path / 'pending.json')
    projects = [{'id': 'p', 'number': 1, 'name': 'Project'}]
    monkeypatch.setattr(software, 'run_monitor_json', Mock(return_value=software.CommandResult(0, {'ok': True, 'projects': projects}, '')))
    assert '回复数字' not in software.handle_list()
    assert not software.PENDING_PATH.exists()
    source = SimpleNamespace(platform='telegram', chat_id='123')
    software.handle_remove_list(source)
    previous = software.PENDING_PATH.read_bytes()
    software.handle_list()
    assert software.PENDING_PATH.read_bytes() == previous


def test_gmail_and_ssh_validate_before_fixed_operations(monkeypatch):
    execute = Mock(return_value='已加入规则')
    monkeypatch.setattr(commands, 'locked_command', execute)
    assert '格式不正确' in commands.handle('default', '/gmail_accept ../../rules')
    execute.assert_not_called()
    assert '本次未删除邮件' in commands.handle('default', '/gmail_accept gmail-nt-abc123')
    args = execute.call_args.args[1]
    assert args[1:] == ['-m', 'pylon.tasks.gmail.cleanup', '--accept-candidate', 'gmail-nt-abc123', '--format', 'text']
    check = Mock()
    monkeypatch.setattr(scheduler, 'ssh', check)
    assert '有效的 IPv4' in commands.handle('default', '/ssh_check 1.2.3.4;id')
    check.assert_not_called()


def test_chiphell_paused_busy_failed_and_fresh_success_are_distinct(monkeypatch):
    data = {'poller_active': False, 'pending': 3, 'health': {'status': 'OK'}}
    admin = Mock(side_effect=scheduler.AdminError(SimpleNamespace(returncode=1, stdout=json.dumps(data), stderr='')))
    monkeypatch.setattr(scheduler, 'admin', admin)
    monkeypatch.setattr(scheduler, 'paused', lambda _: True)
    assert '已暂停' in commands.handle('default', '/chiphell_status')
    admin.side_effect = scheduler.AdminError(SimpleNamespace(returncode=1, stdout='POLLER_LOCKED', stderr=''))
    assert '未执行测试' in commands.handle('default', '/chiphell_test')
    admin.side_effect = scheduler.AdminError(SimpleNamespace(returncode=1, stdout='POLL_FAULT status=LOGIN_REQUIRED', stderr=''))
    assert '操作失败' in commands.handle('default', '/chiphell_test')
    admin.side_effect, admin.return_value = None, 'POLL_OK candidates=0 observe=1'
    assert '测试通过' in commands.handle('default', '/chiphell_test')
    admin.return_value = ''
    assert '未确认' in commands.handle('default', '/chiphell_test')


def test_outbox_is_read_only_and_keeps_uncertain_notifications(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.db')
    box.enqueue(profile='default', job_id='bot', kind='text', payload={'text': 'private content'}, key='one')
    event = box.claim()
    box.finish(event['id'], 'uncertain', error='network error')
    monkeypatch.setattr(commands, 'Outbox', lambda: box)
    before = box.counts()
    text = commands.handle('default', '/outbox')
    assert '回执不确定：1' in text and '不会自动重发' in text
    assert 'private content' not in text
    assert box.counts() == before


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setattr(bot_menu, 'profile_env', lambda p: {'TELEGRAM_BOT_TOKEN': 'token-' + p, 'TELEGRAM_HOME_CHANNEL': '123'})
    state, writes = {}, []

    def request(http, token, method, *, data, read_only):
        assert read_only
        query = dict(data)
        field = 'commands' if 'Commands' in method else 'menu_button'
        value = query.pop(field, None)
        key = (token, field, json.dumps(query, sort_keys=True))
        if method.startswith('set'):
            writes.append((token, method, dict(data)))
            state[key] = json.loads(value)
            return True
        return state.get(key, [{'command': 'model', 'description': 'Hermes model'}] if field == 'commands' else {'type': 'default'})

    monkeypatch.setattr(bot_menu, 'telegram_request', request)
    return state, writes, request


def test_both_bots_all_scopes_languages_buttons_and_idempotence(telegram):
    state, writes, _ = telegram
    for profile in bot_menu.PROFILES:
        result = bot_menu.synchronize(profile, http=object())
        assert result['ok'] and result['checked'] == 11 and result['changed'] == 11
        assert bot_menu.synchronize(profile, http=object())['changed'] == 0
    assert len(writes) == 22
    assert len([v for (_, field, _), v in state.items() if field == 'commands' and v == menu_commands()]) == 18
    assert not any('Hermes' in json.dumps(v) for v in state.values())


def test_check_is_read_only_and_reports_drift(telegram):
    _, writes, _ = telegram
    result = bot_menu.synchronize('default', apply=False, http=object())
    assert not result['ok'] and len(result['differences']) == 11
    assert not writes


def test_metadata_timeout_reads_back_before_retrying_write(telegram, monkeypatch):
    _, writes, original = telegram
    failed = False

    def uncertain(*args, **kwargs):
        nonlocal failed
        result = original(*args, **kwargs)
        if args[2].startswith('set') and not failed:
            failed = True
            raise DeliveryError('pending', 'ReadTimeout', 30)
        return result

    monkeypatch.setattr(bot_menu, 'telegram_request', uncertain)
    with pytest.raises(DeliveryError):
        bot_menu.synchronize('default', http=object())
    assert bot_menu.synchronize('default', http=object())['changed'] == 10
    assert len(writes) == 11


def test_sync_rejects_success_response_without_matching_readback(telegram, monkeypatch):
    _, _, original = telegram
    monkeypatch.setattr(bot_menu, 'telegram_request', lambda http, token, method, **kw:
                        True if method.startswith('set') else original(http, token, method, **kw))
    with pytest.raises(RuntimeError, match='核对不一致'):
        bot_menu.synchronize('default', http=object())


def test_background_honors_retry_after_without_repeating_successful_bot(monkeypatch):
    counts, waits = {}, []
    def sync(profile):
        counts[profile] = counts.get(profile, 0) + 1
        if profile == 'default' and counts[profile] == 1:
            raise DeliveryError('pending', 'rate limit', 42)
        return {'commands': 24, 'changed': 0}
    monkeypatch.setattr(bot_menu, 'synchronize', sync)
    stop = Mock(spec=threading.Event)
    stop.is_set.return_value = False
    stop.wait.side_effect = waits.append
    bot_menu.synchronize_background(stop)
    assert counts == {'default': 2, 'webops': 1}
    assert waits == [42]


@pytest.mark.parametrize('response', ['rate_limit', 'timeout'])
def test_real_transport_metadata_failure_remains_retryable(response, monkeypatch):
    monkeypatch.setattr(bot_menu, 'profile_env', lambda _: {'TELEGRAM_BOT_TOKEN': '123456789:SecretTokenWithEnoughCharacters123', 'TELEGRAM_HOME_CHANNEL': '123'})
    def handler(request):
        if response == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        return httpx.Response(429, json={'ok': False, 'error_code': 429, 'description': 'rate limit', 'parameters': {'retry_after': 55}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(DeliveryError) as failure:
            bot_menu.synchronize('default', http=http)
    assert failure.value.status == 'pending'
    assert failure.value.delay == (55 if response == 'rate_limit' else 30)
    assert 'SecretToken' not in str(failure.value)
