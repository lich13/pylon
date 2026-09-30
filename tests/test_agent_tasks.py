import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import time
from unittest.mock import Mock
import uuid
import sys

import pytest

from pylon import agent_tasks as client, commands, bot
from pylon.admin import agents, agent_state as state, agent_runtime as runtime, software
from pylon.outbox import Outbox


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(state, 'ROOT', tmp_path / 'root')
    monkeypatch.setattr(state, 'CONFIG', tmp_path / 'config.json')
    monkeypatch.setattr(software, 'ROOT', tmp_path / 'updates')
    monkeypatch.setattr(runtime, 'CODEX_HOME', tmp_path / 'codex')
    monkeypatch.setattr(runtime, 'GROK_HOME', tmp_path / 'grok')
    monkeypatch.setattr(agents, 'kick', Mock())
    monkeypatch.setattr(runtime, 'delete_session', Mock())
    monkeypatch.setenv('PYLON_HOME', str(tmp_path / 'pylon'))
    monkeypatch.delenv('PYLON_COMMAND_ID', raising=False)
    monkeypatch.delenv('PYLON_REPLY_MESSAGE_ID', raising=False)
    return tmp_path


def new(provider='codex', number=1):
    return state.submit(provider, 'default', f'telegram:default:{number}', '检查实际运行版本，不更新')


def raw(key):
    with state.database() as db:
        return dict(state.task(db, key))


def result(status='completed', **kwargs):
    return dict(status=status, summary='检查完成', question='需要补充什么？' if status == 'needs_input' else '',
                changes=[], verification=['读取运行中的实际程序版本'], limitations=[],
                checks=[dict(kind='software_version', target='sub2api', expected='0.2.10')]) | kwargs


def ack_all():
    for event in state.results():
        state.acknowledge(event['id'])


def test_submit_durable_idempotence_and_provider_selection(isolated):
    first = new('grok')
    assert new('codex')['id'] == first['id']
    assert raw(first['id'])['provider'] == 'grok'
    with state.database() as db:
        assert db.execute('SELECT count(*) FROM tasks').fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM inputs').fetchone()[0] == 1
    assert 'text' not in first and 'session_id' not in first


@pytest.mark.parametrize('provider,profile,key,text', [
    ('bash', 'default', 'telegram:default:1', 'x'), ('codex', 'other', 'cli:' + 'a' * 32, 'x'),
    ('codex', 'default', 'telegram:webops:1', 'x'), ('codex', 'default', '../bad', 'x'),
    ('codex', 'default', 'cli:' + 'a' * 32, ''), ('codex', 'default', 'cli:' + 'a' * 32, 'x' * 16001),
])
def test_privileged_input_boundary(isolated, provider, profile, key, text):
    with pytest.raises(ValueError):
        state.submit(provider, profile, key, text)


def test_root_helper_rejects_worker_and_arbitrary_parameters(isolated):
    for args in [['worker'], ['run', 'codex', 'default'], ['status', '../../root'], ['ack', '../x'], ['logs', 'a', 'b']]:
        with pytest.raises(ValueError):
            agents.main(args)
    agents.kick.assert_not_called()


def test_command_payload_is_stdin_not_shell_arguments(monkeypatch):
    value = '更新测试工具; $(touch /tmp/injected)\n原样作为任务描述'
    call = Mock(return_value=Mock(returncode=0, stdout='{}'))
    monkeypatch.setattr(client.subprocess, 'run', call)
    client.invoke('run', 'codex', 'default', 'cli:' + 'a' * 32, text=value)
    assert call.call_args.kwargs['input'] == value
    assert value not in call.call_args.args[0]
    assert 'shell' not in call.call_args.kwargs


