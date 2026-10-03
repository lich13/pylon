from pylon.tasks.gmail.logic import html_to_text, matches, validate_program, narrow_query, get_message_full
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


def test_new_query_requires_sender_and_preserves_quoted_terms():
    assert narrow_query('-is:starred from:alerts@example.com subject:"weekly digest"')
    assert not narrow_query('-is:starred subject:"weekly digest"')
    assert not narrow_query('-is:starred from:alerts@example.com {digest unsubscribe}')


def test_full_message_reads_text_attachment_and_decodes_headers():
    class Call:
        def __init__(self, value): self.value = value
        def execute(self): return self.value
    class Attachments:
        def get(self, **kwargs):
            return Call({'data': 'VGV4dCBhdHRhY2htZW50'})
    class Messages:
        def get(self, **kwargs):
            return Call({'id': 'm1', 'internalDate': '2000', 'labelIds': ['INBOX'], 'payload': {
                'headers': [{'name': 'From', 'value': '=?utf-8?b?5Y+R5Lu2?= <a@example.com>'}],
                'mimeType': 'multipart/mixed', 'parts': [
                    {'mimeType': 'text/plain', 'body': {'data': '5L2g5aW9'}},
                    {'mimeType': 'text/plain', 'filename': 'note.txt', 'body': {'attachmentId': 'a1', 'size': 20}},
                ]}})
        def attachments(self): return Attachments()
    class Users:
        def messages(self): return Messages()
    class Service:
        def users(self): return Users()
    result = get_message_full(Service(), 'm1')
    assert result['from'].startswith('发件')
    assert '你好' in result['body'] and 'Text attachment' in result['body']
    assert result['attachment_names'] == ['note.txt']


def test_sender_address_equality_handles_display_name():
    from pylon.tasks.gmail.logic import matches
    assert matches({'version': 1, 'all': [{'field': 'from', 'op': 'equals', 'value': 'fixture@example.invalid'}], 'any': [], 'none': []},
                   {'from': 'Fixture Name <fixture@example.invalid>'})
