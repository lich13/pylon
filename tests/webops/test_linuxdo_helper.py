from __future__ import annotations

import asyncio
import contextlib
import copy
import errno
import io
import json
import os
import pty
import select
import signal
import sqlite3
import stat
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from pylon.outbox import Outbox
from pylon.tasks.linuxdo import __main__ as linuxdo_main
from pylon.tasks.linuxdo import following, helper, helper_account, helper_collect, helper_store, judge, pipeline, policy
from pylon.tasks.linuxdo.common import digest
from pylon.tasks.linuxdo.helper import SOURCE, envelope, parse_message
from pylon.tasks.linuxdo.helper_account import AuthorizationRequired, RateLimited
from pylon.tasks.linuxdo.helper_store import HelperStore
from pylon.tasks.linuxdo.sources import Observation, Page
from pylon.tasks.linuxdo.store import Store


PUBLISHED_AT = '2026-01-02T03:04:05+00:00'
EDITED_AT = '2026-01-02T03:05:05+00:00'
IDENTITY = {'account_id': 90001, 'bot_id': 90002}
BODY = '合成脚本可按顺序执行三个操作。详情见 https://example.invalid/resource 。'


def message(message_id=101, *, kind='replied', topic_id='9101', post_number=3,
            actor='fixture_actor', body=BODY):
    url = f'https://linux.do/t/topic/{topic_id}/{post_number}'
    return {
        'message_id': message_id,
        'incoming': True,
        'text': f'【{kind}】 @{actor} 合成话题\n{body}',
        'published_at': PUBLISHED_AT,
        'edited_at': '',
        'links': [{'label': '合成话题', 'url': url, 'header': True}],
        'buttons': [{'label': '查看原帖', 'url': url}],
    }


def two_line_message(message_id=301, *, header='✉️ @fixture_actor 发给你 一条私信',
                     title_prefix='在 ', body=BODY):
    title = '合成通知主题'
    return {
        'message_id': message_id, 'incoming': True,
        'text': f'{header}\n{title_prefix}{title}\n\n{body}',
        'published_at': PUBLISHED_AT, 'edited_at': '',
        'links': [
            {'label': '@fixture_actor', 'url': 'https://linux.do/u/fixture_actor', 'header': True, 'line': 1},
            {'label': title, 'url': 'https://linux.do/t/topic/9150/3', 'header': False, 'line': 2},
        ],
        'buttons': [],
    }


def published_message(message_id=301, *, actor='fixture_actor', post_number=1, body=BODY):
    raw = two_line_message(message_id, header=f'💬 @{actor} 发布了', body=body)
    raw['links'][0].update(label=f'@{actor}', url=f'https://linux.do/u/{actor}')
    raw['links'][1]['url'] = f'https://linux.do/t/topic/9150/{post_number}'
    return raw


def checkpoint(cursor=100, *, baseline_id=100, identity=None):
    return {'identity': identity or digest(IDENTITY), 'baseline': True,
            'baseline_id': baseline_id, 'cursor': cursor, 'retry_at': 0}


def save(store, *messages, followed=None):
    previous = store.checkpoint(SOURCE) or checkpoint()
    cursor = max([previous['cursor'], *(raw['message_id'] for raw in messages)])
    return HelperStore(store, followed=followed).save_page(list(messages), dict(previous, cursor=cursor))


def rows(store, query, parameters=()):
    with store.box.connect() as db:
        return [dict(row) for row in db.execute(query, parameters)]


def event_rows(store):
    return rows(store, 'SELECT * FROM events ORDER BY created_at,id')


def collection_config(**overrides):
    return {'mode': 'helper', 'max_pages': 2, 'page_size': 2, 'recheck_limit': 0,
            'fetch_timeout_seconds': 5, 'source_budget_seconds': 40} | overrides


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    forbidden = Mock(side_effect=AssertionError('Helper tests must use synthetic local inputs'))
    monkeypatch.setattr(httpx.Client, 'send', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden)
    monkeypatch.setattr(helper_account, '_client', forbidden)


@pytest.fixture(autouse=True)
def following_root(tmp_path, monkeypatch):
    root = tmp_path / 'following-private'
    root.mkdir(mode=0o700)
    monkeypatch.setattr(following, 'private_root', lambda: root)
    monkeypatch.setattr(helper_account, 'private_root', lambda: root)
    return root


def write_following(root, usernames):
    path = root / 'following.json'
    path.write_text(json.dumps({'version': 1, 'complete': True, 'imported_at': PUBLISHED_AT,
                                'usernames': usernames}))
    path.chmod(0o600)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.delenv('PYLON_RUN_ID', raising=False)
    monkeypatch.setattr(pipeline, 'LEGACY_STATE', tmp_path / 'unused-legacy-state.json')
    return Store(Outbox(tmp_path / 'outbox.sqlite3'))


@pytest.fixture
def clock(monkeypatch):
    current = [datetime.fromisoformat(PUBLISHED_AT).timestamp() + 3600]
    monkeypatch.setattr(helper_collect.time, 'time', lambda: current[0])
    return current


@pytest.mark.parametrize(('kind', 'label', 'route'), [
    ('following_posted', '关注者发帖', 'personal'),
    ('following_replied', '关注者回复', 'personal'),
    ('private_message', '私信', 'personal'),
    ('replied', '回复', 'personal'),
    ('mentioned', '提及', 'personal'),
    ('quoted', '引用', 'personal'),
    ('group_mentioned', '群组提及', 'personal'),
    ('linked', '被引用链接', 'personal'),
    ('posted', '关注主题回复', 'personal'),
    ('event_reminder', '活动提醒', 'personal'),
    ('event_invitation', '活动邀请', 'personal'),
    ('watching_first_post', '关注首帖', 'board'),
    ('watching_category_or_tag', '板块订阅', 'board'),
])
def test_explicit_notification_types_select_the_expected_route(kind, label, route):
    for header in (kind, label):
        notice = parse_message(message(kind=header))
        assert (notice.kind, notice.route) == (kind, route)
        assert (notice.topic_id, notice.post_number) == ('9101', 3)


@pytest.mark.parametrize(('header', 'kind'), [
    ('合成用户给你发送了私信', 'private_message'),
    ('你关注的合成用户发布了新帖', 'following_posted'),
    ('你关注的合成用户回复了主题', 'following_replied'),
    ('合成用户回复了你的帖子', 'replied'),
    ('合成用户提及了你', 'mentioned'),
    ('合成用户引用了你', 'quoted'),
    ('合成用户链接到了你的帖子', 'linked'),
])
def test_anchored_personal_headers_are_recognized(header, kind):
    raw = message()
    raw['text'] = header + '\n' + BODY
    assert (parse_message(raw).kind, parse_message(raw).route) == (kind, 'personal')


@pytest.mark.parametrize(('header', 'kind'), [
    ('✉️ @fixture_actor 发给你 一条私信', 'private_message'),
    ('↩️ @fixture_actor 回复 给你', 'replied'),
    ('💬 @fixture_actor 提及 你', 'mentioned'),
    ('💬 @fixture_actor 引用 你', 'quoted'),
    ('🔗 @fixture_actor 链接 你的帖子', 'linked'),
])
@pytest.mark.parametrize('title_prefix', ['', '在 ', '从 '])
def test_two_line_templates_route_personal_notices_and_use_the_topic_header(header, kind, title_prefix):
    raw = two_line_message(header=header, title_prefix=title_prefix)

    notice = parse_message(raw)

    assert (notice.kind, notice.route, notice.actor) == (kind, 'personal', 'fixture_actor')
    assert (notice.topic_id, notice.post_number, notice.url) == ('9150', 3, 'https://linux.do/t/topic/9150/3')
    assert notice.title == '合成通知主题'


def test_body_reference_before_the_two_line_header_link_never_becomes_the_original_post():
    raw = two_line_message(body='合成通知主题是正文引用标签。\n' + BODY)
    raw['links'].insert(0, {
        'label': '合成通知主题', 'url': 'https://linux.do/t/topic/8001/2', 'header': False, 'line': 4,
    })

    notice = parse_message(raw)

    assert (notice.topic_id, notice.post_number) == ('9150', 3)
    assert notice.url == 'https://linux.do/t/topic/9150/3'
    assert notice.title == '合成通知主题'


@pytest.mark.parametrize('actor_in_body', [False, True])
def test_two_line_template_does_not_guess_a_missing_header_actor_from_body(actor_in_body):
    raw = two_line_message(header='💬 @fixture_actor 提及 你',
                           body='✉️ @fixture_actor 发给你 一条私信\n合成用户回复了你的帖子。')
    actor_link = raw['links'].pop(0)
    if actor_in_body:
        raw['links'].append(actor_link | {'header': False, 'line': 4})

    notice = parse_message(raw)

    assert (notice.kind, notice.route, notice.actor) == ('unknown', 'unknown', '')


def test_unconfirmed_published_header_stays_unknown_even_with_a_topic_action(store):
    raw = two_line_message(header='💬 @fixture_actor 发布了')
    raw['buttons'] = [{'label': '在线查看', 'url': 'https://linux.do/t/topic/9150/3'}]

    notice = parse_message(raw)
    saved = save(store, raw)

    assert (notice.kind, notice.route) == ('unknown', 'unknown')
    assert saved['unknown'] == 1 and saved['queued'] == 0
    assert rows(store, 'SELECT * FROM linuxdo_helper_events') == []
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert rows(store, 'SELECT * FROM linuxdo_evidence') == []