def test_wait_answer_resumes_same_provider_session_and_refreshes_context(isolated, monkeypatch):
    first = new('grok')
    sid = str(uuid.uuid4())
    seen = []
    def turn(row, inputs, remaining):
        seen.append((row['provider'], row['session_id'], inputs))
        runtime.record_session(row['id'], sid)
        return result('needs_input') if len(seen) == 1 else result()
    monkeypatch.setattr(runtime, 'run_turn', turn)
    monkeypatch.setattr(runtime, 'verify', lambda checks: ['实际版本 0.2.10'])
    agents.execute_next()
    assert raw(first['id'])['status'] == 'waiting'
    assert state.reply(first['id'][:12], 'webops', 'telegram:webops:2', '只检查')['status'] == 'queued'
    agents.execute_next()
    assert seen == [('grok', '', ['检查实际运行版本，不更新']), ('grok', sid, ['只检查'])]
    assert raw(first['id'])['status'] == 'succeeded'
    runtime.delete_session.assert_not_called()
    ack_all()
    agents.execute_next()
    assert raw(first['id'])['cleanup'] == 'done'
    assert runtime.delete_session.call_args.args[0]['session_id'] == sid
    with state.database() as db:
        assert db.execute('SELECT count(*) FROM inputs').fetchone()[0] == 0
    with pytest.raises(ValueError, match='已结束'):
        state.reply(first['id'], 'default', 'telegram:default:3', '再继续')


def test_message_arriving_during_completion_is_not_lost(isolated, monkeypatch):
    row = new()
    def turn(current, inputs, remaining):
        runtime.record_session(current['id'], str(uuid.uuid4()))
        state.reply(current['id'], 'default', 'telegram:default:2', '再核对健康状态')
        return result()
    monkeypatch.setattr(runtime, 'run_turn', turn)
    monkeypatch.setattr(runtime, 'verify', lambda _: ['实际版本正确'])
    agents.execute_next()
    assert raw(row['id'])['status'] == 'queued'
    assert raw(row['id'])['session_id']
    with state.database() as db:
        assert [x[0] for x in db.execute('SELECT text FROM inputs WHERE consumed=0')] == ['再核对健康状态']


