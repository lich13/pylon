from __future__ import annotations

import json
import sqlite3

from pylon.tasks.chiphell import categories


def test_single_category_can_be_silenced_without_touching_mixed_or_unknown(tmp_path):
    policy_path = tmp_path / 'category-policy.json'
    phone = {'title': 'iPhone 15 Pro 全新', 'body_excerpt': '明盘 5000'}
    mixed = {'title': 'iPad Air 和 MacBook 一起出', 'body_excerpt': '两个商品'}
    unknown = {'title': '闲置杂物', 'body_excerpt': '一些东西'}
    policy = {'silent_categories': ['phone_apple']}

    kept = categories.filter_normal_posts([phone, mixed, unknown], policy)

    assert phone not in kept
    assert {row['title'] for row in kept} == {mixed['title'], unknown['title']}
    assert categories.classify_post(mixed)['mixed'] is True
    assert categories.classify_post(unknown)['categories'] == [categories.UNKNOWN_KEY]


def test_clear_multi_item_body_keeps_non_silent_segment_and_formats_it():
    post = {
        'title': '两件物品一起出',
        'body_full': 'iPhone 15 Pro 5000\n机械键盘 300',
        'url': 'https://example.invalid/thread-1',
    }
    result = categories.filter_normal_posts([post], {'silent_categories': ['phone_apple']})
    assert len(result) == 1
    assert result[0]['category_segments'][0]['categories'] == ['computer_peripherals']


def test_policy_is_versioned_and_idempotent(tmp_path):
    path = tmp_path / 'category-policy.json'
    first = categories.set_category_silence('生活·服饰鞋包', True, path=path)
    second = categories.set_category_silence('life_clothing', True, path=path)
    restored = categories.set_category_silence('生活·服饰鞋包', False, path=path)

    assert first['version'] == categories.CATEGORY_VERSION
    assert second['silent_categories'] == ['life_clothing']
    assert restored['silent_categories'] == []
    assert json.loads(path.read_text())['updated_by'] == 'local'


def test_concurrent_style_updates_preserve_existing_category(tmp_path):
    path = tmp_path / 'category-policy.json'
    categories.set_category_silence('手机·苹果', True, path=path)
    categories.set_category_silence('电脑·CPU', True, path=path)
    assert categories.load_policy(path)['silent_categories'] == ['computer_cpu', 'phone_apple']


def test_history_reader_is_read_only_and_counts_posts(tmp_path):
    queue = tmp_path / 'delivery.sqlite3'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE delivery_queue (dedupe_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER)')
        db.execute('INSERT INTO delivery_queue VALUES (?,?,?,?)', (
            'one', '## Chiphell 新帖\n1. [iPhone 15](https://example.invalid/t1) 摘要：全新', 1, None))
    before = queue.stat().st_mtime_ns
    posts = categories.history_posts(queue)
    counts = categories.category_counts(posts)
    assert len(posts) == 1
    assert counts['phone_apple'] == 1
    assert queue.stat().st_mtime_ns == before


def test_cli_category_report_does_not_need_telegram_profile(tmp_path):
    queue = tmp_path / 'delivery.sqlite3'
    with sqlite3.connect(queue) as db:
        db.execute('CREATE TABLE delivery_queue (dedupe_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER)')
        db.execute('INSERT INTO delivery_queue VALUES (?,?,?,?)', (
            'one', '## Chiphell 新帖\n1. [iPhone 15](https://example.invalid/t1) 摘要：全新', 1, None))
    text, markup = categories.category_report('cli', queue_path=queue)
    assert '手机·苹果' in text
    assert markup == {}


def test_callback_signature_and_owner_boundary(monkeypatch, tmp_path):
    monkeypatch.setattr(categories, 'profile_env', lambda _: {
        'TELEGRAM_BOT_TOKEN': 'fixture-token', 'TELEGRAM_HOME_CHANNEL': '123'})
    monkeypatch.setattr(categories, 'POLICY_PATH', tmp_path / 'policy.json')
    data = categories.callback_data('default', 'mute', 'phone_apple')
    good = {'data': data, 'from': {'id': 123}, 'message': {'chat': {'id': 123, 'type': 'private'}}}
    bad = {'data': data, 'from': {'id': 456}, 'message': {'chat': {'id': 123, 'type': 'private'}}}

    assert '已静默' in categories.callback('default', good)['text']
    assert categories.load_policy()['silent_categories'] == ['phone_apple']
    assert '无权' in categories.callback('default', bad)['text']
