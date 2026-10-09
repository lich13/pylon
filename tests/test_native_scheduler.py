import importlib.util
import ipaddress
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylon import commands, runner, scheduler
from pylon.outbox import Outbox

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'ops' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bootstrap = load('bootstrap')
from pylon.admin import control as admin, ssh


def test_all_jobs_keep_schedule_and_native_catchup():
    catalog = json.loads((ROOT / 'jobs.json').read_text())
    for job_id, command in [('b7a84d1d9f21', 'collect'), ('linuxdo-review', 'review')]:
        job = catalog[job_id]
        assert job['module'] == 'pylon.tasks.linuxdo'
        assert job['args'] == [command]
        assert job['schedule'] == '* * * * *'
        assert job['deliver'] is False
    for key, job in catalog.items():
        unit = bootstrap.timer_text(key, job)
        assert f'Unit=pylon-task@{key}.service' in unit
        if not job.get('monotonic'):
            assert 'Persistent=true' in unit
            assert 'Asia/Shanghai' in unit
    assert 'OnCalendar=*-*-* 9,12,15,18:55:00 Asia/Shanghai' in bootstrap.timer_text('quote', catalog['f3ec3ec24210'])
    assert 'RandomizedDelaySec=30m' in bootstrap.timer_text('refresh', catalog['ssh-beijing-refresh'])
    assert 'OnCalendar=Mon *-*-* 9:0:00 Asia/Shanghai' in bootstrap.timer_text('software-maintenance', catalog['software-maintenance'])
    assert 'OnCalendar=*-*-* 0/6:15:00 Asia/Shanghai' in bootstrap.timer_text('software-update-review', catalog['software-update-review'])
    assert list(catalog)[-2] == 'software-update-review'
    assert list(catalog)[-1] == 'marketplace-poll'
    assert catalog['marketplace-poll'] == {
        'name': '三平台二手交易', 'profile': 'webops', 'schedule': '* * * * *',
        'deliver': False, 'failure_notify': False, 'timeout': 240,
        'module': 'pylon.tasks.marketplace.poll',
    }
    assert catalog['software-update-review']['profile'] == 'default'
    assert '--apply' in catalog['xianbao-retention-cleanup']['args']
    assert catalog['f3ec3ec24210']['lock'] == catalog['e961da51fe69']['lock']


@pytest.mark.parametrize('event_statuses', [('suppressed',), ('delivered', 'suppressed')],
                         ids=['only-suppressed', 'mixed-terminal'])
def test_runner_completes_terminal_suppressed_events_without_waiting(tmp_path, monkeypatch, capsys, event_statuses):
    monkeypatch.setenv('PYLON_HOME', str(tmp_path))
    monkeypatch.setenv('INVOCATION_ID', 'fixture-invocation')
    box = Outbox(tmp_path / 'outbox.sqlite3')
    monkeypatch.setattr(runner, 'Outbox', lambda: box)
    monkeypatch.setattr(runner, 'jobs', lambda: {'fixture-job': {
        'name': '合成到期通知任务', 'profile': 'webops', 'module': 'fixture_module',
        'timeout': 1, 'deliver': False, 'failure_notify': False,
    }})
    monkeypatch.setattr(runner, 'task_environment', lambda profile: {})
    monkeypatch.setattr(runner.time, 'time', lambda: 1000.0)
    wait = Mock(side_effect=AssertionError('terminal notifications must not wait'))
    monkeypatch.setattr(runner.time, 'sleep', wait)

    def start_process(*args, **kwargs):
        run_id = kwargs['env']['PYLON_RUN_ID']
        for index, status in enumerate(event_statuses):
            eid = box.enqueue(profile='webops', job_id='fixture-job', kind='text',
                              payload={'text': '合成通知'}, key=f'fixture:{index}', run_id=run_id)
            box.finish(eid, status)
        return SimpleNamespace(returncode=0, poll=lambda: 0, communicate=lambda timeout: ('', ''))

    process = Mock(side_effect=start_process)
    monkeypatch.setattr(runner.subprocess, 'Popen', process)

    result = runner.run('fixture-job')

    assert result == 0
    process.assert_called_once()
    wait.assert_not_called()
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'ok'
    assert output['detail'] == ''
    assert output['notifications'] == {
        'delivered': event_statuses.count('delivered'), 'suppressed': event_statuses.count('suppressed'),
        'pending': 0, 'sending': 0, 'uncertain': 0, 'failed': 0,
    }
    with box.connect() as db:
        retained = db.execute('SELECT status,finished_at FROM runs WHERE id=?',
                              ('fixture-job:fixture-invocation',)).fetchone()
    assert retained['status'] == 'ok'
    assert retained['finished_at'] is not None