def test_install_lock_and_worker_lock_prevent_overlapping_execution(isolated, monkeypatch):
    row = new()
    turn = Mock(return_value=result())
    monkeypatch.setattr(runtime, 'run_turn', turn)
    with software.database():
        pass
    with (software.ROOT / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        agents.execute_next()
    turn.assert_not_called()
    assert raw(row['id'])['status'] == 'queued'
    with (state.ROOT / 'worker.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        agents.execute_next()
    turn.assert_not_called()


def test_interrupted_task_never_reexecutes_and_cancel_works_inflight(isolated, monkeypatch):
    first = new()
    with state.database() as db:
        db.execute("UPDATE tasks SET status='running',started_at=? WHERE id=?", (time.time() - 10, first['id']))
    turn = Mock(side_effect=AssertionError('must not replay'))
    monkeypatch.setattr(runtime, 'run_turn', turn)
    agents.execute_next()
    turn.assert_not_called()
    assert raw(first['id'])['status'] == 'interrupted'
    second = new(number=2)
    def cancel_during_turn(row, inputs, remaining):
        state.cancel(row['id'], 'default', 'telegram:default:3')
        return result('needs_input')
    monkeypatch.setattr(runtime, 'run_turn', cancel_during_turn)
    agents.execute_next()
    assert raw(second['id'])['status'] == 'cancelled'


def test_time_limits_and_no_false_success(isolated, monkeypatch):
    row = new()
    monkeypatch.setattr(runtime, 'run_turn', lambda *args: result(checks=[]))
    agents.execute_next()
    assert raw(row['id'])['status'] == 'uncertain'
    waiting = new(number=2)
    with state.database() as db:
        db.execute("UPDATE tasks SET status='waiting',waiting_since=? WHERE id=?", (time.time() - 86401, waiting['id']))
    with pytest.raises(ValueError, match='到期'):
        state.reply(waiting['id'], 'default', 'telegram:default:3', '晚到')
    agents.execute_next()
    assert raw(waiting['id'])['status'] == 'timed_out'
    limit = new(number=4)
    with state.database() as db:
        db.execute('UPDATE tasks SET runtime=3601 WHERE id=?', (limit['id'],))
    agents.execute_next()
    assert raw(limit['id'])['status'] == 'timed_out'


def test_cleanup_requires_persisted_result_and_retry_never_reexecutes(isolated, monkeypatch):
    row = new()
    state.cancel(row['id'], 'default', 'telegram:default:2')
    agents.execute_next()
    runtime.delete_session.assert_not_called()
    ack_all()
    runtime.delete_session.side_effect = OSError('disk busy')
    agents.execute_next()
    assert raw(row['id'])['cleanup'] == 'failed'
    assert len(state.results()) == 1
    ack_all()
    with state.database() as db:
        db.execute('UPDATE tasks SET cleanup_after=0 WHERE id=?', (row['id'],))
    runtime.delete_session.side_effect = None
    agents.execute_next()
    assert raw(row['id'])['cleanup'] == 'done'
    turn = Mock(side_effect=AssertionError('never rerun'))
    monkeypatch.setattr(runtime, 'run_turn', turn)
    agents.execute_next()
    turn.assert_not_called()


def test_queue_handoff_is_idempotent_after_crash_and_scrubs_only_owned_input(isolated, monkeypatch):
    row = new()
    state.cancel(row['id'], 'default', 'telegram:default:2')
    box = Outbox()
    def invoke(action, *args, **kwargs):
        if action == 'results':
            return state.results()
        if action == 'ack':
            raise OSError('crash after durable enqueue')
        if action == 'cleaned':
            return []
    monkeypatch.setattr(client, 'invoke', invoke)
    for _ in range(2):
        with pytest.raises(OSError):
            client.reconcile(box)
    assert box.counts() == {'pending': 1}
    assert state.results()
    box.put('agent-command:telegram:default:1', row['id'])
    with box.connect() as db:
        db.execute("INSERT INTO updates(profile,update_id,payload,status) VALUES('default',1,'secret request','done')")
        db.execute("INSERT INTO updates(profile,update_id,payload,status) VALUES('default',9,'unrelated input','done')")
    client.scrub_inputs(box, {row['id']})
    with box.connect() as db:
        assert [r[0] for r in db.execute('SELECT payload FROM updates ORDER BY update_id')] == ['{}', 'unrelated input']


def test_replies_require_real_receipt_and_do_not_arm_monitor_removal(isolated, monkeypatch):
    box = Outbox()
    monkeypatch.setattr(client, 'profile_env', lambda _: {'TELEGRAM_HOME_CHANNEL': '123'})
    call = Mock(return_value='已继续')
    monkeypatch.setattr(client, 'command', call)
    eid = box.enqueue(profile='default', job_id='agent', kind='text', payload={'text': '问题', 'agent_task': 'a' * 32}, key='one')
    assert client.reply_to('default', 77, '1', box) is None
    box.finish(eid, 'delivered', receipt=[{'message_id': 77, 'chat_id': 123}])
    assert client.reply_to('webops', 77, '1', box) is None
    assert client.reply_to('default', 77, '1', box) == '已继续'
    call.assert_called_once_with('reply', 'a' * 32 + ' 1', 'default')
    monkeypatch.setenv('PYLON_REPLY_MESSAGE_ID', '77')
    monkeypatch.setattr(client, 'Outbox', lambda: box)
    monitor = Mock(side_effect=AssertionError('must not remove a monitor'))
    monkeypatch.setattr(commands, 'monitor_command', monitor)
    assert commands.handle('default', '1') == '已继续'
    monitor.assert_not_called()


def test_owner_gate_still_precedes_agent_dispatch(isolated):
    profile = Path(os.environ['PYLON_HOME']) / 'profiles/default'
    profile.mkdir(parents=True)
    (profile / '.env').write_text('TELEGRAM_HOME_CHANNEL=123\n')
    box = Outbox()
    bot.store_updates(box, 'default', [{'update_id': 1, 'message': {'chat': {'id': 123, 'type': 'private'}, 'from': {'id': 456}, 'text': '/agent_codex delete'}}])
    with box.connect() as db:
        assert db.execute('SELECT count(*) FROM updates').fetchone()[0] == 0


def test_native_arguments_pin_provider_disable_memory_and_use_exact_resume(isolated):
    folder = isolated / 'work'
    config = json.dumps({'model': 'existing-model', 'effort': 'max'})
    sid = str(uuid.uuid4())
    for provider in ('codex', 'grok'):
        row = dict(provider=provider, session_id=sid, settings=config, turns=2)
        args = runtime.argv(row, folder)
        assert args[:3] == ['/usr/bin/sudo', '-n', '-H']
        assert args[args.index('--') + 1] == '/usr/local/bin/' + provider
        if provider == 'grok':
            assert 'GROK_MEMORY=0' in args[:args.index('--')]
            disabled = set(args[args.index('--disallowed-tools') + 1].split(','))
            # All scheduler tools depend on the subagent runtime, including
            # the read-only list operation; leaving one enabled prevents init.
            assert {'scheduler_create', 'scheduler_delete', 'scheduler_list'} <= disabled
        assert sid in args and '--last' not in args and '--continue' not in args
        assert 'existing-model' in args
        assert 'resume' in args if provider == 'codex' else '--resume' in args
    args = runtime.argv(dict(provider='codex', session_id='', settings=config, turns=1), folder)
    assert 'memories' in args and 'multi_agent' in args and '--no-daemon' in args


def test_result_validation_and_independent_checks(isolated, monkeypatch):
    assert runtime.parse_result('说明\n```json\n' + json.dumps(result(), ensure_ascii=False) + '\n```')['status'] == 'completed'
    for bad in ('done', '{"status":"completed"}', json.dumps(result('needs_input', question=''))):
        with pytest.raises(ValueError):
            runtime.parse_result(bad)
    target = isolated / 'fixture'
    target.write_text('verified')
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    assert runtime.verify([dict(kind='file_sha256', target=str(target), expected=digest)]) == [f'{target}：{digest}']
    target.write_text('changed')
    with pytest.raises(ValueError, match='独立验收失败'):
        runtime.verify([dict(kind='file_sha256', target=str(target), expected=digest)])
    with pytest.raises(ValueError):
        runtime.verify([dict(kind='shell', target='rm anything', expected='ok')])


def test_state_and_session_identity_guards(isolated, monkeypatch):
    row = new()
    sid = str(uuid.uuid4())
    runtime.record_session(row['id'], sid)
    with pytest.raises(ValueError, match='意外改变'):
        runtime.record_session(row['id'], str(uuid.uuid4()))
    with pytest.raises(ValueError):
        runtime.record_session(row['id'], '../wrong')
    root = isolated / 'outside'
    root.mkdir()
    unsafe = isolated / 'unsafe'
    unsafe.symlink_to(root)
    monkeypatch.setattr(state, 'ROOT', unsafe)
    with pytest.raises(PermissionError):
        state.rows()


def test_secret_redaction_keeps_context_valid_json():
    data = {'log': 'authorization=abc\nBearer abcd\nhttps://host/?token=abc&next=x', 'version': '0.2.10'}
    cleaned = state.clean_data(data)
    assert json.loads(json.dumps(cleaned))['version'] == '0.2.10'
    assert 'abcd' not in cleaned['log'] and 'token=abc' not in cleaned['log']


def test_retention_does_not_remove_unhanded_results(isolated):
    row = new()
    state.cancel(row['id'], 'default', 'telegram:default:2')
    with state.database() as db:
        db.execute("UPDATE tasks SET cleanup='done',updated_at=? WHERE id=?", (time.time() - 31 * 86400, row['id']))
    agents.housekeeping()
    assert state.rows(row['id'])
    ack_all()
    agents.housekeeping()
    assert not state.rows()


def test_streaming_adapter_and_timeout_stop_only_its_process(isolated, monkeypatch):
    row = new()
    script = isolated / 'fake_cli.py'
    sid = str(uuid.uuid4())
    script.write_text('import json,time\n' +
        'print(json.dumps({"type":"thread.started","thread_id":' + repr(sid) + '}),flush=True)\n' +
        'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":' + repr(json.dumps(result('needs_input'))) + '}}),flush=True)\n' +
        'print(json.dumps({"type":"turn.completed"}),flush=True)\n')
    monkeypatch.setattr(runtime, 'context', lambda _: {})
    monkeypatch.setattr(runtime, 'settings', lambda _: {'model': 'fixture', 'effort': 'max'})
    monkeypatch.setattr(runtime, 'argv', lambda *_: [sys.executable, str(script)])
    assert runtime.run_turn(raw(row['id']), ['问题'], 5)['status'] == 'needs_input'
    assert raw(row['id'])['session_id'] == sid
    script.write_text('import time\ntime.sleep(10)\n')
    with pytest.raises(TimeoutError):
        runtime.run_turn(raw(row['id']), ['继续'], 0.1)


def test_foreign_session_and_symlink_cleanup_is_rejected(isolated, monkeypatch):
    # Restore the real cleaner for this ownership test.
    import importlib
    cleaner = importlib.reload(runtime).delete_session
    monkeypatch.setattr(runtime, 'CODEX_HOME', isolated / 'codex')
    monkeypatch.setattr(runtime, 'GROK_HOME', isolated / 'grok')
    runtime.CODEX_HOME.mkdir()
    sid = str(uuid.uuid4())
    row = new()
    with sqlite3.connect(runtime.CODEX_HOME / 'state_5.sqlite') as db:
        db.execute('CREATE TABLE threads(id TEXT,cwd TEXT,rollout_path TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?,?)', (sid, '/unrelated/user/project', '/unrelated/rollout.jsonl'))
    current = raw(row['id']) | {'session_id': sid}
    native = Mock(side_effect=AssertionError('must not delete another session'))
    monkeypatch.setattr(runtime.subprocess, 'run', native)
    with pytest.raises(ValueError, match='归属'):
        cleaner(current)
    native.assert_not_called()
    with sqlite3.connect(runtime.CODEX_HOME / 'state_5.sqlite') as db:
        db.execute('DELETE FROM threads')
    folder = state.ROOT / 'work'
    folder.mkdir()
    (folder / row['id']).symlink_to(isolated)
    with pytest.raises(PermissionError):
        cleaner(raw(row['id']))


def test_grok_cleanup_removes_owned_prompt_history_and_rejects_unknown_files(isolated, monkeypatch):
    import importlib
    from urllib.parse import quote
    cleaner = importlib.reload(runtime).delete_session
    monkeypatch.setattr(runtime, 'GROK_HOME', isolated / 'grok')
    row = new('grok')
    folder = runtime.workdir(row['id'])
    group = runtime.GROK_HOME / 'sessions' / quote(str(folder), safe='')
    group.mkdir(parents=True)
    (group / 'prompt_history.jsonl').write_text('{"text":"private task input"}\n')
    foreign = group.parent / 'existing-project'
    foreign.mkdir()
    (foreign / 'existing-session').write_text('keep')
    cleaner(raw(row['id']))
    assert not group.exists() and not folder.exists()
    assert (foreign / 'existing-session').read_text() == 'keep'
    group.mkdir()
    (group / 'unexpected').write_text('preserve')
    with pytest.raises(ValueError, match='未识别残留'):
        cleaner(raw(row['id']))
    assert (group / 'unexpected').read_text() == 'preserve'
    (group / 'unexpected').unlink()
    (group / 'prompt_history.jsonl').symlink_to(foreign / 'existing-session')
    with pytest.raises(PermissionError):
        cleaner(raw(row['id']))
    assert (foreign / 'existing-session').read_text() == 'keep'
