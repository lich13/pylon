import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pylon.admin import apt_guard, software as admin


MESA = [
    'libegl-mesa0',
    'libgbm1',
    'libgl1-mesa-dri',
    'libglx-mesa0',
    'mesa-libgallium',
]
APT_PACKAGES = MESA + ['sosreport', 'thermald']


def item(package, before='1.0', candidate='1.1'):
    return {'package': package, 'before': before, 'candidate': candidate}


def apt_simulation(items, extra=()):
    rows = list(items) + list(extra)
    lines = [f"Inst {row['package']} [{row['before']}] ({row['candidate']} Ubuntu:24.04/stable)"
             for row in rows]
    lines.append(f'{len(rows)} upgraded, 0 newly installed, 0 to remove.')
    return '\n'.join(lines)


def get_run(run_id):
    with admin.database() as db:
        row = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        return dict(row) if row else None


@pytest.fixture
def privileged_root(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, 'ROOT', tmp_path / 'privileged')
    launcher = Mock(return_value='')
    monkeypatch.setattr(admin, 'capture', launcher)
    return launcher


def _capture_for_worker(*, held='', audit='', thermald='active'):
    def capture(argv, timeout=15, **kwargs):
        if argv[:2] == ['/usr/bin/apt-mark', 'showhold']:
            return held
        if argv[:2] == ['/usr/bin/dpkg', '--audit']:
            return audit
        if argv[:3] == ['/usr/bin/systemctl', 'is-active', 'thermald']:
            return thermald
        return ''

    return capture


def _patch_install_worker(monkeypatch, items, *, held='', audit='', thermald='active', phase=None,
                          after=None, install=None):
    by_package = {row['package']: row for row in items}
    calls = {row['package']: 0 for row in items}
    after = after or {}

    def version(package):
        index = calls[package]
        calls[package] += 1
        if index == 0:
            return by_package[package]['before']
        return after.get(package, by_package[package]['candidate'])

    monkeypatch.setattr(admin, '_apt_version', version)
    monkeypatch.setattr(admin, '_apt_candidate', lambda package: by_package[package]['candidate'])
    monkeypatch.setattr(admin, '_apt_phase_candidate',
                        lambda package: phase.get(package, by_package[package]['candidate'])
                        if isinstance(phase, dict) else
                        (phase if phase is not None else by_package[package]['candidate']))
    monkeypatch.setattr(admin, '_apt_newer', lambda candidate, installed: True)
    monkeypatch.setattr(admin, '_apt_simulation',
                        lambda packages: apt_simulation([by_package[p] for p in
                                                         ([packages] if isinstance(packages, str) else packages)]))
    monkeypatch.setattr(admin, 'capture', _capture_for_worker(held=held, audit=audit, thermald=thermald))
    install = install or Mock(return_value=SimpleNamespace(returncode=0, stdout='', stderr=''))
    monkeypatch.setattr(admin.subprocess, 'run', install)
    return install, calls


def test_apt_simulation_rejects_single_mesa_with_four_implicit_packages():
    selected = [item(MESA[0])]
    extra = [item(package) for package in MESA[1:]]
    safe, reason = admin._apt_simulation_safe(apt_simulation(selected, extra), [MESA[0]])
    assert not safe
    assert '关联包' in reason


@pytest.mark.parametrize('packages', [MESA, APT_PACKAGES])
def test_apt_simulation_accepts_exact_five_or_seven_package_sets(packages):
    selected = [item(package) for package in packages]
    assert admin._apt_simulation_safe(apt_simulation(selected), packages) == (True, '')


def test_apt_simulation_rejects_foreign_arch_and_duplicate_inst_actions():
    foreign = apt_simulation([item('sosreport:i386')])
    duplicate = apt_simulation([item('sosreport'), item('sosreport')])
    for output in (foreign, duplicate):
        safe, reason = admin._apt_simulation_safe(output, ['sosreport'])
        assert not safe
        assert reason