@pytest.mark.parametrize(('followed', 'post_number', 'kind', 'route'), [
    (None, 1, 'unknown', 'unknown'),
    (None, 3, 'unknown', 'unknown'),
    (frozenset(), 1, 'watching_first_post', 'board'),
    (frozenset(), 3, 'unknown', 'unknown'),
    (frozenset({'fixture_actor'}), 1, 'following_posted', 'personal'),
    (frozenset({'fixture_actor'}), 3, 'following_replied', 'personal'),
    (frozenset({'fixture_other'}), 1, 'watching_first_post', 'board'),
    (frozenset({'fixture_other'}), 3, 'unknown', 'unknown'),
])
def test_complete_following_list_disambiguates_the_same_published_template(
        followed, post_number, kind, route):
    notice = parse_message(published_message(post_number=post_number), followed=followed)

    assert (notice.kind, notice.route) == (kind, route)
    assert (notice.actor, notice.topic_id, notice.post_number) == ('fixture_actor', '9150', post_number)


def test_following_match_uses_decoded_casefolded_profile_username_not_display_label():
    raw = published_message()
    raw['text'] = raw['text'].replace('@fixture_actor', '合成显示名', 1)
    raw['links'][0].update(label='合成显示名', url='https://linux.do/u/FiXtUrE_%61CtOr/')

    notice = parse_message(raw, followed=frozenset({'fixture_actor'}))

    assert (notice.actor, notice.kind, notice.route) == ('FiXtUrE_aCtOr', 'following_posted', 'personal')


@pytest.mark.parametrize('mutation', [
    'missing', 'body-only', 'second-line', 'external', 'encoded-slash', 'duplicate', 'wrong-label',
])
def test_published_template_requires_one_valid_first_line_profile_link(mutation):
    raw = published_message(body='正文提到 @fixture_actor，不能据此当作关注作者。')
    raw['buttons'] = [{'label': '在线查看', 'url': 'https://linux.do/t/topic/9150/1'}]
    actor = raw['links'][0]
    if mutation == 'missing':
        raw['links'].pop(0)
    elif mutation == 'body-only':
        actor.update(header=False, line=4)
    elif mutation == 'second-line':
        actor['line'] = 2
    elif mutation == 'external':
        actor['url'] = 'https://example.invalid/u/fixture_actor'
    elif mutation == 'encoded-slash':
        actor['url'] = 'https://linux.do/u/fixture_actor%2Fprofile'
    elif mutation == 'duplicate':
        raw['links'].insert(0, dict(actor))
    else:
        actor['label'] = '@fixture_other'

    notice = parse_message(raw, followed=frozenset({'fixture_actor'}))

    assert (notice.kind, notice.route) == ('unknown', 'unknown')


def test_legacy_first_line_profile_link_without_line_metadata_remains_supported():
    raw = published_message()
    raw['links'][0].pop('line')

    assert parse_message(raw, followed=frozenset({'fixture_actor'})).route == 'personal'


def test_plain_text_board_actor_cannot_promote_a_notice_to_personal():
    raw = message(kind='watching_first_post', post_number=1)

    notice = parse_message(raw, followed=frozenset({'fixture_actor'}))

    assert (notice.kind, notice.route) == ('watching_first_post', 'board')


@pytest.mark.parametrize(('post_number', 'route'), [(1, 'board'), (3, 'unknown')])
def test_followed_person_mentioned_only_in_body_does_not_select_personal_route(post_number, route):
    raw = published_message(actor='fixture_other', post_number=post_number,
                            body='正文引用 @fixture_actor 的个人资料。')
    raw['links'].append({'label': '@fixture_actor', 'url': 'https://linux.do/u/fixture_actor',
                         'header': False, 'line': 4})

    notice = parse_message(raw, followed=frozenset({'fixture_actor'}))

    assert notice.actor == 'fixture_other'
    assert notice.route == route


@pytest.mark.parametrize('followed', [None, frozenset(), frozenset({'fixture_other'})])
def test_private_message_ignores_following_configuration_and_never_reaches_grok(store, followed):
    raw = two_line_message(body='合成私信正文只允许进入个人通知队列。')
    request = Mock(side_effect=AssertionError('private messages must not reach Grok'))

    saved = save(store, raw, followed=followed)
    reviewed = pipeline.review(store, sources={'mode': 'helper'}, config=policy.default_policy(),
                               model='fixture-model', request=request,
                               topic_fetch=Mock(side_effect=AssertionError('no private topic reads')),
                               official_fetch=Mock(side_effect=AssertionError('no private resource reads')))

    assert saved['personal'] == saved['queued'] == 1
    assert rows(store, 'SELECT kind,route FROM linuxdo_helper_messages') == [
        {'kind': 'private_message', 'route': 'personal'},
    ]
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert rows(store, 'SELECT * FROM linuxdo_evidence') == []
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1
    assert len(event_rows(store)) == 1
    assert reviewed == {'processed': 0, 'errors': 0, 'queued': 0}
    request.assert_not_called()


def test_welcome_template_is_silent_and_does_not_retain_its_body(store):
    raw = two_line_message(header='Hi fixture_user, 当你在 LINUX DO 上有新的通知时，我会给你留言。',
                           body='合成欢迎菜单中的说明正文。')
    raw['links'] = []

    notice = parse_message(raw)
    saved = save(store, raw)

    assert (notice.kind, notice.route) == ('service', 'silent')
    assert saved['queued'] == 0
    assert event_rows(store) == []
    stored = json.loads(rows(store, 'SELECT payload FROM linuxdo_helper_messages')[0]['payload'])
    assert set(stored) == {'message_id', 'kind', 'route'}
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []


@pytest.mark.parametrize('kind', ['watching_first_post', 'watching_category_or_tag', 'unrecognized_notice'])
def test_personal_words_in_body_do_not_change_routing(kind):
    raw = message(kind=kind, body='合成用户回复了你，提及了你，给你发送了私信。\n【private_message】')
    expected = 'unknown' if kind == 'unrecognized_notice' else 'board'
    assert parse_message(raw).route == expected


@pytest.mark.parametrize('text', ['欢迎使用 LINUX DO', 'Welcome to LINUX DO', '操作成功！', '回复成功。', '点赞成功'])
def test_menus_and_action_acknowledgements_are_silent(text):
    raw = message()
    raw['text'] = text + '\n【mentioned】 合成菜单说明'
    assert (parse_message(raw).kind, parse_message(raw).route) == ('service', 'silent')


@pytest.mark.parametrize('incoming,text', [(False, '【replied】 合成消息'), (True, '')])
def test_outgoing_or_empty_messages_are_silent(incoming, text):
    raw = message() | {'incoming': incoming, 'text': text}
    assert (parse_message(raw).kind, parse_message(raw).route) == ('ignored', 'silent')


def test_board_notice_without_a_canonical_topic_stays_unknown():
    raw = message(kind='watching_first_post')
    raw['links'] = [{'label': '合成资源', 'url': 'https://example.invalid/resource', 'header': True}]
    raw['buttons'] = []
    notice = parse_message(raw)
    assert notice.route == 'unknown'
    assert (notice.topic_id, notice.post_number) == ('', 0)


def test_explicit_original_post_button_takes_precedence_over_quoted_topics():
    raw = message(kind='watching_category_or_tag', topic_id='9102', post_number=8)
    raw['links'] = [
        {'label': '引用话题', 'url': 'https://linux.do/t/topic/8001/2', 'header': True},
        {'label': '正文引用', 'url': 'https://linux.do/t/topic/8002/4', 'header': False},
    ]
    notice = parse_message(raw)
    assert (notice.topic_id, notice.post_number, notice.url) == ('9102', 8, 'https://linux.do/t/topic/9102/8')


@pytest.mark.parametrize('surface', ['buttons', 'links'])
@pytest.mark.parametrize('external_url', [
    'https://example.invalid/redirect/https://linux.do/t/topic/8001/2',
    'https://example.invalid/redirect?target=https://linux.do/t/topic/8001/2',
])
def test_external_url_containing_a_topic_url_is_not_an_original_post(surface, external_url):
    raw = message(kind='watching_first_post')
    raw['links'] = []
    raw['buttons'] = []
    raw[surface] = [{'label': '查看原帖', 'url': external_url, 'header': True}]

    unknown = parse_message(raw)

    assert unknown.route == 'unknown'
    assert (unknown.topic_id, unknown.post_number, unknown.url) == ('', 0, '')
    raw['links'].append({'label': '当前合成话题', 'url': 'https://linux.do/t/topic/9102/8', 'header': True})
    recognized = parse_message(raw)
    assert recognized.route == 'board'
    assert (recognized.topic_id, recognized.post_number, recognized.url) == ('9102', 8, 'https://linux.do/t/topic/9102/8')
    assert recognized.title == '当前合成话题'


@pytest.mark.parametrize('matching_title_present', [True, False])
def test_title_belongs_to_the_selected_canonical_topic(matching_title_present):
    raw = message(kind='watching_category_or_tag', topic_id='9102', post_number=8)
    raw['links'] = [{'label': '被引用话题的标题', 'url': 'https://linux.do/t/topic/8001/2', 'header': True}]
    if matching_title_present:
        raw['links'].append({'label': '当前合成话题', 'url': 'https://linux.do/t/topic/9102/1', 'header': True})

    notice = parse_message(raw)

    assert (notice.topic_id, notice.post_number) == ('9102', 8)
    assert notice.title == ('当前合成话题' if matching_title_present else raw['text'].split('\n', 1)[0])
    assert notice.title != '被引用话题的标题'


