import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylon import scheduler, software_inventory as inventory
from pylon.admin import software as admin
from pylon.outbox import Outbox


@pytest.fixture
def container(monkeypatch):
    identity = ('sha256:' + 'a' * 64, 123, '2026-09-29', 1, 2)
    data = {'Mounts': [], 'Config': {'Labels': {'org.opencontainers.image.version': '0.1.123'}}}
    snapshot = Mock(return_value=(identity, data))
    monkeypatch.setattr(admin, 'container_snapshot', snapshot)
    return identity, data, snapshot


def test_sub2api_online_update_uses_program_instead_of_old_image_label(container, monkeypatch):
    output = ('2026-09-30T09:33:52.592+0800 INFO stdlog '
              'Sub2API 0.2.10 (commit: abc, built: 2026-09-29T08:21:03Z)')
    capture = Mock(return_value=output)
    monkeypatch.setattr(admin, 'capture', capture)
    assert admin.container_version('sub2api') == {'version': '0.2.10', 'basis': '运行程序自报'}
    capture.assert_called_once_with(['/usr/bin/docker', 'exec', 'sub2api', '/app/sub2api', '--version'],
                                    timeout=8, include_stderr=True)


def test_version_probe_includes_stderr_used_by_sub2api_logger(monkeypatch):
    run = Mock(return_value=SimpleNamespace(stdout='', stderr='Sub2API 0.2.10 (commit: abc)\n'))
    monkeypatch.setattr(admin.subprocess, 'run', run)
    text = admin.capture(['version-only'], include_stderr=True)
    assert admin.probe_version('sub2api', text) == '0.2.10'
    assert run.call_args.kwargs['check'] is True


def test_failed_program_version_never_falls_back_to_image_label(container, monkeypatch):
    capture = Mock(return_value='Go version go1.27.0; failed to read application version')
    monkeypatch.setattr(admin, 'capture', capture)
    with pytest.raises(ValueError, match='sub2api.*Version'):
        admin.container_version('sub2api')
    capture.assert_called_once()


def test_changed_container_during_probe_is_not_reported_as_current(container, monkeypatch):
    identity, data, snapshot = container
    snapshot.side_effect = [(identity, data), (identity[:-1] + (99,), data)]
    monkeypatch.setattr(admin, 'capture', Mock(return_value='Sub2API 0.2.10'))
    with pytest.raises(ValueError, match='检查期间容器发生变化'):
        admin.container_version('sub2api')


def test_snapshot_rejects_replaced_binary_still_running_old_inode(monkeypatch):
    data = {'Image': 'image', 'State': {'Running': True, 'Pid': 123, 'StartedAt': 'today'}}
    monkeypatch.setattr(admin, 'capture', lambda _: json.dumps([data]))
    monkeypatch.setattr(admin.Path, 'samefile', lambda *_: False)
    with pytest.raises(ValueError, match='运行程序与容器内文件不同'):
        admin.container_snapshot('sub2api')


@pytest.mark.parametrize('target,output,expected', [
    ('codex', 'codex-cli 0.159.1', '0.159.1'),
    ('grok', 'grok 1.0.44 (5b807183dd79) [stable]', '1.0.44'),
    ('openlist', 'Go Version: go1.27.0\nVersion: v4.2.6\nWebVersion: v4.2.5', '4.2.6'),
    ('sing-box', 'Environment: go1.26.8\nsing-box version 1.14.2', '1.14.2'),
    ('nexushub', 'nexushub-webd 1.2.3', '1.2.3'),
    ('certbot', 'certbot 5.7.0', '5.7.0'),
    ('npm', '11.19.1\n', '11.19.1'),
    ('pnpm', '11.26.0\n', '11.26.0'),
    ('corepack', '0.35.0', '0.35.0'),
    ('uv', 'uv 0.12.10 (x86_64-unknown-linux-gnu)', '0.12.10'),
    ('rsync', 'rsync  version 3.4.3  protocol version 32', '3.4.3'),
    ('chrome', 'Google Chrome for Testing 152.0.7977.82 ', '152.0.7977.82'),
    ('docker', '29.1.3', '29.1.3'),
    ('postgres', 'postgres (PostgreSQL) 18.3', '18.3'),
    ('redis', 'Redis server v=8.6.3 sha=00000000:1 malloc=jemalloc-5.3.0', '8.6.3'),
    ('frp', '0.69.0', '0.69.0'),
])
def test_version_formats_observed_in_production(target, output, expected):
    assert admin.probe_version(target, output) == expected
    with pytest.raises(ValueError):
        admin.probe_version(target, 'Go Version: go1.27.0\nWebVersion: v4.2.5')