@pytest.mark.parametrize('output', [
    '1 upgraded, 1 newly installed, 0 to remove.\nInst sosreport [1.0] (1.1 Ubuntu:24.04/stable)',
    '1 upgraded, 0 newly installed, 1 to remove.\nInst sosreport [1.0] (1.1 Ubuntu:24.04/stable)\nRemv evil',
    '1 upgraded, 0 newly installed, 0 to remove.\nautoremove would remove evil',
    'The following packages have been kept back:\n  sosreport',
    'sosreport deferred due to phasing',
])
def test_apt_simulation_rejects_new_remove_autoremove_held_or_phased(output):
    safe, reason = admin._apt_simulation_safe(output, ['sosreport'])
    assert not safe
    assert reason


def test_apt_simulation_allows_passive_autoremove_advice():
    output = apt_simulation([item('sosreport')]) + "\nUse 'sudo apt autoremove' to remove them."
    assert admin._apt_simulation_safe(output, ['sosreport']) == (True, '')


def test_apt_simulation_rejects_unselected_configure_action():
    output = apt_simulation([item('sosreport')]) + '\nConf libgbm1 (1.1)'
    safe, reason = admin._apt_simulation_safe(output, ['sosreport'])
    assert not safe
    assert '配置' in reason


def test_apt_guard_accepts_version_two_exact_install_plan():
    items = [item('libegl-mesa0'), item('sosreport')]
    text = 'VERSION 2\n\n' + '\n'.join(
        f"{row['package']} {row['before']} < {row['candidate']} /var/cache/apt/archives/{row['package']}_{row['candidate']}.deb"
        for row in items
    )
    assert apt_guard.validate_actions(text, items) is None


@pytest.mark.parametrize('text', [
    'VERSION 1\n\nlibegl-mesa0 1.0 < 1.1 /tmp/libegl-mesa0.deb',
    'VERSION 2\nlibegl-mesa0 1.0 < 1.1 /tmp/libegl-mesa0.deb',
    'VERSION 2\n\nlibegl-mesa0 1.0 < 1.1 /tmp/libegl-mesa0.deb\nevil 1.0 < 1.1 /tmp/evil.deb',
    'VERSION 2\n\nlibegl-mesa0 9.0 < 1.1 /tmp/libegl-mesa0.deb',
    'VERSION 2\n\nlibegl-mesa0 1.0 < 1.1 **REMOVE**',
    'VERSION 2\n\nlibegl-mesa0 1.0 < 1.1 **CONFIGURE**',
])
def test_apt_guard_rejects_unsupported_or_incomplete_plan(text):
    with pytest.raises(ValueError):
        apt_guard.validate_actions(text, [item('libegl-mesa0')])


def test_start_apt_batch_deduplicates_request_and_freezes_items(privileged_root):
    items = [item('sosreport'), item('libegl-mesa0')]
    request_id = 'software-batch:' + 'a' * 24
    first = admin.start_apt_batch(items, 'default', request_id)
    second = admin.start_apt_batch(list(reversed(items)), 'default', request_id)
    assert first['id'] == second['id']
    assert first['target'] == 'apt-batch:' + first['id']
    assert json.loads(first['items']) == sorted(items, key=lambda row: row['package'])
    assert privileged_root.call_count == 1
    with pytest.raises(ValueError, match='不能修改'):
        admin.start_apt_batch([item('sosreport', candidate='1.2'), item('libegl-mesa0')],
                              'default', request_id)


@pytest.mark.parametrize('items,profile,request_id', [
    ([], 'default', 'software-batch:' + 'a' * 24),
    ([item('sosreport') for _ in range(21)], 'default', 'software-batch:' + 'a' * 24),
    ([item('frp')], 'default', 'software-batch:' + 'a' * 24),
    ([item('sosreport'), item('sosreport')], 'default', 'software-batch:' + 'a' * 24),
    ([{'package': 'sosreport', 'before': '1.0/bad', 'candidate': '1.1'}],
     'default', 'software-batch:' + 'a' * 24),
    ([item('sosreport')], 'webops', 'software-batch:' + 'a' * 24),
    ([item('sosreport')], 'default', 'software-batch:' + 'g' * 24),
])
def test_start_apt_batch_rejects_invalid_scope_or_request(privileged_root, items, profile, request_id):
    with pytest.raises(ValueError):
        admin.start_apt_batch(items, profile, request_id)
    privileged_root.assert_not_called()


