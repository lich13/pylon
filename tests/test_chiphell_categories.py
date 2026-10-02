from __future__ import annotations

import sqlite3
import json

import pytest

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

    policy = categories.load_policy(path)

    assert policy['silent_categories'] == ['computer_cpu']
    assert policy['source'].endswith('SILENT_CATEGORIES')
    with pytest.raises(RuntimeError, match='源码维护'):
        categories.set_category_silence('生活·服饰鞋包', True, path=path)


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
    text, markup = categories.category_report('cli', queue_path=queue)
    assert '手机·苹果' in text
    assert markup == {}


def test_empty_category_report_has_no_empty_keyboard(tmp_path):
    text, markup = categories.category_report('default', queue_path=tmp_path / 'missing.sqlite3')
    assert '已解析商品帖：0 条' in text
    assert markup == {}


def test_unknown_prompt_is_once_only_and_records_callback(monkeypatch, tmp_path):
    monkeypatch.setattr(categories, 'profile_env', lambda _: {
        'TELEGRAM_BOT_TOKEN': 'fixture-token', 'TELEGRAM_HOME_CHANNEL': '123'})
    path = tmp_path / 'unknown.json'
    post = {'id': 't1', 'title': '新奇设备 Alpha 2', 'body_excerpt': '明盘 100', 'url': 'https://example.invalid/t1'}
    first = categories.unknown_prompts([post], profile='default', path=path)
    second = categories.unknown_prompts([post], profile='default', path=path)

    assert len(first) == 1
    assert second == []
    fingerprint = first[0]['fingerprint']
    data = categories.unknown_callback_data('default', 'mute', fingerprint)
    good = {'data': data, 'from': {'id': 123}, 'message': {'chat': {'id': 123, 'type': 'private'}}}
    bad = {'data': data, 'from': {'id': 456}, 'message': {'chat': {'id': 123, 'type': 'private'}}}
    monkeypatch.setattr(categories, 'UNKNOWN_REVIEW_PATH', path)

    assert '已记录' in categories.callback('default', good)['text']
    assert categories.load_unknown_reviews(path)['reviews'][fingerprint]['status'] == 'pending_mute'
    assert '无权' in categories.callback('default', bad)['text']


def test_retired_category_callback_cannot_mutate_policy(monkeypatch):
    monkeypatch.setattr(categories, 'profile_env', lambda _: {
        'TELEGRAM_BOT_TOKEN': 'fixture-token', 'TELEGRAM_HOME_CHANNEL': '123'})
    payload = {'data': 'ch1|mute|phone_apple|anything', 'from': {'id': 123},
               'message': {'chat': {'id': 123, 'type': 'private'}}}
    assert '入口已移除' in categories.callback('default', payload)['text']