def test_header_permalink_takes_precedence_over_body_topic_when_no_action_button():
    raw = message(kind='watching_first_post', topic_id='9103', post_number=1)
    raw['buttons'] = []
    raw['links'].insert(0, {'label': '引用', 'url': 'https://linux.do/t/topic/8001/7', 'header': False})
    assert (parse_message(raw).topic_id, parse_message(raw).post_number) == ('9103', 1)


def test_notification_identity_comes_from_the_original_link_not_a_quoted_link():
    raw = message()
    raw['buttons'][0]['url'] += '?notification_id=70001'
    raw['links'].insert(0, {'label': '引用通知', 'url': 'https://linux.do/t/topic/8001/2?notification_id=70002',
                            'header': True})
    notice = parse_message(raw)
    assert notice.notification_id == '70001'
    assert (notice.topic_id, notice.post_number) == ('9101', 3)


def test_body_notification_query_does_not_create_a_false_deduplication_identity():
    raw = message()
    raw['links'].append({'label': '引用通知', 'url': 'https://linux.do/t/topic/8001/2?notification_id=70002',
                         'header': False})
    assert parse_message(raw).notification_id == ''


def test_envelope_decodes_utf16_entities_and_keeps_callback_buttons_inert():
    text = '【mentioned】 @fixture_actor 🔔 合成话题\n参考 https://example.invalid/reference'
    prefix, title = text.split('合成话题', 1)[0], '合成话题'
    body_url = 'https://example.invalid/reference'
    entity_text_url = type('MessageEntityTextUrl', (SimpleNamespace,), {})
    entity_url = type('MessageEntityUrl', (SimpleNamespace,), {})
    click = Mock(side_effect=AssertionError('callback buttons must never be invoked'))
    raw = envelope(SimpleNamespace(
        id=101, out=False, sender_id=90002, peer_id=SimpleNamespace(user_id=90002),
        message=text, date=datetime.fromisoformat(PUBLISHED_AT), edit_date=None,
        entities=[
            entity_text_url(offset=len(prefix.encode('utf-16-le')) // 2,
                            length=len(title.encode('utf-16-le')) // 2,
                            url='https://linux.do/t/topic/9104/9'),
            entity_url(offset=len(text[:text.index(body_url)].encode('utf-16-le')) // 2,
                       length=len(body_url)),
        ],
        reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=[
            SimpleNamespace(text='查看原帖', url='https://linux.do/t/topic/9104/9'),
            SimpleNamespace(text='回复', data=b'fixture-callback', click=click),
        ])]),
    ), 90002)

    assert raw['links'] == [
        {'label': title, 'url': 'https://linux.do/t/topic/9104/9', 'header': True, 'line': 1},
        {'label': body_url, 'url': body_url, 'header': False, 'line': 2},
    ]
    assert raw['buttons'] == [{'label': '查看原帖', 'url': 'https://linux.do/t/topic/9104/9'}]
    assert (parse_message(raw).route, parse_message(raw).post_number) == ('personal', 9)
    click.assert_not_called()


def test_nested_inline_url_buttons_are_kept_and_callback_actions_remain_inert():
    source = two_line_message()
    entity_type = type('MessageEntityTextUrl', (SimpleNamespace,), {})
    button_type = type('KeyboardInlineButton', (SimpleNamespace,), {})
    url_action_type = type('InlineButtonTypeUrl', (SimpleNamespace,), {})
    callback_action_type = type('InlineButtonTypeCallback', (SimpleNamespace,), {})
    text = source['text']
    entities = [entity_type(
        offset=len(text[:text.index(link['label'])].encode('utf-16-le')) // 2,
        length=len(link['label'].encode('utf-16-le')) // 2, url=link['url'],
    ) for link in source['links']]
    click = Mock(side_effect=AssertionError('Helper callback actions must remain untouched'))
    buttons = [
        button_type(text='在线查看', type=url_action_type(url='https://linux.do/t/topic/9150/3')),
        button_type(text='合成资料', type=url_action_type(url='https://example.invalid/resource')),
        button_type(text='回复', type=callback_action_type(data=b'fixture-reply'), click=click),
        button_type(text='查看原帖', type=callback_action_type(data=b'https://linux.do/t/topic/8001/2'), click=click),
    ]

    raw = envelope(SimpleNamespace(
        id=source['message_id'], out=False, sender_id=IDENTITY['bot_id'],
        peer_id=SimpleNamespace(user_id=IDENTITY['bot_id']), message=text,
        date=datetime.fromisoformat(PUBLISHED_AT), edit_date=None, entities=entities,
        reply_markup=SimpleNamespace(rows=[SimpleNamespace(buttons=buttons)]),
    ), IDENTITY['bot_id'])

    assert raw['links'] == source['links']
    assert raw['buttons'] == [
        {'label': '在线查看', 'url': 'https://linux.do/t/topic/9150/3'},
        {'label': '合成资料', 'url': 'https://example.invalid/resource'},
    ]
    notice = parse_message(raw)
    assert (notice.kind, notice.route, notice.actor) == ('private_message', 'personal', 'fixture_actor')
    assert 'https://example.invalid/resource' in notice.links
    assert 'https://linux.do/t/topic/8001/2' not in notice.links
    click.assert_not_called()


@pytest.mark.parametrize(('outgoing', 'sender', 'peer', 'incoming'), [
    (False, 90002, 90002, True), (True, 90002, 90002, False),
    (False, 90003, 90002, False), (False, 90002, 90003, False),
])
def test_envelope_accepts_only_incoming_messages_from_the_pinned_dialog(outgoing, sender, peer, incoming):
    raw = envelope(SimpleNamespace(id=101, out=outgoing, sender_id=sender,
                                   peer_id=SimpleNamespace(user_id=peer), message='【mentioned】 合成通知'), 90002)
    assert raw['incoming'] is incoming
    assert parse_message(raw).route == ('personal' if incoming else 'silent')


def judgment(snapshot, *, change_kind='initial'):
    evidence = snapshot['evidence'][0]
    return {
        'decision': 'push', 'categories': ['C2'], 'content_type': 'tutorial',
        'reason': '脚本提供可复用的顺序操作方法。', 'action': '阅读资源页中的操作步骤。',
        'missing_evidence': [], 'expires_at': '', 'change_kind': change_kind,
        'provider': '', 'region_scope': 'unknown',
        'facts': [{'category': 'C2', 'kind': 'method', 'subject': '合成脚本',
                   'value': '按顺序执行三个操作',
                   'evidence': [{'ref': evidence['ref'], 'quote': '合成脚本可按顺序执行三个操作。'}]}],
    }


def process(store, topic_id='9101', *, request=None):
    return pipeline.process_topic(store, topic_id, policy.default_policy(), model='fixture-model',
                                  request=request or (lambda snapshot, config: judgment(snapshot)),
                                  topic_fetch=lambda topic_id, numbers: [], official_fetch=lambda snapshot: [])


def test_personal_notice_has_no_model_evidence_or_value_judgment(store):
    raw = message(body='合成个人正文只应保留于独立消息表。')
    saved = save(store, raw)
    request = Mock(side_effect=AssertionError('personal notices must not reach the value model'))

    reviewed = pipeline.review(store, sources={'mode': 'helper'}, config=policy.default_policy(),
                               model='fixture-model', request=request,
                               topic_fetch=Mock(side_effect=AssertionError('no topic reads')),
                               official_fetch=Mock(side_effect=AssertionError('no official reads')))

    assert saved['personal'] == saved['queued'] == 1
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert rows(store, 'SELECT * FROM linuxdo_evidence') == []
    assert rows(store, 'SELECT * FROM linuxdo_topics') == []
    assert rows(store, 'SELECT * FROM linuxdo_decisions') == []
    assert rows(store, 'SELECT * FROM linuxdo_notifications') == []
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_messages')) == 1
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1
    events = event_rows(store)
    assert len(events) == 1
    assert '合成个人正文只应保留于独立消息表。' in json.loads(events[0]['payload'])['text']
    assert events[0]['status'] == 'pending'
    assert reviewed == {'processed': 0, 'errors': 0, 'queued': 0}
    request.assert_not_called()


def test_personal_notice_preserves_external_urls_hidden_behind_body_anchor_text(store):
    raw = message(body='合成资料见下载页面。')
    resource = 'https://example.invalid/download?item=fixture'
    raw['links'].extend([
        {'label': '下载页面', 'url': resource, 'header': False},
        {'label': '同一资料', 'url': resource, 'header': False},
    ])
    assert resource not in raw['text']

    assert save(store, raw)['queued'] == 1

    delivered_text = json.loads(event_rows(store)[0]['payload'])['text']
    assert '合成资料见下载页面。' in delivered_text
    assert delivered_text.count(resource) == 1
    assert parse_message(raw).url in delivered_text
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []


@pytest.mark.parametrize('incoming', [True, False])
def test_silent_messages_do_not_retain_body_or_original_envelope(store, incoming):
    raw = message() | {'incoming': incoming, 'text': '回复成功\n合成服务正文不应保留。'}

    assert save(store, raw)['queued'] == 0

    stored = rows(store, 'SELECT route,payload FROM linuxdo_helper_messages')[0]
    payload = json.loads(stored['payload'])
    assert stored['route'] == 'silent'
    assert set(payload) == {'message_id', 'kind', 'route'}
    assert '合成服务正文不应保留。' not in stored['payload']
    assert event_rows(store) == []
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []


