import json
from unittest.mock import Mock

import pytest

from pylon import agent_tasks, inline
from pylon.tasks.gmail import batch, cleanup, review, store
from .test_gmail_review import files, candidate, proposal


def prepare(files, monkeypatch):
    candidates = {}
    for n in range(3):
        c = candidate('gmail-nt-fixture' + str(n))
        c.update(sample_ids=[f'm{n}'], subject_family=f'fixture-{n}')
        candidates[c['id']] = c
    cleanup.save_json(cleanup.NEW_TYPE_STATE_PATH, {'version': 1, 'candidates': candidates})
    messages = {
        'm0': {'id': 'm0', 'from': 'alerts@example.invalid', 'subject': '营销通知', 'body': '自动营销内容', 'labels': []},
        'm1': {'id': 'm1', 'from': 'alerts@example.invalid', 'subject': '安全提醒', 'body': '账号异常', 'labels': []},
        'm2': {'id': 'm2', 'from': 'build@example.invalid', 'subject': '构建成功', 'body': '自动构建完成', 'labels': []},
    }
    monkeypatch.setattr(cleanup, 'build_gmail_service', lambda: object())
    monkeypatch.setattr(batch, 'get_message_full', lambda _, mid: dict(messages[mid]))
    monkeypatch.setattr(review, 'get_message_full', lambda _, mid: dict(messages[mid]))
    monkeypatch.setattr(cleanup, 'batch_delete', Mock(side_effect=AssertionError('must not delete')))
    batch.dashboard_payload('default')
    for n, mode in [(0, 'delete'), (1, 'keep'), (2, 'delete')]:
        batch.mark(f'gmail-nt-fixture{n}', f'm{n}', mode, 'default')
        assert batch.bind_reason('default', 0, '自动通知无价值' if mode == 'delete' else '账号安全必须保留')
    return messages


def generated():
    operations = []
    for n, sender, subject in [(0, 'alerts@example.invalid', '营销'), (2, 'build@example.invalid', '构建成功')]:
        operations.append({'operation': 'add', 'target_rule': '', 'name': 'fixture_rule_' + str(n),
            'description': '删除自动通知', 'query': '-is:starred from:' + sender,
            'reason': '普通通知自动产生，安全提醒包含异常信息',
            'logic': {'version': 1, 'all': [{'field': 'from', 'op': 'equals', 'value': sender},
                                         {'field': 'subject', 'op': 'contains', 'value': subject}],
                      'any': [], 'none': [{'field': 'subject', 'op': 'contains', 'value': '安全'}]},
            'sample_validation': {'delete': [f'm{n}'], 'keep': ['m1'], 'difference': '必须保留的安全通知被明确排除'}})
    return {'title': '批量通知规则', 'summary': '保留安全邮件', 'operations': operations,
            'impact': ['未来自动通知'], 'verification': ['验证正反样本']}


def test_cross_candidate_batch_one_task_and_restart(files, monkeypatch):
    prepare(files, monkeypatch)
    invoke = Mock(return_value={'id': 'b' * 32})
    monkeypatch.setattr(agent_tasks, 'invoke', invoke)
    data = batch.state(); bid = data['active']
    assert len(data['batches'][bid]['items']) == 3
    assert '一起交给 Grok' in batch.submit(bid, 'default')
    assert '已经提交' in batch.submit(bid, 'default')
    assert invoke.call_count == 1
    args = invoke.call_args
    assert args.args[:2] == ('gmail-run', 'grok')
    from pylon.admin.agent_state import request_key, prompt_text
    request_key(args.args[3], args.args[2])
    prompt_text(json.loads(args.kwargs['text'])['prompt'])
    bundle = json.loads(args.kwargs['text'])
    assert len(bundle['examples']) == 3
    assert all('body' in x for x in bundle['examples'])
    assert 'body' not in json.dumps(batch.state())
    assert batch.state()['batches'][bid]['task_id'] == 'b' * 32
    result = batch.consume_agent_result({'id': 'b' * 32, 'status': 'succeeded', 'result': {'gmail_proposal': generated()}})
    assert result['status'] == 'proposal_pending'
    assert len(review._proposals()['proposals']) == 1
    assert batch.consume_agent_result({'id': 'b' * 32, 'status': 'succeeded'}) == result


