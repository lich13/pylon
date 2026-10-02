from __future__ import annotations

import json
import sqlite3

import pytest

from pylon.tasks.chiphell import categories
from pylon.outbox import Outbox


def test_single_category_can_be_silenced_without_touching_mixed_or_unknown():
    phone = {'title': 'iPhone 15 Pro 全新', 'body_excerpt': '明盘 5000'}
    mixed = {'title': 'iPad Air 和 MacBook 一起出', 'body_excerpt': '两个商品'}
    unknown = {'title': '闲置杂物', 'body_excerpt': '一些东西'}
    policy = {'silent_categories': ['phone_apple']}

    kept = categories.filter_normal_posts([phone, mixed, unknown], policy)

    assert phone not in kept
    assert {row['title'] for row in kept} == {mixed['title'], unknown['title']}
    assert categories.classify_post(mixed)['mixed'] is True
    assert categories.classify_post(unknown)['categories'] == [categories.UNKNOWN_KEY]


def test_unknown_segment_is_preserved_in_mixed_classification():
    result = categories.classify_post({
        'title': '两件物品一起出',
        'body_full': 'iPhone 15 Pro 5000\n机械键盘 300\n新奇设备 Alpha 100',
    })
    assert result['mixed'] is True
    assert categories.UNKNOWN_KEY in result['categories']


def test_clear_multi_item_body_keeps_non_silent_segment_and_formats_it():
    post = {
        'title': '两件物品一起出',
        'body_full': 'iPhone 15 Pro 5000\n机械键盘 300',
        'url': 'https://example.invalid/thread-1',
    }
    result = categories.filter_normal_posts([post], {'silent_categories': ['phone_apple']})
    assert len(result) == 1
    assert result[0]['category_segments'][0]['categories'] == ['computer_peripherals']


def test_policy_is_source_controlled_and_runtime_file_cannot_override(tmp_path, monkeypatch):
    path = tmp_path / 'category-policy.json'
    path.write_text(json.dumps({'silent_categories': ['phone_apple']}), encoding='utf-8')
    monkeypatch.setattr(categories, 'SILENT_CATEGORIES', ('computer_cpu',))

    policy = categories.load_policy()

    assert policy['silent_categories'] == ['computer_cpu']
    assert json.loads(path.read_text())['silent_categories'] == ['phone_apple']
    assert not hasattr(categories, 'set_category_silence')


def test_history_reader_is_read_only_and_counts_posts(tmp_path):
    queue = tmp_path / 'delivery.sqlite3'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE delivery_queue (dedupe_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER)')
        db.execute('INSERT INTO delivery_queue VALUES (?,?,?,?)', (
            'one', '## Chiphell 新帖\n1. [[上海]iPhone 15](https://example.invalid/t1) 摘要：全新', 1, None))
    before = queue.stat().st_mtime_ns
    posts = categories.history_posts(queue)
    counts = categories.category_counts(posts)
    assert len(posts) == 1
    assert posts[0]['title'] == '[上海]iPhone 15'
    assert counts['phone_apple'] == 1
    assert queue.stat().st_mtime_ns == before


def test_cli_category_report_does_not_need_telegram_profile(tmp_path):
    queue = tmp_path / 'delivery.sqlite3'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE delivery_queue (dedupe_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER)')
        db.execute('INSERT INTO delivery_queue VALUES (?,?,?,?)', (
            'one', '## Chiphell 新帖\n1. [[上海]iPhone 15](https://example.invalid/t1) 摘要：全新', 1, None))
    text, markup = categories.category_report(queue_path=queue)
    assert '手机·苹果' in text
    assert markup == {}


def test_empty_category_report_has_no_empty_keyboard(tmp_path):
    text, markup = categories.category_report(queue_path=tmp_path / 'missing.sqlite3')
    assert '已解析商品帖：0 条' in text
    assert markup == {}


@pytest.fixture
def review_box(tmp_path, monkeypatch):
    box = Outbox(tmp_path / 'outbox.sqlite3')
    monkeypatch.setattr(categories, 'profile_env', lambda _: {
        'TELEGRAM_BOT_TOKEN': 'fixture-token', 'TELEGRAM_HOME_CHANNEL': '123'})
    monkeypatch.setattr(categories, 'Outbox', lambda: box)
    return box


def sample(title='新奇设备 Alpha', **values):
    return {'id': 'fixture', 'title': title, 'body_full': '明盘 100 元', 'body_complete': True,
            'body_excerpt': '明盘 100 元', 'url': 'https://example.invalid/fixture', **values}


def callback_payload(box, post, action='mute'):
    fingerprint = categories.unknown_fingerprint(post)
    review = box.get(categories.UNKNOWN_REVIEW_PREFIX + fingerprint)
    box.finish(review['event_id'], 'delivered', receipt=[{'message_id': 88, 'chat_id': 123}])
    return {'data': categories.unknown_callback_data('default', action, fingerprint),
            'from': {'id': 123}, 'message': {'message_id': 88, 'chat': {'id': 123, 'type': 'private'}}}