def test_runner_can_finish_without_waiting_for_pending_delivery(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('PYLON_HOME', str(tmp_path))
    monkeypatch.setenv('INVOCATION_ID', 'fixture-helper-invocation')
    box = Outbox(tmp_path / 'outbox.sqlite3')
    monkeypatch.setattr(runner, 'Outbox', lambda: box)
    monkeypatch.setattr(runner, 'jobs', lambda: {'fixture-helper': {
        'name': '合成 Helper 采集任务', 'profile': 'webops', 'module': 'fixture_module',
        'timeout': 1, 'deliver': False, 'failure_notify': False, 'wait_for_delivery': False,
    }})
    monkeypatch.setattr(runner, 'task_environment', lambda profile: {})
    monkeypatch.setattr(runner.time, 'time', lambda: 1000.0)
    wait = Mock(side_effect=AssertionError('collection must finish while delivery is pending'))
    monkeypatch.setattr(runner.time, 'sleep', wait)

    def start_process(*args, **kwargs):
        box.enqueue(profile='webops', job_id='fixture-helper', kind='text',
                    payload={'text': '合成个人通知'}, key='fixture-helper-notice',
                    run_id=kwargs['env']['PYLON_RUN_ID'])
        return SimpleNamespace(returncode=0, poll=lambda: 0, communicate=lambda timeout: ('', ''))

    process = Mock(side_effect=start_process)
    monkeypatch.setattr(runner.subprocess, 'Popen', process)

    result = runner.run('fixture-helper')

    assert result == 0
    process.assert_called_once()
    wait.assert_not_called()
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'ok'
    assert output['detail'] == ''
    assert output['notifications'] == {
        'delivered': 0, 'suppressed': 0, 'pending': 1, 'sending': 0, 'uncertain': 0, 'failed': 0,
    }
    with box.connect() as db:
        retained = db.execute('SELECT status,finished_at FROM runs WHERE id=?',
                              ('fixture-helper:fixture-helper-invocation',)).fetchone()
        event = db.execute('SELECT status,receipt FROM events').fetchone()
    assert retained['status'] == 'ok'
    assert retained['finished_at'] is not None
    assert event['status'] == 'pending'
    assert event['receipt'] is None


def test_root_helper_rejects_unknown_units_actions_and_extra_arguments(tmp_path, monkeypatch):
    path = tmp_path / 'jobs.json'
    path.write_text('{"safe":{}}')
    monkeypatch.setattr(admin, 'CATALOG', path)
    monkeypatch.setattr(admin.os, 'geteuid', lambda: 0)
    execute = Mock(side_effect=AssertionError('must not invoke a subprocess'))
    monkeypatch.setattr(admin, 'command', execute)
    for args in [['run', 'ssh.service'], ['run', 'safe', '--user'], ['stop', 'safe'], ['run', 'safe;id'], ['ssh', 'load']]:
        with pytest.raises(ValueError):
            admin.main(args)
    execute.assert_not_called()


def test_status_shows_monotonic_next_and_scheduled_running(monkeypatch):
    monkeypatch.setattr(scheduler, 'jobs', lambda: {'safe': {}})
    output = 'Id=pylon-job-safe.timer\nActiveState=active\nUnitFileState=enabled\n\nId=pylon-task@safe.service\nActiveState=activating\n'
    responses = iter([SimpleNamespace(stdout=output), SimpleNamespace(stdout=json.dumps([{'unit': 'pylon-job-safe.timer', 'next': 1790697600000000}]))])
    monkeypatch.setattr(scheduler.subprocess, 'run', lambda *a, **kw: next(responses))
    state = scheduler.states()['safe']
    assert state['running'] and not state['paused'] and state['enabled']
    assert state['next'] == '09-30 00:00:00'


def test_management_commands_do_not_run_shell_or_accept_unscoped_arguments(monkeypatch):
    monkeypatch.setattr(commands, 'jobs', lambda: {'abc': {'name': '任务'}})
    start = Mock(return_value='已提交执行')
    monkeypatch.setattr(scheduler, 'start', start)
    assert '已下线' in commands.handle('default', '/run 1')
    start.assert_not_called()
    assert commands.handle('default', '/ssh refresh') == '已提交执行'
    with pytest.raises(ValueError):
        commands.handle('default', '/ssh refresh https://example.com')
    assert start.call_args_list[0].args == ('ssh-beijing-refresh',)


def test_trusted_login_learning_requires_exact_user_and_fingerprint():
    config = {'user': 'ubuntu', 'trusted_fingerprints': ['SHA256:Current']}
    journal = '\n'.join([
        'Accepted publickey for ubuntu from 111.201.1.2 port 555 ssh2: ED25519 SHA256:Current',
        'Accepted publickey for ubuntu from 111.201.1.3 port 555 ssh2: ED25519 SHA256:Old',
        'Accepted publickey for root from 111.201.1.4 port 555 ssh2: ED25519 SHA256:Current',
        'Failed publickey for ubuntu from 111.201.1.5 port 555 ssh2: ED25519 SHA256:Current',
        'Accepted password for ubuntu from 111.201.1.6 port 555 SHA256:Current'])
    assert ssh.learned_ips(config, journal) == ['111.201.1.2']


@pytest.mark.parametrize('text', ['', '0.0.0.0/0', '127.0.0.1/32', '::1/128', 'not a network', '1.1.1.1/32\n1.1.1.1/32'])
def test_reject_bad_downloads(text):
    with pytest.raises(ValueError):
        ssh.networks(text, minimum=1)


def transaction_fixture(tmp_path, monkeypatch):
    old_values = [ipaddress.ip_network(f'1.0.{i // 256}.{i % 256}/32') for i in range(1024)]
    new_values = [ipaddress.ip_network(f'1.1.{i // 256}.{i % 256}/32') for i in range(1024)]
    for name in ('CIDRS', 'PREVIOUS', 'TRANSACTION'):
        monkeypatch.setattr(ssh, name, tmp_path / name)
    old = ''.join(f'{x}\n' for x in old_values).encode()
    ssh.CIDRS.write_bytes(old)
    kernel = {ssh.SET: old_values}
    def fake_run(args, **kwargs):
        action = args[1]
        if action == 'create': kernel.setdefault(args[2], [])
        elif action == 'flush': kernel[args[2]] = []
        elif action == 'restore': kernel[ssh.STAGE] = [ipaddress.ip_network(line.split()[2]) for line in kwargs['input'].splitlines()]
        elif action == 'swap': kernel[args[2]], kernel[args[3]] = kernel[args[3]], kernel[args[2]]
        elif action == 'destroy': kernel.pop(args[2], None)
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(ssh, 'run', fake_run)
    return old_values, new_values, kernel, old


def test_file_commit_failure_restores_live_set_and_file(tmp_path, monkeypatch):
    old_values, values, kernel, old = transaction_fixture(tmp_path, monkeypatch)
    real_write = ssh.atomic_write
    def write(path, data):
        if path == ssh.CIDRS and data != old:
            raise OSError('disk full')
        real_write(path, data)
    monkeypatch.setattr(ssh, 'atomic_write', write)
    with pytest.raises(OSError, match='disk full'):
        ssh.install(values)
    assert ssh.CIDRS.read_bytes() == old
    assert kernel[ssh.SET] == old_values
    assert not ssh.TRANSACTION.exists()


def test_crash_between_kernel_swap_and_file_commit_recovers(tmp_path, monkeypatch):
    old_values, values, kernel, old = transaction_fixture(tmp_path, monkeypatch)
    ssh.PREVIOUS.write_bytes(old)
    ssh.TRANSACTION.write_text('{"stage":"prepared"}')
    kernel[ssh.SET] = values
    ssh.CIDRS.write_text(''.join(f'{x}\n' for x in values))
    ssh.recover()
    assert kernel[ssh.SET] == old_values
    assert ssh.CIDRS.read_bytes() == old
    assert not ssh.TRANSACTION.exists() and not ssh.PREVIOUS.exists()


def test_current_admin_source_cannot_be_removed(tmp_path, monkeypatch):
    _, values, kernel, old = transaction_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(ssh, 'member', lambda address: True)
    with pytest.raises(ValueError, match='management source'):
        ssh.install(values, protected=['1.0.0.1'])
    assert ssh.CIDRS.read_bytes() == old
    assert ssh.STAGE not in kernel
