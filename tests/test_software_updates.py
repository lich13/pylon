import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from pylon import commands, software_inventory as inventory, software_updates as updates
from pylon.admin import software as admin
from pylon.outbox import Outbox
from pylon.tasks.software import infrastructure, maintenance, monitor


@pytest.fixture
def root_db(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, 'ROOT', tmp_path / 'privileged')
    launch = Mock(return_value='')
    monkeypatch.setattr(admin, 'capture', launch)
    monkeypatch.setattr(admin, 'active', lambda _: False)
    return launch


def get_run(key):
    with admin.database() as db:
        return dict(db.execute('SELECT * FROM runs WHERE id=?', (key,)).fetchone())


def test_duplicate_requests_merge_across_bots_and_survive_reopen(root_db):
    first = admin.start('codex', 'default', 'telegram:default:1')
    second = admin.start('codex', 'webops', 'telegram:webops:2')
    assert first['id'] == second['id']
    assert admin.start('codex', 'default', 'telegram:default:1')['id'] == first['id']
    root_db.assert_called_once()
    admin.finish(first['id'], 'updated', before='1.0.0', after='1.0.1')
    assert {row['profile'] for row in admin.results()} == {'default', 'webops'}
    admin.main(['ack', 'telegram:default:1'])
    assert [row['profile'] for row in admin.results()] == ['webops']
    assert admin.start('codex', 'default', 'telegram:default:1')['status'] == 'updated'
    root_db.assert_called_once()


def test_concurrent_update_requests_launch_one_worker(root_db):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda n: admin.start('codex', 'webops', 'parallel:' + str(n)), range(8)))
    assert len({row['id'] for row in rows}) == 1
    root_db.assert_called_once()


@pytest.mark.parametrize('args', [
    ['update', 'codex;id', 'default', 'a'], ['update', 'codex', 'stranger', 'a'],
    ['update', 'grok', 'webops', '../../a'], ['update', 'codex', 'webops', 'a', '--force'],
    ['inventory', '/root/auth.json'], ['execute', 'codex'], ['ack', '../a'],
])
def test_privileged_boundary_rejects_unscoped_input(args, root_db):
    with pytest.raises(ValueError):
        admin.main(args)
    root_db.assert_not_called()


def test_inventory_uses_fixed_service_outside_bot_mount_namespace(monkeypatch, capsys):
    launch = Mock(return_value='{"installed":{},"python":{},"apt":{}}')
    monkeypatch.setattr(admin, 'capture', launch)
    admin.main(['inventory'])
    argv = launch.call_args.args[0]
    assert argv[:5] == ['/usr/bin/systemd-run', '--quiet', '--wait', '--pipe', '--collect']
    assert '--property=User=root' in argv
    assert argv[-4:] == ['-I', '-m', 'pylon.admin.software', 'inventory-worker']
    assert json.loads(capsys.readouterr().out)['installed'] == {}


@pytest.mark.parametrize('busy_sequence', [[[77]], [[], [88]]])
def test_session_occupancy_prevents_native_update(root_db, monkeypatch, busy_sequence):
    row = admin.start('grok', 'webops', 'one')
    monkeypatch.setattr(admin, 'installed', lambda _: '1.0.44')
    monkeypatch.setattr(admin, 'busy_processes', Mock(side_effect=busy_sequence))
    monkeypatch.setattr(admin, 'latest', lambda *_: ('1.0.45', 'official'))
    native = Mock(side_effect=AssertionError('must not install'))
    monkeypatch.setattr(admin, 'native_update', native)
    admin.execute('grok')
    assert get_run(row['id'])['status'] == 'busy'
    native.assert_not_called()


@pytest.mark.parametrize('after,exit_code,status', [('1.0.45', 0, 'updated'), ('1.0.44', 0, 'failed'), ('1.0.45', 1, 'failed')])
def test_success_requires_exit_and_real_version(root_db, monkeypatch, after, exit_code, status):
    row = admin.start('grok', 'webops', 'one')
    monkeypatch.setattr(admin, 'installed', Mock(side_effect=['1.0.44', after]))
    monkeypatch.setattr(admin, 'busy_processes', lambda _: [])
    monkeypatch.setattr(admin, 'latest', lambda *_: ('1.0.45', 'official'))
    native = Mock(return_value=(exit_code, 'installer output'))
    monkeypatch.setattr(admin, 'native_update', native)
    admin.execute('grok')
    assert get_run(row['id'])['status'] == status
    admin.execute('grok')
    native.assert_called_once_with('grok')