def test_unknown_prompt_survives_restart_and_only_records_opinion(review_box):
    post = sample()
    assert categories.publish_unknown_prompts([post], box=review_box, now=100) == 1
    result = categories.callback('default', callback_payload(review_box, post))
    assert '已记录' in result['text']
    restarted = Outbox(review_box.path)
    key = categories.UNKNOWN_REVIEW_PREFIX + categories.unknown_fingerprint(post)
    assert restarted.get(key)['status'] == 'pending_mute'
    assert categories.load_policy()['silent_categories'] == []
    assert categories.publish_unknown_prompts([post], box=restarted, now=100000) == 0
    with review_box.connect() as db:
        assert db.execute('SELECT profile FROM events').fetchall()[0][0] == 'default'
        assert db.execute('SELECT count(*) FROM events').fetchone()[0] == 1


def test_pending_question_and_daily_limit_prevent_bursts(review_box):
    post, other = sample(), sample('不同设备 Beta')
    assert categories.publish_unknown_prompts([post, other], box=review_box, now=100) == 1
    assert categories.publish_unknown_prompts([other], box=review_box, now=100000) == 0
    categories.callback('default', callback_payload(review_box, post, 'keep'))
    assert categories.publish_unknown_prompts([other], box=review_box, now=101) == 0
    assert categories.publish_unknown_prompts([other], box=review_box, now=100000) == 1


def test_question_and_dedup_marker_commit_or_rollback_together(review_box):
    with review_box.connect() as db:
        db.execute("CREATE TRIGGER fail_review BEFORE INSERT ON kv WHEN NEW.key LIKE 'chiphell:unknown-review:%' BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='fixture failure'):
        categories.publish_unknown_prompts([sample()], box=review_box)
    assert review_box.counts() == {}
    with review_box.connect() as db:
        db.execute('DROP TRIGGER fail_review')
    assert categories.publish_unknown_prompts([sample()], box=review_box) == 1


def test_historical_unknowns_are_baselined_without_questions(review_box):
    old = sample('旧设备 Beta')
    assert categories.seed_unknown_history(review_box, [old]) == 1
    assert categories.seed_unknown_history(review_box, [sample()]) == 0
    assert categories.publish_unknown_prompts([old], box=review_box) == 0
    assert review_box.counts() == {}
    assert categories.publish_unknown_prompts([sample()], box=review_box) == 1


def test_unknown_title_variants_dedup_without_using_prices_or_locations():
    first = sample('[甲地] 全新 出新奇设备 Alpha 2 明盘100元')
    other = sample('[乙地] 闲置 新奇设备 Alpha 3 售价200元')
    assert categories.unknown_fingerprint(first) == categories.unknown_fingerprint(other)
    assert categories.unknown_fingerprint(sample('出点闲置')) == ''


@pytest.mark.parametrize('post', [
    sample('iPhone 15'), sample('MacBook 和 iPad'),
    sample(body_complete=False), sample(body_full=''),
])
def test_known_mixed_and_failed_fetches_do_not_trigger_questions(review_box, post):
    assert categories.publish_unknown_prompts([post], box=review_box) == 0
    assert review_box.counts() == {}


@pytest.mark.parametrize('change', ['sender', 'group', 'signature', 'message', 'profile', 'receipt'])
def test_callback_rejects_wrong_owner_signature_or_original_message(review_box, change):
    post = sample()
    categories.publish_unknown_prompts([post], box=review_box)
    payload = callback_payload(review_box, post)
    profile = 'default'
    if change == 'sender':
        payload['from']['id'] = 456
    elif change == 'group':
        payload['message']['chat']['type'] = 'group'
    elif change == 'signature':
        payload['data'] = payload['data'][:-1] + ('a' if payload['data'][-1] != 'a' else 'b')
    elif change == 'message':
        payload['message']['message_id'] += 1
    elif change == 'profile':
        profile = 'webops'
    else:
        review = review_box.get(categories.UNKNOWN_REVIEW_PREFIX + categories.unknown_fingerprint(post))
        review_box.finish(review['event_id'], 'uncertain', receipt=[])
    result = categories.callback(profile, payload)
    assert '已记录' not in result['text']
    assert review_box.get(categories.UNKNOWN_REVIEW_PREFIX + categories.unknown_fingerprint(post))['status'] == 'asked'


def test_repeat_click_does_not_reverse_recorded_opinion(review_box):
    post = sample()
    categories.publish_unknown_prompts([post], box=review_box)
    first = categories.callback('default', callback_payload(review_box, post, 'keep'))
    again = categories.callback('default', callback_payload(review_box, post, 'mute'))
    assert first == again
    assert categories.load_policy()['silent_categories'] == []


def test_retired_category_callback_cannot_mutate_policy(review_box):
    payload = {'data': 'ch1|mute|phone_apple|anything', 'from': {'id': 123},
               'message': {'chat': {'id': 123, 'type': 'private'}}}
    assert '入口已移除' in categories.callback('default', payload)['text']
    assert review_box.counts() == {}