@pytest.mark.parametrize('upgradable,candidate', [
    (False, '29.1.3-0ubuntu3~24.04.2'), (True, '29.1.3-0ubuntu3~24.04.3'),
])
def test_docker_uses_apt_candidate_and_discards_old_cached_upstream_result(tmp_path, monkeypatch, upgradable, candidate):
    box = Outbox(tmp_path / 'outbox.db')
    box.put('software_inventory', {'checked_at': time.time(), 'rows': [{'id': 'docker', 'available': True, 'latest': '29.8.1'}]})
    monkeypatch.setattr(inventory, 'Outbox', lambda: box)
    monkeypatch.setattr(inventory, 'SOFTWARE', {'docker': inventory.SOFTWARE['docker']})
    package = {'installed': '29.1.3-0ubuntu3~24.04.2', 'candidate': candidate, 'upgradable': upgradable}
    local = {'installed': {'docker': {'version': '29.1.3'}}, 'python': {},
             'apt': {'packages': [], 'managed': {'docker.io': package}, 'index_time': time.time(), 'reboot_required': False}}
    read = Mock(return_value=json.dumps(local))
    monkeypatch.setattr(scheduler, 'admin', read)
    upstream = Mock(side_effect=AssertionError('Docker upgrades must not use upstream GitHub versions'))
    monkeypatch.setattr(inventory, 'latest', upstream)
    checked = inventory.check()
    row = checked['rows'][0]
    assert checked['schema'] == inventory.CACHE_SCHEMA
    assert row['available'] is upgradable and row['latest'] == candidate
    assert ('Docker（Ubuntu' in inventory.render(checked)) is upgradable
    assert 'Docker（Ubuntu' in inventory.render(checked, full=True)
    assert inventory.check() == checked
    read.assert_called_once()
    upstream.assert_not_called()


def test_missing_docker_package_is_unknown_instead_of_no_updates(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.db')
    monkeypatch.setattr(inventory, 'Outbox', lambda: box)
    monkeypatch.setattr(inventory, 'SOFTWARE', {'docker': inventory.SOFTWARE['docker']})
    local = {'installed': {'docker': {'version': '29.1.3'}}, 'python': {},
             'apt': {'packages': [], 'index_time': time.time(), 'reboot_required': False}}
    monkeypatch.setattr(scheduler, 'admin', lambda *_, **__: json.dumps(local))
    row = inventory.check()['rows'][0]
    assert 'error' in row and 'available' not in row


@pytest.mark.parametrize('keeps_updating', [False, True])
def test_update_finishing_during_collection_cannot_restore_stale_cache(tmp_path, monkeypatch, keeps_updating):
    box = Outbox(tmp_path / 'outbox.db')
    monkeypatch.setattr(inventory, 'Outbox', lambda: box)
    monkeypatch.setattr(inventory, 'SOFTWARE', {'codex': inventory.SOFTWARE['codex']})
    local = {'installed': {'codex': {'version': '0.159.1'}}, 'python': {},
             'apt': {'packages': [], 'index_time': time.time(), 'reboot_required': False}}
    read = Mock(side_effect=lambda *_, **__: json.dumps(local))
    monkeypatch.setattr(scheduler, 'admin', read)
    calls = 0

    def latest(*_):
        nonlocal calls
        calls += 1
        if calls == 1 or keeps_updating:
            local['installed']['codex']['version'] = '0.159.2'
            inventory.invalidate(box)
        return '0.159.2', 'official'

    monkeypatch.setattr(inventory, 'latest', latest)
    if keeps_updating:
        with pytest.raises(RuntimeError, match='检查期间软件多次更新'):
            inventory.check()
        assert box.get('software_inventory') is None
    else:
        checked = inventory.check()
        assert checked['rows'][0]['version'] == '0.159.2'
        assert checked['rows'][0]['available'] is False
        assert box.get('software_inventory') == checked
    assert read.call_count == 2