def test_apt_batch_marks_changed_installed_version_stale(privileged_root, monkeypatch):
    row = admin.start_apt_batch([item('sosreport', before='4.8.0', candidate='4.8.1')],
                                'default', 'software-batch:' + 'a' * 24)
    monkeypatch.setattr(admin, '_apt_version', lambda _: '4.8.2')
    monkeypatch.setattr(admin, '_apt_candidate', lambda _: '4.8.1')
    simulation = Mock(side_effect=AssertionError('stale candidate must not simulate or install'))
    monkeypatch.setattr(admin, '_apt_simulation', simulation)
    admin.apt_batch(row['id'])
    result = get_run(row['id'])
    assert result['status'] == 'stale'
    simulation.assert_not_called()


def test_apt_batch_phase_mismatch_defers_without_install(privileged_root, monkeypatch):
    items = [item('sosreport')]
    row = admin.start_apt_batch(items, 'default', 'software-batch:' + 'a' * 24)
    install, _ = _patch_install_worker(monkeypatch, items, phase='1.2')
    admin.apt_batch(row['id'])
    assert get_run(row['id'])['status'] == 'deferred'
    install.assert_not_called()


def test_apt_batch_hold_defers_without_install(privileged_root, monkeypatch):
    items = [item('sosreport')]
    row = admin.start_apt_batch(items, 'default', 'software-batch:' + 'a' * 24)
    install, _ = _patch_install_worker(monkeypatch, items, held='sosreport')
    admin.apt_batch(row['id'])
    assert get_run(row['id'])['status'] == 'deferred'
    install.assert_not_called()


def test_apt_batch_dpkg_audit_blocks_install(privileged_root, monkeypatch):
    items = [item('sosreport')]
    row = admin.start_apt_batch(items, 'default', 'software-batch:' + 'a' * 24)
    install, _ = _patch_install_worker(monkeypatch, items, audit='sosreport: half-configured')
    admin.apt_batch(row['id'])
    result = get_run(row['id'])
    assert result['status'] == 'failed'
    assert 'dpkg' in result['detail']
    install.assert_not_called()


@pytest.mark.parametrize('packages', [MESA, APT_PACKAGES])
def test_apt_batch_exact_package_set_installs_once_with_guard(privileged_root, monkeypatch, packages):
    items = [item(package) for package in packages]
    row = admin.start_apt_batch(items, 'default', 'software-batch:' + 'a' * 24)
    install, _ = _patch_install_worker(monkeypatch, items)
    admin.apt_batch(row['id'])
    assert get_run(row['id'])['status'] == 'updated'
    install.assert_called_once()
    command = install.call_args.args[0]
    assert '--no-remove' in command
    assert 'APT::Get::Phase-Policy=true' in command
    assert any(value.startswith('DPkg::Tools::Options::/srv/pylon/venv/bin/python::Version=2')
               for value in command)
    assert command[-len(packages):] == [f"{row['package']}={row['candidate']}" for row in items]
    assert '-s' not in command
    assert 'upgrade' not in command


def test_apt_batch_thermald_health_failure_is_failed(privileged_root, monkeypatch):
    items = [item('thermald')]
    row = admin.start_apt_batch(items, 'default', 'software-batch:' + 'a' * 24)
    install, _ = _patch_install_worker(monkeypatch, items, thermald='inactive')
    admin.apt_batch(row['id'])
    result = get_run(row['id'])
    assert result['status'] == 'failed'
    assert 'thermald' in result['detail']
    install.assert_called_once()


def test_apt_batch_timeout_is_uncertain_and_never_replayed(privileged_root, monkeypatch):
    items = [item('sosreport')]
    row = admin.start_apt_batch(items, 'default', 'software-batch:' + 'a' * 24)
    install = Mock(side_effect=TimeoutError('install timeout'))
    _patch_install_worker(monkeypatch, items, install=install)
    admin.apt_batch(row['id'])
    assert get_run(row['id'])['status'] == 'uncertain'
    admin.apt_batch(row['id'])
    install.assert_called_once()


from pylon import inline, software_batch, software_updates as updates
from pylon.outbox import Outbox


