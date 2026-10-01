import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from pylon.tasks.gmail import cleanup, review
from pylon.admin import agent_runtime


def candidate(cid='gmail-nt-test1234'):
    return {
        'id': cid, 'from': 'alerts@example.com', 'subject_family': '登录提醒',
        'suggested_query': '-is:starred from:alerts@example.com subject:"登录提醒"',
        'sample_count': 2, 'sample': {
            'id': 'message-1', 'from': 'alerts@example.com', 'subject': '登录提醒',
            'date': 'Thu, 1 Oct 2026 10:00:00 +0800', 'snippet': '你的账号有一次登录活动',
            'labels': ['INBOX'],
        }, 'status': 'pending',
    }


@pytest.fixture
def files(tmp_path, monkeypatch):
    data = tmp_path / 'gmail'
    data.mkdir()
    candidates = data / 'new_type_candidates.json'
    extra = data / 'extra_rules.json'
    overrides = data / 'rule_overrides.json'
    reviews = data / 'reviews.json'
    proposals = data / 'proposals.json'
    monkeypatch.setattr(cleanup, 'DATA_DIR', data)
    monkeypatch.setattr(cleanup, 'NEW_TYPE_STATE_PATH', candidates)
    monkeypatch.setattr(cleanup, 'EXTRA_RULES_PATH', extra)
    monkeypatch.setattr(cleanup, 'RULE_OVERRIDES_PATH', overrides)
    monkeypatch.setattr(review, 'REVIEWS_PATH', reviews)
    monkeypatch.setattr(review, 'PROPOSALS_PATH', proposals)
    monkeypatch.setattr(review, 'LOCK_PATH', data / '.lock')
    cleanup.save_json(candidates, {'version': 1, 'candidates': {candidate()['id']: candidate()}})
    return data


def proposal(operation='add'):
    return {
        'title': '登录提醒清理', 'summary': '删除普通登录通知，保留安全告警。',
        'operations': [{
            'operation': operation, 'target_rule': '' if operation == 'add' else 'promotions_social',
            'name': 'login_notice' if operation == 'add' else 'promotions_social',
            'description': '登录通知',
            'query': '-is:starred from:alerts@example.com subject:"登录提醒"' if operation != 'disable' else '',
            'reason': '本人意见',
        }],
        'impact': ['只影响非星标邮件'], 'verification': ['预览匹配数量'],
    }


def test_validate_requires_star_protection(files):
    value = proposal()
    assert review.validate_proposal(value)['operations'][0]['query'].startswith('-is:starred')
    value['operations'][0]['query'] = 'from:alerts@example.com'
    with pytest.raises(ValueError, match='-is:starred'):
        review.validate_proposal(value)


def test_logic_proposal_requires_narrow_query_exclusion(files):
    value = proposal()
    value['operations'][0]['logic'] = {
        'version': 1,
        'all': [{'field': 'from', 'op': 'equals', 'value': 'alerts@example.com'}],
        'any': [],
        'none': [{'field': 'subject', 'op': 'contains', 'value': '安全'}],
    }
    assert review.validate_proposal(value, require_logic=True)['operations'][0]['logic']['version'] == 1
    value['operations'][0]['query'] = '-is:starred subject:登录'
    with pytest.raises(ValueError, match='准确发件人'):
        review.validate_proposal(value, require_logic=True)


def test_agent_schema_accepts_gmail_proposal():
    payload = {
        'status': 'completed', 'summary': '已生成草案', 'question': '',
        'changes': [], 'verification': [], 'limitations': [], 'checks': [],
        'gmail_proposal': proposal(),
    }
    parsed = agent_runtime.parse_result(json.dumps(payload, ensure_ascii=False))
    assert parsed['gmail_proposal']['operations'][0]['query'].startswith('-is:starred')


def test_feedback_is_idempotent_and_starts_one_grok_task(files, monkeypatch):
    from pylon import agent_tasks
    calls = []

    def invoke(action, *args, text=''):
        calls.append((action, args, text))
        if action == 'status':
            return []
        return {'id': 'a' * 32, 'provider': 'grok', 'status': 'queued'}

    monkeypatch.setattr(agent_tasks, 'invoke', invoke)
    result = review.start_feedback('gmail-nt-test1234', '这是登录提醒，保留安全告警', 'default', 'telegram:default:1')
    assert '提交 Grok' in result
    duplicate = review.start_feedback('gmail-nt-test1234', '这是登录提醒，保留安全告警', 'default', 'telegram:default:1')
    assert '已经受理' in duplicate
    assert [call[0] for call in calls] == ['run']
    state = json.loads((files / 'new_type_candidates.json').read_text())
    assert state['candidates']['gmail-nt-test1234']['status'] == 'reviewing'
    review_state = json.loads((files / 'reviews.json').read_text())
    assert len(review_state['reviews']['gmail-nt-test1234']['feedback']) == 1


