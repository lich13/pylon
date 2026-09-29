from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylon.tasks.software import commands


@pytest.mark.parametrize('text,kind,value', [
    ('增加监控 https://github.com/openai/codex', 'add', 'https://github.com/openai/codex'),
    ('加监控 owner/repo', 'add', 'owner/repo'),
    ('监控这个网站 https://example.com/feed.xml', 'add', 'https://example.com/feed.xml'),
    ('移除监控', 'remove_list', None),
    ('删除监控', 'remove_list', None),
])
def test_existing_command_syntax(text, kind, value):
    parsed = commands.parse_command(text)
    assert (parsed.kind, parsed.value) == (kind, value)


def test_number_requires_pending_selection():
    assert commands.parse_command('3') is None
    assert commands.parse_command('3', has_pending=True).value == '3'
    assert commands.parse_command('帮我增加监控 owner/repo') is None
    assert commands.parse_command('增加监控') is None


def result(data, code=0):
    return commands.CommandResult(code, data, '')


def test_add_validates_saved_configuration_and_reports_failure(monkeypatch):
    run = Mock(side_effect=[result({'ok': True, 'added': True}), result({'ok': False})])
    monkeypatch.setattr(commands, 'run_monitor_json', run)
    text = commands.handle_add('owner/repo')
    assert '已添加' in text and 'FAIL' in text
    assert run.call_args_list[0].args[0] == ['--add-monitor-url', 'owner/repo', '--json']
    assert run.call_args_list[1].args[0] == ['--validate-config', '--json']


@pytest.mark.parametrize('data,expected', [
    ({'ok': True, 'existing': True}, '已存在'),
    ({'ok': False, 'unsupported': True, 'error': 'unsupported site'}, '不支持，未添加'),
    ({'ok': False, 'error': 'request failed'}, '添加失败'),
])
def test_add_result_preserves_user_feedback(data, expected, monkeypatch):
    monkeypatch.setattr(commands, 'run_monitor_json', Mock(return_value=result(data)))
    assert expected in commands.handle_add('owner/repo')


def test_remove_uses_stable_identity_and_preserves_pending_after_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(commands, 'PENDING_PATH', tmp_path / 'pending.json')
    source = SimpleNamespace(platform='telegram', chat_id='owner', thread_id=None)
    projects = [{'number': 1, 'id': 'stable-project', 'name': 'Project'}]
    run = Mock(return_value=result({'ok': True, 'projects': projects}))
    monkeypatch.setattr(commands, 'run_monitor_json', run)
    assert '1.' in commands.handle_remove_list(source)
    assert '编号无效' in commands.handle_remove_number(source, '2')
    run.return_value = result({'ok': False, 'error': 'temporarily unavailable'})
    assert '移除失败' in commands.handle_remove_number(source, '1')
    assert commands.source_key(source) in commands.load_pending(commands.PENDING_PATH)
    assert run.call_args.args[0] == ['--remove-project', 'stable-project', '--json']
    run.return_value = result({'ok': True, 'removed': True})
    assert '移除' in commands.handle_remove_number(source, '1')
    assert commands.source_key(source) not in commands.load_pending(commands.PENDING_PATH)


def test_subprocess_is_a_fixed_module_with_literal_user_input(monkeypatch):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr=''))
    monkeypatch.setattr(commands.subprocess, 'run', run)
    commands.run_monitor_json(['--add-monitor-url', 'owner/repo;touch /tmp/unwanted', '--json'])
    assert run.call_args.args[0][1:3] == ['-m', 'pylon.tasks.software.monitor']
    assert run.call_args.args[0][4] == 'owner/repo;touch /tmp/unwanted'
    assert not run.call_args.kwargs.get('shell')
