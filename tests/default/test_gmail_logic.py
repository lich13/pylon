from pylon.tasks.gmail.logic import html_to_text, matches, validate_program
from pylon.tasks.gmail import cleanup


def program():
    return {
        'version': 1,
        'all': [{'field': 'from', 'op': 'contains', 'value': 'notice@example'}],
        'any': [{'field': 'subject', 'op': 'contains', 'value': 'digest'},
                {'field': 'body', 'op': 'contains', 'value': 'unsubscribe'}],
        'none': [{'field': 'subject', 'op': 'contains', 'value': 'security'}],
    }


def test_logic_tree_matches_positive_and_keeps_protected_example():
    validate_program(program())
    assert matches(program(), {'from': 'notice@example.com', 'subject': 'weekly digest', 'body': 'x', 'labels': []})
    assert not matches(program(), {'from': 'notice@example.com', 'subject': 'security digest', 'body': 'unsubscribe', 'labels': []})


def test_logic_tree_rejects_flat_keyword_or_and_bad_fields():
    bad = {'version': 1, 'all': [], 'any': [{'field': 'subject', 'op': 'contains', 'value': str(i)} for i in range(4)], 'none': []}
    try:
        validate_program(bad)
    except ValueError as exc:
        assert '平铺关键词' in str(exc)
    else:
        raise AssertionError('flat OR must be rejected')
    try:
        validate_program({'version': 1, 'all': [{'field': 'shell', 'op': 'contains', 'value': 'x'}], 'any': [], 'none': []})
    except ValueError:
        pass
    else:
        raise AssertionError('unknown field must be rejected')


def test_html_body_is_converted_without_script_noise():
    text = html_to_text('<div>通知<br><b>正文</b></div><script>ignore()</script>')
    assert '通知' in text and '正文' in text and 'ignore' not in text


def test_cleanup_logic_filter_honors_starred_and_effective_time(monkeypatch):
    messages = {
        'm1': {'labels': [], 'from': 'notice@example.com', 'subject': 'weekly digest', 'body': 'unsubscribe', 'received_at_ms': 2000},
        'm2': {'labels': ['STARRED'], 'from': 'notice@example.com', 'subject': 'weekly digest', 'body': 'unsubscribe', 'received_at_ms': 2000},
        'm3': {'labels': [], 'from': 'notice@example.com', 'subject': 'weekly digest', 'body': 'unsubscribe', 'received_at_ms': 999},
    }
    monkeypatch.setattr('pylon.tasks.gmail.logic.get_message_full', lambda service, mid: dict(messages[mid]))
    rule = {'logic': program(), 'effective_after_utc': '1970-01-01T00:00:01Z'}
    kept, rejected = cleanup.filter_logic_message_ids(object(), rule, ['m1', 'm2', 'm3'])
    assert kept == ['m1'] and rejected == 2