@pytest.fixture
def ui_box(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.sqlite3')
    env = {'TELEGRAM_BOT_TOKEN': 'fixture-token', 'TELEGRAM_HOME_CHANNEL': '123'}
    monkeypatch.setattr(software_batch, 'Outbox', lambda: box)
    monkeypatch.setattr(updates, 'Outbox', lambda: box)
    monkeypatch.setattr(software_batch, 'profile_env', lambda profile: env)
    monkeypatch.setattr(updates, 'profile_env', lambda profile: env)
    monkeypatch.setattr(inline, 'profile_env', lambda profile: env)
    return box


def seed_candidates(box, packages=('sosreport', 'thermald'), candidate='1.1'):
    snapshot = {'apt': {'packages': [
        {'name': package, 'installed': '1.0', 'candidate': candidate}
        for package in packages
    ]}}
    candidates = updates.eligible_candidates(snapshot)
    return candidates, software_batch.current_candidates(box, candidates)


def record_card(box, markup, message_id=88, chat_id='123'):
    event_id = box.enqueue(
        profile='default', job_id='software-update-review', kind='text',
        payload={'text': 'software review card', 'reply_markup': markup},
        key='software-card:' + str(message_id),
    )
    box.finish(event_id, 'sent', receipt=[{'message_id': message_id, 'chat_id': str(chat_id)}])
    return message_id


def payload(data, message_id=88, chat_id=123, sender_id=123):
    return {
        'id': 'callback-1', 'data': data,
        'from': {'id': sender_id, 'is_bot': False},
        'message': {'message_id': message_id, 'chat': {'id': chat_id, 'type': 'private'}},
    }


def button(markup, text):
    return next(button for line in markup['inline_keyboard'] for button in line if button['text'] == text)


def test_batch_keyboard_select_all_clear_and_callback_ids(ui_box):
    _, rows = seed_candidates(ui_box)
    markup = software_batch.keyboard(ui_box, rows)
    select_data = markup['inline_keyboard'][0][0]['callback_data']
    assert select_data.startswith('su2|select|')
    all_data = button(markup, '全选当前候选')['callback_data']
    clear_data = button(markup, '清空选择')['callback_data']
    record_card(ui_box, markup)

    software_batch.callback('default', payload(all_data))
    assert len(ui_box.software_selection()) == 2
    software_batch.callback('default', payload(all_data))
    assert len(ui_box.software_selection()) == 2
    software_batch.callback('default', payload(clear_data))
    assert ui_box.software_selection() == []


def test_batch_callback_requires_original_message_and_valid_signature(ui_box):
    _, rows = seed_candidates(ui_box, packages=('sosreport',))
    markup = software_batch.keyboard(ui_box, rows)
    valid = markup['inline_keyboard'][0][0]['callback_data']
    record_card(ui_box, markup)
    with pytest.raises(ValueError, match='原始消息'):
        software_batch.callback('default', payload(valid, message_id=89))

    bad = valid.rsplit('|', 1)[0] + '|000000000000'
    bad_markup = {'inline_keyboard': [[{'text': 'bad', 'callback_data': bad}]]}
    record_card(ui_box, bad_markup, message_id=90)
    with pytest.raises(ValueError, match='签名'):
        software_batch.callback('default', payload(bad, message_id=90))


def test_batch_selection_and_run_are_idempotent(ui_box, monkeypatch):
    candidates, rows = seed_candidates(ui_box)
    markup = software_batch.keyboard(ui_box, rows)
    record_card(ui_box, markup)
    for row in rows:
        data = software_batch.callback_data('default', 'select', row['id'])
        software_batch.callback('default', payload(data))
    software_batch.callback('default', payload(
        software_batch.callback_data('default', 'select', rows[0]['id'])))
    assert {row['package_name'] for row in ui_box.software_selection()} == {'sosreport', 'thermald'}

    run_markup = software_batch.keyboard(ui_box)
    run_button = next(button for line in run_markup['inline_keyboard'] for button in line
                      if button['text'].startswith('批量更新已选'))
    message_id = record_card(ui_box, run_markup, message_id=91)
    root_row = {'id': 'b' * 32, 'target': 'apt-batch:' + 'b' * 32, 'status': 'queued'}
    admin_call = Mock(return_value=json.dumps(root_row))
    monkeypatch.setattr(software_batch.scheduler, 'admin', admin_call)
    result = updates.callback('default', payload(run_button['callback_data'], message_id=message_id))
    assert '重复点击不会再次安装' in result['text']
    updates.callback('default', payload(run_button['callback_data'], message_id=message_id))
    admin_call.assert_called_once()
    assert ui_box.software_batches()[0]['status'] == 'queued'
    wire = json.loads(admin_call.call_args.args[-1])
    assert {row['package'] for row in wire} == {row['package_name'] for row in candidates}


def test_skipping_old_candidate_allows_new_version_card(ui_box):
    candidates, rows = seed_candidates(ui_box, packages=('sosreport',), candidate='1.1')
    markup = software_batch.keyboard(ui_box, rows)
    record_card(ui_box, markup)
    skip = software_batch.callback_data('default', 'skip', candidates[0]['id'])
    software_batch.callback('default', payload(skip))
    assert ui_box.software_review(candidates[0]['id'])['status'] == 'skipped'

    new_candidates, new_rows = seed_candidates(ui_box, packages=('sosreport',), candidate='1.2')
    assert new_candidates[0]['id'] != candidates[0]['id']
    assert new_rows[0]['status'] == 'pending'
    assert new_rows[0]['available'] == 1
    assert ui_box.software_review(candidates[0]['id'])['available'] == 0


def test_batch_result_is_enqueued_once(ui_box, monkeypatch):
    _, rows = seed_candidates(ui_box, packages=('sosreport',))
    ui_box.update_software_review(rows[0]['id'], selected=1, selection_epoch=1)
    draft_markup = software_batch.keyboard(ui_box)
    run_button = next(button for line in draft_markup['inline_keyboard'] for button in line
                      if button['text'].startswith('批量更新已选'))
    root_row = {'id': 'c' * 32, 'target': 'apt-batch:' + 'c' * 32, 'status': 'queued'}
    monkeypatch.setattr(software_batch.scheduler, 'admin', Mock(return_value=json.dumps(root_row)))
    key = run_button['callback_data'].split('|')[2]
    software_batch.submit(ui_box, key)
    batch = ui_box.software_batches()[0]
    result = root_row | {
        'status': 'updated', 'request_id': batch['request_id'], 'detail': 'done',
        'item_results': json.dumps([{
            'package': 'sosreport', 'before': '1.0', 'candidate': '1.1',
            'after': '1.1', 'status': 'updated',
        }]),
    }
    assert software_batch.sync_result(ui_box, result)
    assert software_batch.sync_result(ui_box, result)
    with ui_box.connect() as db:
        count = db.execute("SELECT count(*) FROM events WHERE job_id='software-update'").fetchone()[0]
    assert count == 1
    assert ui_box.software_batches()[0]['status'] == 'updated'


def test_software_review_selection_columns_default_to_unselected(ui_box):
    _, rows = seed_candidates(ui_box, packages=('sosreport',))
    assert rows[0]['selected'] == 0
    with ui_box.connect() as db:
        column = db.execute("SELECT dflt_value FROM pragma_table_info('software_reviews') WHERE name='selected'").fetchone()
    assert column[0] == '0'


def test_failed_old_candidate_is_retired_when_latest_inventory_changes(ui_box):
    candidates, rows = seed_candidates(ui_box, packages=('sosreport',), candidate='1.1')
    ui_box.update_software_review(candidates[0]['id'], status='failed', detail='audit failed')
    new_candidates, new_rows = seed_candidates(ui_box, packages=('sosreport',), candidate='1.2')
    assert new_candidates[0]['id'] != candidates[0]['id']
    assert ui_box.software_review(candidates[0]['id'])['available'] == 0
    assert new_rows[0]['status'] == 'pending'
    assert new_rows[0]['available'] == 1


def test_old_batch_card_rejects_selection_changed_after_draft(ui_box):
    candidates, rows = seed_candidates(ui_box)
    initial_markup = software_batch.keyboard(ui_box, rows)
    record_card(ui_box, initial_markup)
    first = software_batch.callback_data('default', 'select', candidates[0]['id'])
    software_batch.callback('default', payload(first))
    old_markup = software_batch.keyboard(ui_box)
    old_run = next(button for line in old_markup['inline_keyboard'] for button in line
                   if button['text'].startswith('批量更新已选'))
    record_card(ui_box, old_markup, message_id=92)

    second = software_batch.callback_data('default', 'select', candidates[1]['id'])
    software_batch.callback('default', payload(second))
    with pytest.raises(ValueError, match='选择已变化'):
        software_batch.callback('default', payload(old_run['callback_data'], message_id=92))


def test_manually_reselecting_skipped_candidate_restores_pending(ui_box):
    candidates, rows = seed_candidates(ui_box, packages=('sosreport',))
    markup = software_batch.keyboard(ui_box, rows)
    record_card(ui_box, markup)
    skip = software_batch.callback_data('default', 'skip', candidates[0]['id'])
    software_batch.callback('default', payload(skip))
    assert ui_box.software_review(candidates[0]['id'])['status'] == 'skipped'

    latest = software_batch.keyboard(ui_box)
    select = next(button for line in latest['inline_keyboard'] for button in line
                  if button['callback_data'].startswith('su2|select|'))
    record_card(ui_box, latest, message_id=93)
    software_batch.callback('default', payload(select['callback_data'], message_id=93))
    selected = ui_box.software_review(candidates[0]['id'])
    assert selected['selected'] == 1
    assert selected['status'] == 'pending'


def test_failed_same_fingerprint_can_be_selected_again(ui_box):
    candidates, rows = seed_candidates(ui_box, packages=('libegl-mesa0',), candidate='1.1')
    ui_box.update_software_review(candidates[0]['id'], status='failed', detail='audit failed')
    current = software_batch.current_candidates(ui_box, candidates)
    assert current[0]['status'] == 'failed'
    markup = software_batch.keyboard(ui_box, current)
    select = next(button for line in markup['inline_keyboard'] for button in line
                  if button['callback_data'].startswith('su2|select|'))
    record_card(ui_box, markup, message_id=94)
    software_batch.callback('default', payload(select['callback_data'], message_id=94))
    assert ui_box.software_review(candidates[0]['id'])['selected'] == 1
    run_markup = software_batch.keyboard(ui_box)
    assert any(button['text'].startswith('批量更新已选')
               for line in run_markup['inline_keyboard'] for button in line)


def test_outbox_reopen_preserves_selected_state(ui_box):
    _, rows = seed_candidates(ui_box, packages=('sosreport',))
    ui_box.update_software_review(rows[0]['id'], selected=1, selection_epoch=1)
    reopened = Outbox(ui_box.path)
    assert [row['id'] for row in reopened.software_selection()] == [rows[0]['id']]


def _make_old_submitting_batch(box):
    _, rows = seed_candidates(box, packages=('sosreport',))
    box.update_software_review(rows[0]['id'], selected=1, selection_epoch=1)
    markup = software_batch.keyboard(box)
    run_button = next(button for line in markup['inline_keyboard'] for button in line
                      if button['text'].startswith('批量更新已选'))
    key = run_button['callback_data'].split('|')[2]
    batch = box.software_batches()[0]
    with box.connect() as db:
        db.execute("UPDATE software_update_batches SET status='submitting',updated_at=? WHERE id=?",
                   (time.time() - 61, key))
    return batch


def test_recover_submissions_queries_existing_root_request_without_restarting(ui_box, monkeypatch):
    batch = _make_old_submitting_batch(ui_box)
    admin_call = Mock(return_value=json.dumps({
        'id': 'd' * 32, 'target': 'apt-batch:' + 'd' * 32, 'status': 'running',
    }))
    monkeypatch.setattr(software_batch.scheduler, 'admin', admin_call)
    software_batch.recover_submissions(ui_box)
    assert admin_call.call_args.args == ('software', 'request', batch['request_id'])
    assert all(call.args[1] != 'apt-batch' for call in admin_call.call_args_list)
    assert ui_box.software_batches()[0]['status'] == 'running'


def test_recover_submissions_marks_missing_root_uncertain_without_restart(ui_box, monkeypatch):
    batch = _make_old_submitting_batch(ui_box)
    admin_call = Mock(return_value='null')
    monkeypatch.setattr(software_batch.scheduler, 'admin', admin_call)
    software_batch.recover_submissions(ui_box)
    assert admin_call.call_args.args == ('software', 'request', batch['request_id'])
    assert all(call.args[1] != 'apt-batch' for call in admin_call.call_args_list)
    assert ui_box.software_batches()[0]['status'] == 'uncertain'