def test_missing_reason_and_maximum_never_launch_agent(files, monkeypatch):
    prepare(files, monkeypatch)
    data = batch.state(); b = data['batches'][data['active']]
    b['items'][0]['reason'] = ''
    batch.save(data)
    invoke = Mock(side_effect=AssertionError('no agent'))
    monkeypatch.setattr(agent_tasks, 'invoke', invoke)
    with pytest.raises(ValueError, match='每个样本'):
        batch.submit(b['id'], 'default')
    b['items'] = [{'candidate_id': 'gmail-nt-fixture0', 'message_id': str(i), 'mode': 'keep', 'reason': 'fixture'} for i in range(20)]
    batch.save(data)
    with pytest.raises(ValueError, match='20'):
        batch.mark('gmail-nt-fixture0', 'm0', 'delete', 'default')


def test_waiting_reply_resumes_same_task(files, monkeypatch):
    prepare(files, monkeypatch)
    invoke = Mock(return_value={'id': 'b' * 32})
    monkeypatch.setattr(agent_tasks, 'invoke', invoke)
    bid = batch.state()['active']
    batch.submit(bid, 'default')
    batch.consume_agent_result({'id': 'b' * 32, 'status': 'waiting', 'result': {'question': '如何区分？'}})
    assert batch.bind_reason('default', 0, '成功构建没有价值，失败需要保留')
    invoke.side_effect = lambda action, *args, **kw: [{'status': 'waiting'}] if action == 'status' else {'id': 'b' * 32}
    batch.submit(bid, 'default')
    assert invoke.call_args.args[:2] == ('gmail-reply', 'b' * 32)


def test_batch_preview_apply_preserves_existing_rules(files, monkeypatch):
    messages = prepare(files, monkeypatch)
    monkeypatch.setattr(agent_tasks, 'invoke', lambda *a, **kw: {'id': 'b' * 32})
    bid = batch.state()['active']; batch.submit(bid, 'default')
    result = batch.consume_agent_result({'id': 'b' * 32, 'status': 'succeeded', 'result': {'gmail_proposal': generated()}})
    pid = result['proposal_id']
    monkeypatch.setattr(cleanup, 'list_message_ids', lambda service, query, max_messages: ['m2'] if 'build@' in query else ['m0', 'm1'])
    monkeypatch.setattr(cleanup, 'filter_logic_message_ids', lambda service, rule, ids: ([i for i in ids if i != 'm1'], 1))
    monkeypatch.setattr(cleanup, 'get_message_metadata', lambda _, mid: {'id': mid, 'subject': messages[mid]['subject']})
    assert '预计匹配 2 封' in review.preview(pid)
    assert '本次没有删除邮件' in review.apply(pid)
    assert len(cleanup.all_rules()) == len(cleanup.RULES) + 2
    assert all(x.get('effective_after_utc') for x in cleanup.load_rule_overrides()['operations'])
    assert all(x['status'] == 'applied' for x in cleanup.load_candidate_state()['candidates'].values())
    assert '已回滚' in review.rollback(pid)
    assert len(cleanup.all_rules()) == len(cleanup.RULES)
    assert all(x['status'] == 'pending' for x in cleanup.load_candidate_state()['candidates'].values())


def test_migration_is_once_and_documents_commit_together(files):
    legacy = {'version': 1, 'reviews': {'fixture': {'status': 'pending'}}}
    review.REVIEWS_PATH.write_text(json.dumps(legacy))
    assert review._state() == legacy
    review.REVIEWS_PATH.write_text('{}')
    assert review._state() == legacy
    with pytest.raises(RuntimeError):
        with review.locked():
            review._save(review.REVIEWS_PATH, {'version': 1, 'reviews': {}})
            cleanup.save_json(cleanup.RULE_OVERRIDES_PATH, {'version': 2, 'operations': []})
            raise RuntimeError('interrupt')
    assert review._state() == legacy
    assert cleanup.load_rule_overrides()['version'] == 1


def test_button_authorization_and_replay(files, monkeypatch):
    prepare(files, monkeypatch)
    monkeypatch.setattr(inline, 'owns_callback', lambda *a: False)
    payload = {'id': 'fixture', 'data': batch.token('default', 'submit', batch.state()['active']), 'message': {'message_id': 10}}
    with pytest.raises(ValueError, match='本人会话'):
        batch.callback('default', payload)
    monkeypatch.setattr(inline, 'owns_callback', lambda *a: True)
    invoke = Mock(return_value={'id': 'a' * 32}); monkeypatch.setattr(agent_tasks, 'invoke', invoke)
    first = batch.callback('default', payload)
    assert batch.callback('default', payload) == first
    assert invoke.call_count == 1
    payload['data'] = payload['data'][:-1] + ('0' if payload['data'][-1] != '0' else '1')
    with pytest.raises(ValueError, match='失效'):
        batch.callback('default', payload)