def test_new_feedback_clears_previous_error(files, monkeypatch):
    from pylon import agent_tasks
    candidate_state = json.loads((files / 'new_type_candidates.json').read_text())
    candidate_state['candidates']['gmail-nt-test1234']['status'] = 'error'
    candidate_state['candidates']['gmail-nt-test1234']['last_error'] = '旧错误'
    cleanup.save_json(cleanup.NEW_TYPE_STATE_PATH, candidate_state)
    review._save(review.REVIEWS_PATH, {'version': 1, 'reviews': {
        'gmail-nt-test1234': {'candidate_id': 'gmail-nt-test1234', 'status': 'error',
                              'feedback': [], 'requests': {}, 'proposal_ids': [],
                              'task_id': '', 'last_error': '旧错误'}}})
    monkeypatch.setattr(agent_tasks, 'invoke', lambda action, *args, text='':
                         [] if action == 'status' else {'id': 'b' * 32, 'status': 'queued'})
    review.start_feedback('gmail-nt-test1234', '请重新审查这类通知', 'default', 'telegram:default:2')
    saved_review = json.loads((files / 'reviews.json').read_text())['reviews']['gmail-nt-test1234']
    saved_candidate = json.loads((files / 'new_type_candidates.json').read_text())['candidates']['gmail-nt-test1234']
    assert 'last_error' not in saved_review
    assert 'last_error' not in saved_candidate


def test_agent_result_creates_pending_proposal_without_deleting_mail(files):
    data = {'version': 1, 'reviews': {'gmail-nt-test1234': {
        'candidate_id': 'gmail-nt-test1234', 'status': 'reviewing', 'feedback': [],
        'requests': {}, 'proposal_ids': [], 'task_id': 'b' * 32,
    }}}
    review._save(review.REVIEWS_PATH, data)
    result = review.consume_agent_result({'id': 'b' * 32, 'status': 'succeeded', 'result': {'gmail_proposal': proposal()}})
    assert result['status'] == 'proposal_pending'
    proposals = json.loads((files / 'proposals.json').read_text())['proposals']
    assert len(proposals) == 1
    candidate_state = json.loads((files / 'new_type_candidates.json').read_text())
    assert candidate_state['candidates']['gmail-nt-test1234']['status'] == 'proposal_pending'


def test_preview_and_apply_are_read_only_then_future_only(files, monkeypatch):
    proposal_id = 'gmail-rp-' + 'c' * 12
    record = {'id': proposal_id, 'candidate_id': 'gmail-nt-test1234', 'agent_task_id': 'b' * 32,
              'status': 'pending', 'base_rules_hash': review._rule_digest(), **proposal(), 'created_at_utc': 'now'}
    review._save(review.PROPOSALS_PATH, {'version': 1, 'proposals': {proposal_id: record}})
    monkeypatch.setattr(cleanup, 'build_gmail_service', lambda: object())
    monkeypatch.setattr(cleanup, 'list_message_ids', lambda service, query, max_messages=None: ['m1', 'm2'])
    monkeypatch.setattr(cleanup, 'get_message_metadata', lambda service, mid: {'id': mid, 'subject': '登录提醒', 'from': 'alerts@example.com'})
    batch_delete = Mock(side_effect=AssertionError('review must never delete mail'))
    monkeypatch.setattr(cleanup, 'batch_delete', batch_delete)
    preview_text = review.preview(proposal_id)
    assert '预计匹配 2 封' in preview_text
    assert '本提案｜login_notice：0 封｜登录通知' in preview_text
    assert '本次没有删除邮件' in review.apply(proposal_id)
    batch_delete.assert_not_called()
    assert any(rule['name'] == 'login_notice' for rule in cleanup.all_rules())
    saved = json.loads((files / 'proposals.json').read_text())['proposals'][proposal_id]
    assert saved['status'] == 'applied'
    candidate_state = json.loads((files / 'new_type_candidates.json').read_text())
    assert candidate_state['candidates']['gmail-nt-test1234']['status'] == 'applied'