def test_latest_installed_runs_native_noop_and_reports_current(root_db, monkeypatch):
    row = admin.start('grok', 'webops', 'one')
    monkeypatch.setattr(admin, 'installed', lambda _: '1.0.44')
    monkeypatch.setattr(admin, 'busy_processes', lambda _: [])
    monkeypatch.setattr(admin, 'latest', lambda *_: ('1.0.44', 'official'))
    native = Mock(return_value=(0, 'Already up to date'))
    monkeypatch.setattr(admin, 'native_update', native)
    admin.execute('grok')
    native.assert_called_once()
    assert get_run(row['id'])['status'] == 'current'


def test_timeout_is_uncertain_and_never_replayed(root_db, monkeypatch):
    row = admin.start('grok', 'webops', 'one')
    monkeypatch.setattr(admin, 'installed', lambda _: '1.0.44')
    monkeypatch.setattr(admin, 'busy_processes', lambda _: [])
    monkeypatch.setattr(admin, 'latest', lambda *_: ('1.0.45', 'official'))
    native = Mock(side_effect=TimeoutError('timeout'))
    monkeypatch.setattr(admin, 'native_update', native)
    admin.execute('grok')
    assert get_run(row['id'])['status'] == 'uncertain'
    admin.execute('grok')
    native.assert_called_once()


def test_upstream_check_failure_does_not_install(root_db, monkeypatch):
    row = admin.start('grok', 'webops', 'one')
    monkeypatch.setattr(admin, 'installed', lambda _: '1.0.44')
    monkeypatch.setattr(admin, 'busy_processes', lambda _: [])
    monkeypatch.setattr(admin, 'latest', Mock(side_effect=RuntimeError('HTTP 429')))
    native = Mock()
    monkeypatch.setattr(admin, 'native_update', native)
    admin.execute('grok')
    assert get_run(row['id'])['status'] == 'failed'
    native.assert_not_called()


def test_dead_service_result_is_uncertain(root_db):
    row = admin.start('codex', 'default', 'one')
    with admin.database() as db:
        db.execute("UPDATE runs SET created_at=0,status='running' WHERE id=?", (row['id'],))
    assert admin.results()[0]['status'] == 'uncertain'
    root_db.assert_called_once()


def test_native_command_is_fixed_sudo_and_stable(monkeypatch):
    process = Mock(returncode=0)
    process.communicate.return_value = ('ok', None)
    popen = Mock(return_value=process)
    monkeypatch.setattr(admin.subprocess, 'Popen', popen)
    assert admin.native_update('grok') == (0, 'ok')
    assert popen.call_args.args[0] == ['/usr/bin/sudo', '-n', '-H', '--', '/usr/local/bin/grok', 'update', '--stable']
    assert not popen.call_args.kwargs.get('shell')


def test_process_check_ignores_browser_and_unrelated_services(tmp_path):
    for pid, executable in [(21, '/root/.grok/downloads/grok-1.0.44-linux-x86_64'),
                            (22, '/root/.grok/linux-runtime/chrome-linux64/chrome'),
                            (23, '/root/.codex/packages/standalone/releases/version/bin/codex'),
                            (24, '/usr/local/bin/nexushub-webd')]:
        directory = tmp_path / str(pid)
        directory.mkdir()
        (directory / 'exe').symlink_to(executable)
    assert admin.busy_processes('grok', tmp_path) == [21]
    assert admin.busy_processes('codex', tmp_path) == [23]