def test_replayed_semantic_personal_notice_and_edits_never_enqueue_twice(store):
    first = message()
    assert save(store, first)['queued'] == 1
    reopened = Store(Outbox(store.box.path))
    assert save(reopened, first)['collected'] == 0
    assert save(reopened, message(102, actor='FIXTURE_ACTOR'))['queued'] == 0
    edited = first | {'text': first['text'] + '\n合成编辑内容', 'edited_at': EDITED_AT}
    assert save(reopened, edited)['queued'] == 0
    assert len(rows(reopened, 'SELECT * FROM linuxdo_helper_messages')) == 2
    assert len(rows(reopened, 'SELECT * FROM linuxdo_helper_events')) == 1
    assert len(event_rows(reopened)) == 1
    assert '合成编辑内容' not in json.loads(event_rows(reopened)[0]['payload'])['text']


def test_one_notification_id_across_message_ids_is_not_resent_without_an_actor(store):
    first = message(kind='mentioned')
    first['text'] = '【mentioned】 合成话题\n' + BODY
    first['buttons'][0]['url'] += '?notification_id=70001'
    assert parse_message(first).actor == ''
    assert save(store, first)['queued'] == 1
    second = copy.deepcopy(first)
    second.update(message_id=102, text=first['text'] + '\n合成补充说明')

    assert save(Store(Outbox(store.box.path)), second)['queued'] == 0

    assert len(rows(store, 'SELECT * FROM linuxdo_helper_messages')) == 2
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == len(event_rows(store)) == 1


def test_personal_replies_at_different_post_numbers_are_distinct_events(store):
    assert save(store, message(101, post_number=3), message(102, post_number=4))['queued'] == 2
    events = rows(store, 'SELECT post_number,event_id FROM linuxdo_helper_events ORDER BY post_number')
    assert [row['post_number'] for row in events] == [3, 4]
    assert len({row['event_id'] for row in events}) == 2


def test_personal_message_cannot_become_model_input_after_a_board_shaped_edit(store):
    save(store, message())
    edited = message(kind='watching_first_post') | {'edited_at': EDITED_AT}
    assert save(store, edited)['queued'] == 0
    assert rows(store, 'SELECT route FROM linuxdo_helper_messages') == [{'route': 'personal'}]
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert rows(store, 'SELECT * FROM linuxdo_evidence') == []
    assert len(event_rows(store)) == 1


def test_board_notice_enters_existing_value_pipeline(store, clock):
    saved = save(store, message(kind='watching_category_or_tag'))
    request = Mock(side_effect=lambda snapshot, config: judgment(snapshot))

    result = process(store, request=request)

    assert saved['board'] == 1 and saved['queued'] == 0
    request.assert_called_once()
    snapshot = request.call_args.args[0]
    assert snapshot['evidence'][0]['ref'] == f'tg:{SOURCE}:101'
    assert BODY in snapshot['evidence'][0]['body']
    assert result['decision'] == 'push' and not result['error']
    assert result['event_id'] is not None
    assert store.box.counts() == {'pending': 1}


def test_board_edit_moves_the_message_and_evidence_to_its_new_topic(store, clock):
    save(store, message(kind='watching_first_post', topic_id='9101'))
    old_snapshot = store.snapshot('9101')
    edited = message(kind='watching_first_post', topic_id='9102', post_number=4) | {'edited_at': EDITED_AT}

    assert save(store, edited)['board'] == 1

    observations = rows(store, 'SELECT channel,message_id,topic_id FROM linuxdo_observations')
    assert observations == [{'channel': SOURCE, 'message_id': 101, 'topic_id': '9102'}]
    assert store.snapshot('9101')['evidence'] == []
    assert store.snapshot('9101')['version'] > old_snapshot['version']
    updated = store.snapshot('9102')
    assert [(row['ref'], row['post_number']) for row in updated['evidence']] == [(f'tg:{SOURCE}:101', 4)]
    assert store.finish(old_snapshot, judgment(old_snapshot), message='合成过时判断') is None
    clock[0] += 31
    assert store.claim(source=SOURCE) == ['9102']
    assert process(store, '9102')['decision'] == 'push'
    assert rows(store, 'SELECT * FROM linuxdo_notifications WHERE topic_id=?', ('9101',)) == []


def test_personal_before_board_suppresses_duplicate_without_calling_model(store, clock):
    save(store, message())
    save(store, message(102, kind='watching_category_or_tag'))
    request = Mock(side_effect=AssertionError('already covered personal post needs no model request'))

    result = process(store, request=request)

    assert result['decision'] == 'skip' and result['event_id'] is None
    request.assert_not_called()
    assert len(event_rows(store)) == 1
    assert rows(store, 'SELECT * FROM linuxdo_notifications') == []


def test_later_board_publication_of_the_same_post_preview_stays_silent(store, clock):
    save(store, message(kind='private_message'))
    later = message(102, kind='watching_category_or_tag') | {'published_at': EDITED_AT}
    save(store, later)
    request = Mock(side_effect=AssertionError('later publication of an unchanged preview is still a duplicate'))

    result = process(store, request=request)

    assert result['decision'] == 'skip' and result['event_id'] is None
    request.assert_not_called()
    assert len(event_rows(store)) == 1
    assert rows(store, 'SELECT * FROM linuxdo_notifications') == []


def test_two_line_personal_header_does_not_make_the_same_board_preview_look_new(store, clock):
    save(store, two_line_message())
    board = message(302, kind='watching_category_or_tag', topic_id='9150')
    board['text'] = '【watching_category_or_tag】 @fixture_actor 合成通知主题\n' + BODY
    board['links'][0]['label'] = '合成通知主题'
    board['published_at'] = EDITED_AT
    save(store, board)
    request = Mock(side_effect=AssertionError('a topic heading is not new post content'))

    result = process(store, '9150', request=request)

    assert result['decision'] == 'skip' and result['event_id'] is None
    request.assert_not_called()
    assert len(event_rows(store)) == 1
    assert rows(store, 'SELECT * FROM linuxdo_notifications') == []


@pytest.mark.parametrize('change', ['later-preview', 'edited-preview'])
def test_new_board_preview_after_personal_notice_can_be_judged_without_private_body(store, clock, change):
    private_body = '合成私信正文不可作为模型输入。'
    personal = message(kind='private_message', body=private_body)
    save(store, personal)
    if change == 'edited-preview':
        save(store, message(102, kind='watching_category_or_tag', body=private_body))
        assert process(store, request=Mock(side_effect=AssertionError('unchanged preview needs no judgment')))['decision'] == 'skip'
        board = message(102, kind='watching_category_or_tag') | {'edited_at': EDITED_AT}
    else:
        board = message(102, kind='watching_category_or_tag') | {'published_at': EDITED_AT}
    save(store, board)
    request = Mock(side_effect=lambda snapshot, config: judgment(snapshot, change_kind='new_method'))

    result = process(store, request=request)

    request.assert_called_once()
    assert result['decision'] == 'push' and result['event_id'] is not None
    assert not result['error']
    snapshot = request.call_args.args[0]
    assert private_body not in json.dumps(snapshot, ensure_ascii=False)
    assert private_body not in judge.build_prompt(snapshot, policy.default_policy())
    assert len(snapshot['personal_posts']) == 1
    metadata = snapshot['personal_posts'][0]
    assert set(metadata) == {'post_number', 'source_time', 'post_fingerprint'}
    assert len(metadata['post_fingerprint']) == 64
    assert len(event_rows(store)) == 2


def test_personal_notice_supersedes_an_unsent_board_notification(store, clock):
    save(store, message(kind='watching_category_or_tag'))
    board_event = process(store)['event_id']
    assert board_event is not None

    result = save(store, message(102))

    assert result['queued'] == 1
    events = {row['id']: row for row in event_rows(store)}
    assert events[board_event]['status'] == 'suppressed'
    personal_event = rows(store, 'SELECT event_id FROM linuxdo_helper_events')[0]['event_id']
    assert personal_event != board_event
    assert events[personal_event]['status'] == 'pending'


@pytest.mark.parametrize('delivery_status', ['delivered', 'sending', 'uncertain'])
def test_personal_notice_does_not_duplicate_board_already_sent_or_sending(store, clock, delivery_status):
    save(store, message(kind='watching_category_or_tag'))
    board_event = process(store)['event_id']
    store.box.finish(board_event, delivery_status, receipt={'message_id': 501} if delivery_status == 'delivered' else None)

    assert save(store, message(102))['queued'] == 0

    assert len(event_rows(store)) == 1
    assert rows(store, 'SELECT event_id FROM linuxdo_helper_events') == [{'event_id': board_event}]
    assert event_rows(store)[0]['status'] == delivery_status


def test_personal_arriving_during_board_judgment_invalidates_the_stale_push(store, clock):
    save(store, message(kind='watching_category_or_tag'))

    def request(snapshot, config):
        decision = judgment(snapshot)
        save(store, message(102))
        return decision

    result = process(store, request=request)

    assert result['event_id'] is None
    assert rows(store, 'SELECT * FROM linuxdo_notifications') == []
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1
    assert len(event_rows(store)) == 1


def test_personal_coverage_only_blocks_facts_from_the_same_reply(store, clock):
    save(store, message(post_number=3))
    save(store, message(102, kind='watching_category_or_tag', post_number=4))
    snapshot = store.snapshot('9101')

    result = judge.validate_result(judgment(snapshot, change_kind='new_method'), snapshot, policy.default_policy())

    assert result['decision'] == 'push'