def test_apply_requires_same_preview_snapshot(files, monkeypatch):
    proposal_id = 'gmail-rp-' + 'd' * 12
    record = {'id': proposal_id, 'candidate_id': 'gmail-nt-test1234', 'agent_task_id': 'b' * 32,
              'status': 'pending', 'base_rules_hash': review._rule_digest(), **proposal(), 'created_at_utc': 'now'}
    review._save(review.PROPOSALS_PATH, {'version': 1, 'proposals': {proposal_id: record}})
    monkeypatch.setattr(cleanup, 'build_gmail_service', lambda: object())
    phase = {'value': 0}
    monkeypatch.setattr(cleanup, 'list_message_ids', lambda service, query, max_messages=None: ['m1'] if phase['value'] == 0 else ['m2'])
    monkeypatch.setattr(cleanup, 'get_message_metadata', lambda service, mid: {'id': mid, 'subject': mid, 'from': 'alerts@example.com'})
    review.preview(proposal_id)
    phase['value'] = 1
    with pytest.raises(ValueError, match='变化'):
        review.apply(proposal_id)


def test_reject_returns_candidate_to_pending(files):
    proposal_id = 'gmail-rp-' + 'd' * 12
    record = {'id': proposal_id, 'candidate_id': 'gmail-nt-test1234', 'status': 'pending',
              'base_rules_hash': review._rule_digest(), **proposal(), 'created_at_utc': 'now'}
    review._save(review.PROPOSALS_PATH, {'version': 1, 'proposals': {proposal_id: record}})
    assert '保留待重新提交意见' in review.reject(proposal_id)
    state = json.loads((files / 'new_type_candidates.json').read_text())
    assert state['candidates']['gmail-nt-test1234']['status'] == 'pending'


def test_button_signature_and_sample_reason_flow(files, monkeypatch):
    profile = 'default'
    data = review.dashboard_payload(profile)
    assert 'Gmail 清理向导' in data['text']
    callback = {
        'id': 'callback-1',
        'data': review.callback_data(profile, 'delete', 'gmail-nt-test1234'),
        'message': {'message_id': 77},
    }
    marked = review.callback(profile, callback, 'telegram:default:77')
    assert '应删除' in marked['text']
    callback['id'] = 'callback-2'
    callback['data'] = review.callback_data(profile, 'reason_delete', 'gmail-nt-test1234')
    review.callback(profile, callback, 'telegram:default:78')
    assert '已记录' in review.bind_reason(profile, 77, '这是营销通知，没有安全或账单价值。')
    state = json.loads((files / 'reviews.json').read_text())
    sample = state['reviews']['gmail-nt-test1234']['examples']['delete'][0]
    assert sample['reason'].startswith('这是营销')


def test_logic_proposal_is_persisted_and_preview_uses_local_filter(files, monkeypatch):
    value = proposal()
    value['operations'][0]['logic'] = {
        'version': 1,
        'all': [{'field': 'from', 'op': 'contains', 'value': 'alerts@example.com'}],
        'any': [{'field': 'subject', 'op': 'contains', 'value': '登录'}],
        'none': [{'field': 'subject', 'op': 'contains', 'value': '安全'}],
    }
    assert review.validate_proposal(value)['operations'][0]['logic']['version'] == 1


def test_applied_logic_proposal_can_be_rolled_back(files, monkeypatch):
    proposal_id = 'gmail-rp-' + 'e' * 12
    value = proposal()
    value['operations'][0]['logic'] = {
        'version': 1, 'all': [{'field': 'from', 'op': 'contains', 'value': 'alerts@example.com'}],
        'any': [{'field': 'subject', 'op': 'contains', 'value': '登录'}], 'none': [],
    }
    record = {'id': proposal_id, 'candidate_id': 'gmail-nt-test1234', 'status': 'applied',
              'base_rules_hash': review._rule_digest(), **value, 'created_at_utc': 'now'}
    review._save(review.PROPOSALS_PATH, {'version': 1, 'proposals': {proposal_id: record}})
    cleanup.save_json(cleanup.RULE_OVERRIDES_PATH, {'version': 2, 'operations': [{'operation': 'add', 'target_rule': 'login_notice', 'name': 'login_notice', 'description': 'x', 'query': value['operations'][0]['query'], 'logic': value['operations'][0]['logic'], 'proposal_id': proposal_id}]})
    assert '已回滚' in review.rollback(proposal_id)
    assert json.loads((files / 'rule_overrides.json').read_text())['operations'] == []