def test_result_recovery_enqueues_once_before_ack(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.db')
    row = dict(id='run', request_id='one', profile='webops', target='codex', status='updated',
               before_version='1.0.0', after_version='1.0.1')
    fail_ack = True

    def call(*args):
        if args[1] == 'results':
            return json.dumps([row])
        if fail_ack:
            raise RuntimeError('connection interrupted')
        return '{}'
    monkeypatch.setattr(updates.scheduler, 'admin', call)
    with pytest.raises(RuntimeError):
        updates.reconcile(box)
    assert box.counts() == {'pending': 1}
    fail_ack = False
    updates.reconcile(box)
    assert box.counts() == {'pending': 1}


def test_update_routes_do_not_accept_arguments(monkeypatch):
    install = Mock(return_value='ok')
    monkeypatch.setattr(updates, 'request_update', install)
    for command in ['/update_codex --force', '/update_grok ;reboot']:
        with pytest.raises(ValueError):
            commands.handle('default', command)
    install.assert_not_called()
    assert commands.handle('webops', '/update_grok') == 'ok'
    install.assert_called_once_with('grok', 'webops')


def test_inventory_optional_argument_is_read_only(monkeypatch):
    read = Mock(return_value='list')
    monkeypatch.setattr(updates, 'inventory_text', read)
    assert commands.handle('default', '/updates') == 'list'
    assert commands.handle('default', '/updates all') == 'list'
    assert '用法：' in commands.handle('default', '/updates upgrade')
    assert [x.kwargs for x in read.call_args_list] == [{'full': False}, {'full': True}]


def test_stable_and_branch_selection():
    def respond(request):
        if 'postgresql.org' in request.url.host:
            return httpx.Response(200, text='<rss><channel><item><title>19.0</title></item><item><title>18.6</title><link>https://example.test/18.6</link></item></channel></rss>')
        if '/redis/' in request.url.path:
            return httpx.Response(200, json=[{'tag_name': '8.10.2', 'html_url': 'new'},
                {'tag_name': '8.6.8-rc1', 'prerelease': True, 'html_url': 'rc'}, {'tag_name': '8.6.7', 'html_url': 'stable'}])
        return httpx.Response(200, json={'tag_name': 'v2.0.0-rc1', 'prerelease': True, 'html_url': 'rc'})
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        assert inventory.latest('postgres', '18', http)[0] == '18.6'
        assert inventory.latest('redis', '8.6', http) == ('8.6.7', 'stable')
        with pytest.raises(ValueError, match='非稳定'):
            inventory.latest('github', 'owner/repo', http)


def test_infrastructure_filters_and_intervals():
    sources = {s['id']: monitor.normalize_source(s) for s in infrastructure.SOURCES}
    assert len(sources) == 7
    assert all(s['poll_interval_seconds'] == 3600 for s in sources.values())
    postgres = sources['infra_postgresql_18_releases']
    redis = sources['infra_redis_86_releases']
    for source, yes, no in [(postgres, '18.6', '19.0'), (redis, '8.6.7', '8.10.2')]:
        assert monitor.item_allowed_by_title_filters(source, {'title': yes})
        assert not monitor.item_allowed_by_title_filters(source, {'title': no})
    assert not monitor.due_for_source({'next_poll_at': 3600}, 3599)
    assert monitor.due_for_source({'next_poll_at': 3600}, 3600)
    assert not monitor.due_for_source({'next_poll_at': 3600, 'backoff_until': 4000}, 3601)
    assert monitor.due_for_source({}, 1)
    assert monitor.item_allowed_by_title_filters(sources['infra_easy_proxies_releases'], {'title': 'v3.1.1 - 修复冷却时间'})
    assert not monitor.item_allowed_by_title_filters(sources['infra_easy_proxies_releases'], {'title': 'v3.2.0-rc1'})


def test_registration_preserves_disabled_sources_and_existing_state(monkeypatch):
    disabled = infrastructure.SOURCES[0]['id']
    existing = infrastructure.SOURCES[1]
    monkeypatch.setattr(monitor, 'load_custom_sources_config', lambda: {'sources': [existing], 'disabled_source_ids': [disabled, 'hermes_agent_releases']})
    add, baseline = Mock(), Mock(return_value={'failed': 0})
    monkeypatch.setattr(monitor, 'add_custom_sources', add)
    monkeypatch.setattr(monitor, 'baseline_sources', baseline)
    monkeypatch.setattr(monitor, 'validate_custom_sources_config', lambda: {'errors': []})
    assert infrastructure.install()['added'] == 5
    assert all(s['id'] not in {disabled, existing['id']} for s in add.call_args.args[0])
    assert baseline.call_args.args[0] == add.call_args.args[0]


def test_weekly_digest_persists_notification_and_keeps_noop_silent(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.db')
    monkeypatch.setattr(maintenance, 'Outbox', lambda: box)
    monkeypatch.setattr(maintenance, 'render', lambda _: '维护事项')
    monkeypatch.setenv('PYLON_RUN_ID', 'weekly:one')
    monkeypatch.setenv('PYLON_TRIGGER', 'scheduled')
    snapshot = {'checked_at': 1, 'apt': {'packages': [], 'index_time': time.time()}, 'rows': []}
    monkeypatch.setattr(maintenance, 'check', lambda **_: snapshot)
    maintenance.main()
    assert box.counts() == {}
    snapshot['rows'].append({'available': True})
    maintenance.main()
    maintenance.main()
    assert box.counts() == {'pending': 1}
    assert box.run_events('weekly:one')[0]['profile'] == 'webops'


def test_inventory_render_separates_failed_check_and_distribution_versions():
    data = {'checked_at': 1, 'apt': {'packages': [], 'index_time': 1, 'reboot_required': False},
            'rows': [{'name': '工具 A', 'version': '1.0.0', 'error': 'HTTP 429'},
                     {'name': 'Docker', 'version': '29.1.3', 'latest': '29.8.1', 'available': True, 'note': 'Ubuntu 软件源'}]}
    text = inventory.render(data)
    assert '检查未完成：工具 A' in text and 'HTTP 429' in text
    assert 'Ubuntu 软件源' in text and '超过 48 小时' in text