def test_unknown_edit_is_reparsed_once_and_closes_the_format_alert(store):
    save(store, message(kind='new_fixture_notice'))
    assert len(event_rows(store)) == 1
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert save(store, message(kind='mentioned') | {'edited_at': EDITED_AT})['queued'] == 1
    assert rows(store, 'SELECT route FROM linuxdo_helper_messages') == [{'route': 'personal'}]
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1
    assert json.loads(rows(store, "SELECT value FROM linuxdo_meta WHERE key='helper:format_alert'")[0]['value']) is False
    assert save(store, message(kind='mentioned') | {'edited_at': EDITED_AT})['queued'] == 0
    assert len(event_rows(store)) == 2


@pytest.mark.parametrize(('recognized_kind', 'route', 'queued', 'observations'), [
    ('mentioned', 'personal', 1, 0), ('watching_first_post', 'board', 0, 1),
])
def test_unknown_unchanged_message_is_reparsed_after_parser_support_is_added(
        store, monkeypatch, recognized_kind, route, queued, observations):
    raw = message(kind='new_fixture_notice')
    save(store, raw)
    original_kind = helper._kind
    monkeypatch.setattr(helper, '_kind', lambda header: recognized_kind if header.startswith('【new_fixture_notice】') else original_kind(header))

    result = save(Store(Outbox(store.box.path)), raw)

    assert result['queued'] == queued
    assert rows(store, 'SELECT route FROM linuxdo_helper_messages') == [{'route': route}]
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == queued
    assert len(rows(store, 'SELECT * FROM linuxdo_observations')) == observations


def test_repeated_unknown_notices_raise_one_alert_until_the_condition_clears(store):
    save(store, message(kind='unknown_fixture'))
    save(store, message(102, kind='unknown_fixture'))
    save(store, message(102, kind='unknown_fixture'))
    assert len(event_rows(store)) == 1
    assert rows(store, 'SELECT * FROM linuxdo_helper_events') == []


def test_importing_an_empty_complete_list_reparses_unknown_first_posts_as_board(store):
    raw = published_message()
    assert save(store, raw)['unknown'] == 1
    imported = HelperStore(Store(Outbox(store.box.path)), followed=frozenset())

    result = imported.reparse_pending()

    assert result['collected'] == result['board'] == 1
    assert result['queued'] == 0
    assert rows(store, 'SELECT route FROM linuxdo_helper_messages') == [{'route': 'board'}]
    assert len(rows(store, 'SELECT * FROM linuxdo_observations')) == 1
    assert rows(store, 'SELECT * FROM linuxdo_helper_events') == []
    assert imported.reparse_pending() == {'collected': 0, 'queued': 0}


class FakeAccount:
    def __init__(self, pages, *, identity=None, before_page=None, enter_error=None):
        self.identity = dict(IDENTITY if identity is None else identity)
        self.pages = list(pages)
        self.calls = []
        self.entered = 0
        self.exited = 0
        self.before_page = before_page
        self.enter_error = enter_error

    async def __aenter__(self):
        self.entered += 1
        if self.enter_error:
            raise self.enter_error
        return self

    async def __aexit__(self, *_):
        self.exited += 1

    async def page(self, **kwargs):
        self.calls.append(kwargs)
        if self.before_page:
            self.before_page(kwargs)
        if not self.pages:
            raise AssertionError('unexpected extra Helper page')
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def collect(store, account, **config):
    return asyncio.run(helper_collect.collect_async(store, collection_config(**config), account_factory=lambda: account))


@pytest.mark.parametrize('previous_followed', [None, frozenset()])
def test_new_following_fingerprint_reparses_old_unknown_once_without_a_recent_read(
        store, clock, following_root, previous_followed):
    raw = published_message(post_number=3)
    newer = message(401) | {'text': '操作成功'}
    assert save(store, raw, newer, followed=previous_followed)['unknown'] == 1
    before = json.loads(rows(store, 'SELECT payload FROM linuxdo_helper_messages WHERE message_id=301')[0]['payload'])
    write_following(following_root, ['fixture_actor'])
    account = FakeAccount([[]])

    result = collect(Store(Outbox(store.box.path)), account)

    assert result['error'] == '' and result['changed'] == result['queued'] == 1
    assert result['following']['configured'] is True
    assert result['following']['count'] == 1
    assert account.calls == [{'after': 401, 'limit': 2}]
    assert store.checkpoint(SOURCE)['cursor'] == 401
    updated = rows(store, 'SELECT kind,route,payload FROM linuxdo_helper_messages WHERE message_id=301')[0]
    assert (updated['kind'], updated['route']) == ('following_replied', 'personal')
    assert json.loads(updated['payload'])['_parser'] != before['_parser']
    assert json.loads(updated['payload'])['_raw'] == raw
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert rows(store, 'SELECT * FROM linuxdo_evidence') == []
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1

    repeated = collect(Store(Outbox(store.box.path)), FakeAccount([[]]))
    assert repeated['changed'] == repeated['queued'] == 0
    assert save(store, raw, followed=frozenset({'fixture_other'}))['queued'] == 0
    assert rows(store, 'SELECT route FROM linuxdo_helper_messages WHERE message_id=301') == [{'route': 'personal'}]
    assert rows(store, 'SELECT * FROM linuxdo_evidence') == []
    assert save(store, raw, raw | {'message_id': 402},
                followed=frozenset({'fixture_actor', 'fixture_other'}))['queued'] == 0
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1
    assert len(event_rows(store)) == 2  # One format alert and one personal notification.


def test_corrupt_following_snapshot_does_not_interrupt_personal_collection(store, clock, following_root):
    path = following_root / 'following.json'
    path.write_text('{"usernames": ["fixture_private_name"]')
    path.chmod(0o600)
    HelperStore(store).save_page([], checkpoint())
    private = two_line_message(301, body='合成私信正文不可出现在采集状态中。')
    ambiguous = published_message(302)
    account = FakeAccount([[private, ambiguous], []])

    result = collect(store, account)

    assert result['error'] == '' and result['caught_up'] is True
    assert result['changed'] == 2 and result['queued'] == 1
    assert result['following'] == {'configured': False, 'error': 'JSONDecodeError'}
    assert store.checkpoint(SOURCE)['cursor'] == 302
    assert rows(store, 'SELECT message_id,route FROM linuxdo_helper_messages ORDER BY message_id') == [
        {'message_id': 301, 'route': 'personal'}, {'message_id': 302, 'route': 'unknown'},
    ]
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert rows(store, 'SELECT * FROM linuxdo_evidence') == []
    assert 'fixture_private_name' not in json.dumps(result)
    assert private['text'] not in json.dumps(result, ensure_ascii=False)


def test_helper_check_uses_the_list_but_outputs_no_names_message_content_or_links(
        following_root, monkeypatch, capsys):
    write_following(following_root, ['fixture_actor', 'fixture_private_name'])
    samples = [published_message(), published_message(302, actor='fixture_other'),
               published_message(303, actor='fixture_other', post_number=3), two_line_message(304)]
    account = FakeAccount([samples])
    account.read_marker = AsyncMock(return_value=901)
    inspect = helper_collect.inspect_account
    monkeypatch.setattr(helper_collect, 'inspect_account', lambda: inspect(account_factory=lambda: account))

    assert linuxdo_main.main(['helper-check']) == 0

    output = capsys.readouterr()
    result = json.loads(output.out)
    assert output.err == ''
    assert result['authorized'] is True and result['sample_count'] == 4
    assert result['routes'] == {'personal:following_posted': 1, 'board:watching_first_post': 1,
                                'unknown:unknown': 1, 'personal:private_message': 1}
    assert result['following']['configured'] is True and result['following']['count'] == 2
    assert result['read_marker_unchanged'] is True
    assert account.read_marker.await_count == 2
    assert account.calls == [{'limit': 100, 'recent': True}]
    for private_value in ('fixture_actor', 'fixture_other', 'fixture_private_name', BODY,
                          '合成通知主题', 'https://linux.do/', 'https://example.invalid/'):
        assert private_value not in output.out


@pytest.mark.parametrize('list_state', ['missing', 'partial', 'corrupt'])
def test_helper_baseline_cli_refuses_an_unavailable_complete_list_before_collection(
        store, following_root, monkeypatch, capsys, list_state):
    if list_state != 'missing':
        path = following_root / 'following.json'
        path.write_text('{"complete": false, "usernames": ["fixture_actor"]}'
                        if list_state == 'partial' else '{fixture-private-invalid-json')
        path.chmod(0o600)
    collector = Mock(side_effect=AssertionError('baseline cannot start without a complete list'))
    monkeypatch.setattr(helper_collect, 'collect', collector)

    with pytest.raises(SystemExit) as error:
        linuxdo_main.main(['helper-baseline', '--apply', '--store', str(store.box.path)])

    assert error.value.code == 2
    output = capsys.readouterr()
    assert 'requires a complete imported following list' in output.err
    assert 'fixture_actor' not in output.err and 'fixture-private' not in output.err
    assert store.checkpoint(SOURCE) is None
    assert event_rows(store) == []
    collector.assert_not_called()


@pytest.mark.parametrize('usernames', [[], ['fixture_actor']])
def test_helper_baseline_cli_accepts_an_imported_complete_list_without_replaying_history(
        store, following_root, monkeypatch, capsys, usernames):
    write_following(following_root, usernames)
    account = FakeAccount([[published_message()]])
    collector = helper_collect.collect
    monkeypatch.setattr(helper_collect, 'collect',
                        lambda target, config: collector(target, config, account_factory=lambda: account))
    monkeypatch.setattr(pipeline, 'source_config', collection_config)

    assert linuxdo_main.main(['helper-baseline', '--apply', '--store', str(store.box.path)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result['baseline'] is True and result['caught_up'] is True
    assert result['changed'] == result['queued'] == 0
    assert result['following']['configured'] is True
    assert result['following']['count'] == len(usernames)
    assert store.checkpoint(SOURCE) == checkpoint(301, baseline_id=301)
    assert rows(store, 'SELECT * FROM linuxdo_helper_messages') == []
    assert event_rows(store) == []
    assert account.calls == [{'limit': 1, 'recent': True}]

    with pytest.raises(SystemExit) as error:
        linuxdo_main.main(['helper-baseline', '--apply', '--store', str(store.box.path)])
    assert error.value.code == 2
    assert 'Helper baseline already exists' in capsys.readouterr().err
    assert account.calls == [{'limit': 1, 'recent': True}]


@pytest.mark.parametrize(('recognized_kind', 'route', 'queued', 'observations'), [
    ('mentioned', 'personal', 1, 0), ('watching_first_post', 'board', 0, 1),
])
def test_parser_upgrade_revisits_old_unknown_messages_outside_the_recent_window(
        store, clock, monkeypatch, recognized_kind, route, queued, observations):
    raw = message(kind='new_fixture_notice')
    recent = [message(number) | {'text': '操作成功'} for number in range(103, 203)]
    save(store, raw, *recent)
    before = json.loads(rows(store, 'SELECT payload FROM linuxdo_helper_messages WHERE message_id=101')[0]['payload'])
    assert before['_raw'] == raw
    assert before['_parser'] == helper_store.PARSER_VERSION
    monkeypatch.setattr(helper_store, 'PARSER_VERSION', 'fixture-parser-upgrade')
    original_kind = helper._kind
    monkeypatch.setattr(helper, '_kind', lambda header: recognized_kind if header.startswith('【new_fixture_notice】') else original_kind(header))
    account = FakeAccount([[], list(reversed(recent))])

    result = collect(Store(Outbox(store.box.path)), account, recheck_limit=100)

    assert result['error'] == '' and result['queued'] == queued
    assert account.calls == [{'after': 202, 'limit': 2}, {'limit': 100, 'recent': True}]
    assert store.checkpoint(SOURCE)['cursor'] == 202
    row = rows(store, 'SELECT route,payload FROM linuxdo_helper_messages WHERE message_id=101')[0]
    assert row['route'] == route
    retained = json.loads(row['payload'])
    assert retained['_raw'] == raw
    assert retained['_parser'] == 'fixture-parser-upgrade'
    assert retained['text'] == raw['text']
    assert len(rows(store, 'SELECT * FROM linuxdo_observations')) == observations
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == queued

    replay = collect(Store(Outbox(store.box.path)), FakeAccount([[], list(reversed(recent))]), recheck_limit=100)
    assert replay['error'] == '' and replay['queued'] == 0
    assert replay['changed'] == 0
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == queued
    assert len(event_rows(store)) == 1 + queued


def test_reparse_limit_does_not_let_an_unsupported_old_notice_starve_the_next_notice(store, monkeypatch):
    save(store, message(kind='still_unknown_fixture'), message(102, kind='new_fixture_notice'))
    monkeypatch.setattr(helper_store, 'PARSER_VERSION', 'fixture-parser-upgrade')
    original_kind = helper._kind
    monkeypatch.setattr(helper, '_kind', lambda header: 'mentioned' if header.startswith('【new_fixture_notice】') else original_kind(header))
    pending = HelperStore(Store(Outbox(store.box.path)))

    first = pending.reparse_pending(limit=1)
    second = pending.reparse_pending(limit=1)
    third = pending.reparse_pending(limit=1)

    assert first['collected'] == 1 and first['queued'] == 0
    assert second['collected'] == second['queued'] == 1
    assert third == {'collected': 0, 'queued': 0}
    assert rows(store, 'SELECT message_id,route FROM linuxdo_helper_messages ORDER BY message_id') == [
        {'message_id': 101, 'route': 'unknown'}, {'message_id': 102, 'route': 'personal'},
    ]
    assert store.checkpoint(SOURCE)['cursor'] == 102
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 1
    assert len(event_rows(store)) == 2


@pytest.mark.parametrize('history', [[message(110)], []])
def test_first_collection_only_sets_the_baseline_and_never_replays_history(store, clock, history):
    account = FakeAccount([history])

    result = collect(store, account)

    assert result['baseline'] is True and result['caught_up'] is True
    assert result['changed'] == result['queued'] == 0
    expected = 110 if history else 0
    assert store.checkpoint(SOURCE) == checkpoint(expected, baseline_id=expected)
    assert rows(store, 'SELECT * FROM linuxdo_helper_messages') == []
    assert rows(store, 'SELECT * FROM linuxdo_observations') == []
    assert event_rows(store) == []
    assert account.calls == [{'limit': 1, 'recent': True}]
    assert account.exited == 1


def test_page_budget_saves_each_page_before_advancing_and_resumes_without_a_gap(store, clock):
    HelperStore(store).save_page([], checkpoint())
    committed_cursors = []
    account = FakeAccount([[message(101), message(102, post_number=4)],
                           [message(103, post_number=5), message(104, post_number=6)]],
                          before_page=lambda kwargs: committed_cursors.append(store.checkpoint(SOURCE)['cursor']))

    result = collect(store, account, max_pages=2)

    assert result['pages'] == 2 and result['caught_up'] is False
    assert result['changed'] == result['queued'] == 4
    assert committed_cursors == [100, 102]
    assert [call['after'] for call in account.calls] == [100, 102]
    assert store.checkpoint(SOURCE)['cursor'] == 104
    second = FakeAccount([[message(105, post_number=7)]])
    resumed = collect(Store(Outbox(store.box.path)), second)
    assert second.calls == [{'after': 104, 'limit': 2}]
    assert resumed['caught_up'] is True and resumed['queued'] == 1
    assert store.checkpoint(SOURCE)['cursor'] == 105
    assert [row['message_id'] for row in rows(store, 'SELECT message_id FROM linuxdo_helper_messages ORDER BY message_id')] == list(range(101, 106))


def test_failed_page_transaction_keeps_committed_cursor_and_rolls_back_its_messages(store, clock):
    HelperStore(store).save_page([], checkpoint())
    with store.box.connect() as db:
        db.execute("""CREATE TRIGGER reject_helper_event BEFORE INSERT ON linuxdo_helper_events
                      WHEN NEW.message_id=103 BEGIN SELECT RAISE(ABORT, 'injected page failure'); END""")
    account = FakeAccount([[message(101), message(102, post_number=4)],
                           [message(103, post_number=5), message(104, post_number=6)]])

    result = collect(store, account)

    assert result['error'] == 'IntegrityError'
    assert result['changed'] == result['queued'] == 2
    assert store.checkpoint(SOURCE)['cursor'] == 102
    assert [row['message_id'] for row in rows(store, 'SELECT message_id FROM linuxdo_helper_messages ORDER BY message_id')] == [101, 102]
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == len(event_rows(store)) == 2
    assert store.checkpoint(SOURCE)['retry_at'] > clock[0]
    with store.box.connect() as db:
        db.execute('DROP TRIGGER reject_helper_event')
    clock[0] = store.checkpoint(SOURCE)['retry_at']
    retry = FakeAccount([[message(103, post_number=5), message(104, post_number=6)], []])
    recovered = collect(store, retry)
    assert retry.calls[0]['after'] == 102
    assert recovered['queued'] == 2 and recovered['error'] == ''
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 4


def test_checkpoint_failure_rolls_back_personal_outbox_and_board_evidence_together(store):
    HelperStore(store).save_page([], checkpoint())
    original = store.checkpoint(SOURCE)
    with store.box.connect() as db:
        db.execute("""CREATE TRIGGER reject_helper_checkpoint BEFORE UPDATE ON linuxdo_sources
                      BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END""")

    with pytest.raises(sqlite3.IntegrityError, match='injected checkpoint failure'):
        save(store, message(101), message(102, kind='watching_first_post', topic_id='9102'))

    assert store.checkpoint(SOURCE) == original
    for table in ('linuxdo_helper_messages', 'linuxdo_helper_events', 'linuxdo_observations', 'linuxdo_evidence', 'linuxdo_topics'):
        assert rows(store, f'SELECT * FROM {table}') == []
    assert event_rows(store) == []


@pytest.mark.parametrize('ids', [[100], [102, 101], [101, 101]])
def test_nonadvancing_or_unordered_pages_do_not_acknowledge_any_message(store, clock, ids):
    HelperStore(store).save_page([], checkpoint())
    result = collect(store, FakeAccount([[message(number) for number in ids]]))
    assert result['error'] == 'ValueError'
    assert store.checkpoint(SOURCE)['cursor'] == 100
    assert rows(store, 'SELECT * FROM linuxdo_helper_messages') == []
    assert event_rows(store) == []


def test_recent_recheck_never_imports_messages_above_cursor_or_at_baseline(store, clock):
    HelperStore(store).save_page([], checkpoint())
    edited = message(101) | {'text': message(101)['text'] + '\n合成编辑', 'edited_at': EDITED_AT}
    account = FakeAccount([[message(101), message(102, post_number=4)],
                           [message(105, post_number=8), edited, message(100, post_number=2)]])

    result = collect(store, account, max_pages=1, recheck_limit=3)

    assert result['caught_up'] is False and result['queued'] == 2
    assert store.checkpoint(SOURCE)['cursor'] == 102
    retained = rows(store, 'SELECT message_id,payload FROM linuxdo_helper_messages ORDER BY message_id')
    assert [row['message_id'] for row in retained] == [101, 102]
    assert json.loads(retained[0]['payload'])['edited_at'] == EDITED_AT
    assert len(rows(store, 'SELECT * FROM linuxdo_helper_events')) == 2
    assert account.calls == [{'after': 100, 'limit': 2}, {'limit': 3, 'recent': True}]


def test_network_failures_back_off_open_one_alert_and_recover_from_the_same_cursor(store, clock):
    HelperStore(store).save_page([], checkpoint())
    delays = []
    for index in range(4):
        result = collect(store, FakeAccount([OSError('synthetic connection failure')]))
        assert result['error'] == 'OSError'
        assert store.checkpoint(SOURCE)['cursor'] == 100
        delays.append(store.checkpoint(SOURCE)['retry_at'] - clock[0])
        blocked = FakeAccount([])
        assert collect(store, blocked)['deferred'] is True
        assert blocked.entered == 0
        assert len(event_rows(store)) == (1 if index >= 2 else 0)
        clock[0] = store.checkpoint(SOURCE)['retry_at']
    assert delays == [60, 120, 240, 480]

    restored = collect(store, FakeAccount([[]]))
    assert restored['error'] == '' and restored['caught_up'] is True
    assert store.checkpoint(SOURCE)['retry_at'] == 0
    assert rows(store, 'SELECT failures,alert_open FROM linuxdo_sources WHERE channel=?', (SOURCE,)) == [{'failures': 0, 'alert_open': 0}]
    assert len(event_rows(store)) == 2
    collect(store, FakeAccount([[]]))
    assert len(event_rows(store)) == 2


def test_rate_limit_respects_server_delay_without_sleeping_or_advancing_cursor(store, clock, monkeypatch):
    HelperStore(store).save_page([], checkpoint())
    sleep = Mock(side_effect=AssertionError('rate limits should defer the next run'))
    monkeypatch.setattr(helper_collect.asyncio, 'sleep', sleep)
    result = collect(store, FakeAccount([RateLimited(173)]))
    assert result['error'] == 'RateLimited'
    assert store.checkpoint(SOURCE)['cursor'] == 100
    assert store.checkpoint(SOURCE)['retry_at'] == clock[0] + 173
    sleep.assert_not_called()
    deferred = FakeAccount([])
    assert collect(store, deferred)['deferred'] is True
    assert deferred.entered == 0
    clock[0] += 173
    assert collect(store, FakeAccount([[]]))['error'] == ''
    assert event_rows(store) == []


def test_expired_authorization_alerts_once_and_preserves_source_identity_and_cursor(store, clock):
    HelperStore(store).save_page([], checkpoint())
    for _ in range(2):
        result = collect(store, FakeAccount([], enter_error=AuthorizationRequired('synthetic expired session')))
        assert result['error'] == 'AuthorizationRequired'
        assert store.checkpoint(SOURCE)['cursor'] == 100
        assert store.checkpoint(SOURCE)['identity'] == digest(IDENTITY)
        assert len(event_rows(store)) == 1
        clock[0] = store.checkpoint(SOURCE)['retry_at']
    assert collect(store, FakeAccount([[]]))['error'] == ''
    assert len(event_rows(store)) == 2


@pytest.mark.parametrize('field', ['account_id', 'bot_id'])
def test_changed_source_identity_is_rejected_before_reading_any_messages(store, clock, field):
    HelperStore(store).save_page([], checkpoint())
    identity = dict(IDENTITY)
    identity[field] += 1
    account = FakeAccount([[message(101)]], identity=identity)

    result = collect(store, account)

    assert result['error'] == 'AuthorizationRequired'
    assert account.calls == []
    assert store.checkpoint(SOURCE)['identity'] == digest(IDENTITY)
    assert store.checkpoint(SOURCE)['cursor'] == 100
    assert rows(store, 'SELECT * FROM linuxdo_helper_messages') == []
    assert len(event_rows(store)) == 1


def test_helper_store_rejects_changed_identity_and_backward_cursor_before_writing(store):
    save(store, message())
    original = store.checkpoint(SOURCE)
    for invalid in (original | {'identity': 'different-fixture-identity'}, original | {'cursor': 100}):
        with pytest.raises(ValueError):
            HelperStore(store).save_page([message(102, post_number=4)], invalid)
        assert store.checkpoint(SOURCE) == original
        assert len(rows(store, 'SELECT * FROM linuxdo_helper_messages')) == 1
        assert len(event_rows(store)) == 1


def test_helper_review_claims_only_topics_with_helper_board_input(store, clock):
    legacy = Observation('LinuxDoNew', 201, '9201', 1, '合成旧来源主题', BODY, [], PUBLISHED_AT,
                         'https://example.invalid/source/201', ['https://example.invalid/resource'])
    store.save_page('LinuxDoNew', Page([legacy], [201]), {'cursor': 201, 'baseline': True})
    save(store, message(kind='watching_first_post'))
    clock[0] += 31
    old = store.snapshot('9201')
    request = Mock(side_effect=lambda snapshot, config: judgment(snapshot))

    result = pipeline.review(store, sources={'mode': 'helper'}, config=policy.default_policy(),
                             model='fixture-model', request=request,
                             topic_fetch=lambda topic_id, numbers: [], official_fetch=lambda snapshot: [])

    assert result == {'processed': 1, 'errors': 0, 'queued': 1}
    request.assert_called_once()
    assert request.call_args.args[0]['topic_id'] == '9101'
    retained = store.snapshot('9201')
    assert retained['status'] == 'pending'
    assert retained['lease_until'] == old['lease_until'] == 0
    assert retained['attempts'] == old['attempts'] == 0
    assert retained['version'] == old['version']
    assert retained['notifications'] == []


def test_helper_review_leaves_old_source_only_pending_when_there_are_no_board_notices(store, clock):
    legacy = Observation('LinuxDoNew', 201, '9201', 1, '合成旧来源主题', BODY, [], PUBLISHED_AT,
                         'https://example.invalid/source/201', [])
    store.save_page('LinuxDoNew', Page([legacy], [201]), {'cursor': 201, 'baseline': True})
    save(store, message())
    clock[0] += 31
    request = Mock(side_effect=AssertionError('legacy-only candidates must stay pending'))

    result = pipeline.review(store, sources={'mode': 'helper'}, config=policy.default_policy(),
                             model='fixture-model', request=request,
                             topic_fetch=lambda topic_id, numbers: [], official_fetch=lambda snapshot: [])

    assert result == {'processed': 0, 'errors': 0, 'queued': 0}
    request.assert_not_called()
    assert store.snapshot('9201')['status'] == 'pending'
    assert store.snapshot('9201')['lease_until'] == 0


def test_authorize_with_a_real_controlling_tty_hides_inputs_and_writes_private_identity(tmp_path, monkeypatch):
    private = tmp_path / 'helper-private'
    fixture_api_id = '12345'
    fixture_api_hash = '0' * 32
    expected_credentials = {'api_id': int(fixture_api_id), 'api_hash': fixture_api_hash}
    expected_identity = IDENTITY | {'access_hash': 90003}
    client = SimpleNamespace(
        connect=AsyncMock(), disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(id=IDENTITY['account_id'], bot=False)),
        get_entity=AsyncMock(return_value=SimpleNamespace(
            id=IDENTITY['bot_id'], bot=True, username=helper_account.HELPER_USERNAME,
            access_hash=expected_identity['access_hash'])),
    )
    factory = Mock(return_value=client)
    monkeypatch.setattr(helper_account, 'private_root', lambda: private)
    monkeypatch.setattr(helper_account, '_client', factory)

    child, terminal = pty.fork()
    if child == 0:
        try:
            result = helper_account.authorize()
            assert result == 0
            factory.assert_called_once_with(private, expected_credentials)
            client.connect.assert_awaited_once()
            client.is_user_authorized.assert_awaited_once()
            client.get_me.assert_awaited_once()
            client.get_entity.assert_awaited_once_with(helper_account.HELPER_USERNAME)
            client.disconnect.assert_awaited_once()
        except BaseException as exc:
            os.write(2, ('authorization fixture failed: ' + type(exc).__name__ + '\n').encode())
            os._exit(1)
        os._exit(0)

    transcript = bytearray()
    inputs = [(b'API ID', fixture_api_id), (b'API hash', fixture_api_hash)]
    sent = 0
    child_status = None
    eof = False
    deadline = time.monotonic() + 15
    try:
        while child_status is None or not eof:
            remaining = deadline - time.monotonic()
            assert remaining > 0, 'synthetic Helper authorization timed out in its controlling tty'
            readable, _, _ = select.select([] if eof else [terminal], [], [], min(0.1, remaining))
            if readable:
                try:
                    chunk = os.read(terminal, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b''
                if chunk:
                    transcript.extend(chunk)
                else:
                    eof = True
            if not eof and sent < len(inputs) and inputs[sent][0] in transcript:
                os.write(terminal, (inputs[sent][1] + '\n').encode())
                sent += 1
            if child_status is None:
                finished, status = os.waitpid(child, os.WNOHANG)
                if finished == child:
                    child_status = status
    finally:
        os.close(terminal)
        if child_status is None:
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(child, 0)

    assert os.waitstatus_to_exitcode(child_status) == 0, transcript.decode(errors='replace')
    assert sent == 2
    assert '授权完成。'.encode() in transcript
    assert fixture_api_id.encode() not in transcript, 'API ID was echoed by the terminal'
    assert fixture_api_hash.encode() not in transcript, 'API hash was echoed by the terminal'
    assert json.loads((private / 'credentials.json').read_text()) == expected_credentials
    assert json.loads((private / 'identity.json').read_text()) == expected_identity
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    for name in ('credentials.json', 'identity.json', 'account.lock'):
        assert stat.S_IMODE((private / name).stat().st_mode) == 0o600
    assert not list(private.glob('account.session*'))


@pytest.mark.parametrize('error_form', ['rpc', 'translated-rpc', 'account', 'authorization'])
def test_authorization_error_output_contains_only_the_exception_code(monkeypatch, capsys, error_form):
    private_detail = 'fixture-input-must-stay-private\nRPC response: https://example.invalid/private-response'
    rpc_error = type('FixtureRpcError', (Exception,), {})(private_detail)
    if error_form == 'rpc':
        error, expected = rpc_error, 'FixtureRpcError'
    elif error_form == 'translated-rpc':
        error, expected = helper_account._translate_error(rpc_error), 'FixtureRpcError'
    elif error_form == 'account':
        error, expected = helper_account.AccountError(private_detail), 'AccountError'
    else:
        error, expected = AuthorizationRequired(private_detail), 'AuthorizationRequired'
    monkeypatch.setattr(helper_account, 'authorize', Mock(side_effect=error))

    result = helper_account.run_authorize()

    output = capsys.readouterr()
    assert result == 1
    assert output.out == f'授权未完成：{expected}\n'
    assert output.err == ''
    assert private_detail not in output.out
    assert 'fixture-input-must-stay-private' not in output.out


@pytest.fixture
def authorization_context(tmp_path, monkeypatch):
    private = tmp_path / 'helper-private'
    credentials = {'api_id': 12345, 'api_hash': '0' * 32}
    hidden_input = Mock(side_effect=[str(credentials['api_id']), credentials['api_hash']])
    terminal_output = []

    @contextlib.contextmanager
    def terminal_stream(*args, **kwargs):
        with io.StringIO() as stream:
            yield stream
            terminal_output.append(stream.getvalue())

    monkeypatch.setattr(helper_account, 'open', terminal_stream, raising=False)
    monkeypatch.setattr(helper_account, 'private_root', lambda: private)
    monkeypatch.setattr(helper_account.getpass, 'getpass', hidden_input)
    previous_umask = os.umask(0o077)
    try:
        yield SimpleNamespace(private=private, credentials=credentials, hidden_input=hidden_input,
                              terminal_output=terminal_output)
    finally:
        os.umask(previous_umask)


def authorized_client(*, connect_errors=None):
    return SimpleNamespace(
        connect=AsyncMock(side_effect=connect_errors), disconnect=AsyncMock(),
        is_user_authorized=AsyncMock(return_value=True),
        get_me=AsyncMock(return_value=SimpleNamespace(id=IDENTITY['account_id'], bot=False)),
        get_entity=AsyncMock(return_value=SimpleNamespace(
            id=IDENTITY['bot_id'], bot=True, username=helper_account.HELPER_USERNAME, access_hash=90003)),
    )


@pytest.mark.parametrize('failure_kind', ['TimeoutError', 'FixtureRpcError'])
def test_temporary_authorization_failure_keeps_private_api_configuration_for_retry(
        authorization_context, monkeypatch, capsys, failure_kind):
    context = authorization_context
    failure_type = TimeoutError if failure_kind == 'TimeoutError' else type('FixtureRpcError', (Exception,), {})
    client = authorized_client(connect_errors=[failure_type('synthetic private RPC response'), None])
    factory = Mock(return_value=client)
    monkeypatch.setattr(helper_account, '_client', factory)

    assert helper_account.run_authorize() == 1

    assert capsys.readouterr().out == f'授权未完成：{failure_kind}\n'
    credentials_file = context.private / 'credentials.json'
    assert json.loads(credentials_file.read_text()) == context.credentials
    assert stat.S_IMODE(credentials_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(context.private.stat().st_mode) == 0o700
    assert context.hidden_input.call_count == 2
    assert not (context.private / 'identity.json').exists()
    client.disconnect.assert_awaited_once()
    no_reprompt = Mock(side_effect=AssertionError('valid stored API credentials should be reused'))
    monkeypatch.setattr(helper_account.getpass, 'getpass', no_reprompt)

    assert helper_account.run_authorize() == 0

    no_reprompt.assert_not_called()
    assert factory.call_count == client.disconnect.await_count == 2
    assert all(call.args == (context.private, context.credentials) for call in factory.call_args_list)
    assert json.loads(credentials_file.read_text()) == context.credentials
    assert json.loads((context.private / 'identity.json').read_text()) == IDENTITY | {'access_hash': 90003}
    assert capsys.readouterr().out == ''
    assert not list(context.private.glob('account.session*'))


@pytest.mark.parametrize('existing_credentials', [False, True])
def test_explicit_invalid_api_error_removes_only_invalid_api_configuration(
        authorization_context, monkeypatch, capsys, existing_credentials):
    context = authorization_context
    context.private.mkdir(mode=0o700)
    identity_file = context.private / 'identity.json'
    identity_contents = json.dumps(IDENTITY | {'access_hash': 90003})
    identity_file.write_text(identity_contents)
    identity_file.chmod(0o600)
    credentials_file = context.private / 'credentials.json'
    if existing_credentials:
        credentials_file.write_text(json.dumps(context.credentials))
        credentials_file.chmod(0o600)
        context.hidden_input.side_effect = AssertionError('existing API credentials should be read privately')
    invalid_api = type('ApiIdInvalidError', (Exception,), {})('synthetic private API rejection body')
    client = authorized_client(connect_errors=[invalid_api])
    monkeypatch.setattr(helper_account, '_client', Mock(return_value=client))

    result = helper_account.run_authorize()

    assert result == 1
    assert capsys.readouterr().out == '授权未完成：ApiIdInvalidError\n'
    assert not credentials_file.exists()
    assert identity_file.read_text() == identity_contents
    assert stat.S_IMODE(identity_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(context.private.stat().st_mode) == 0o700
    assert (context.private / 'account.lock').exists()
    assert context.hidden_input.call_count == (0 if existing_credentials else 2)
    client.disconnect.assert_awaited_once()
    assert not list(context.private.glob('account.session*'))


def seed_private_account(context):
    context.private.mkdir(mode=0o700)
    files = {
        'credentials.json': json.dumps(context.credentials).encode(),
        'identity.json': json.dumps(IDENTITY | {'access_hash': 90003}).encode(),
        'account.session': b'fixture-only session state, never a real Telegram session',
    }
    for name, content in files.items():
        path = context.private / name
        path.write_bytes(content)
        path.chmod(0o600)
    return files


@pytest.mark.parametrize('failure_stage', ['network', 'phone-input', 'phone-rpc'])
def test_existing_api_configuration_survives_network_and_phone_errors(
        authorization_context, monkeypatch, capsys, failure_stage):
    context = authorization_context
    original_files = seed_private_account(context)
    client = authorized_client()
    client.send_code_request = AsyncMock()
    client.sign_in = AsyncMock()
    if failure_stage == 'network':
        client.connect.side_effect = ConnectionError('ApiIdInvalidError occurs only in this synthetic RPC body')
        context.hidden_input.side_effect = AssertionError('stored API configuration should not be requested again')
        expected_code = 'ConnectionError'
    else:
        client.is_user_authorized.return_value = False
        if failure_stage == 'phone-input':
            context.hidden_input.side_effect = ['fixture-not-a-phone-number']
            expected_code = 'AccountError'
        else:
            context.hidden_input.side_effect = ['+1 (202) 555-0100']
            client.send_code_request.side_effect = type('PhoneNumberInvalidError', (Exception,), {})(
                'fixture phone input and RPC response must remain private')
            expected_code = 'PhoneNumberInvalidError'
    monkeypatch.setattr(helper_account, '_client', Mock(return_value=client))

    result = helper_account.run_authorize()

    assert result == 1
    output = capsys.readouterr()
    assert output.out == f'授权未完成：{expected_code}\n'
    assert output.err == ''
    for name, content in original_files.items():
        assert (context.private / name).read_bytes() == content
        assert stat.S_IMODE((context.private / name).stat().st_mode) == 0o600
    assert context.hidden_input.call_count == (0 if failure_stage == 'network' else 1)
    if failure_stage == 'phone-rpc':
        client.send_code_request.assert_awaited_once_with('+12025550100')
    else:
        client.send_code_request.assert_not_awaited()
    client.sign_in.assert_not_awaited()
    client.get_me.assert_not_awaited()
    client.get_entity.assert_not_awaited()
    client.disconnect.assert_awaited_once()


def test_invalid_api_cleanup_preserves_existing_identity_session_and_account_lock(
        authorization_context, monkeypatch, capsys):
    context = authorization_context
    original_files = seed_private_account(context)
    context.hidden_input.side_effect = AssertionError('existing API configuration should be reused')
    invalid_api = type('ApiIdInvalidError', (Exception,), {})('fixture rejection contains no safe diagnostic text')
    client = authorized_client(connect_errors=[invalid_api])
    monkeypatch.setattr(helper_account, '_client', Mock(return_value=client))

    assert helper_account.run_authorize() == 1

    assert capsys.readouterr().out == '授权未完成：ApiIdInvalidError\n'
    assert not (context.private / 'credentials.json').exists()
    for name in ('identity.json', 'account.session'):
        assert (context.private / name).read_bytes() == original_files[name]
        assert stat.S_IMODE((context.private / name).stat().st_mode) == 0o600
    assert stat.S_IMODE((context.private / 'account.lock').stat().st_mode) == 0o600
    assert stat.S_IMODE(context.private.stat().st_mode) == 0o700
    context.hidden_input.assert_not_called()
    client.disconnect.assert_awaited_once()
