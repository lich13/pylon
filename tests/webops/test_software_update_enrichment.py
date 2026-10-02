import json
from pathlib import Path
from unittest import mock

import httpx

from pylon.tasks.software import monitor


def configure(tmp_path, monkeypatch):
    root = tmp_path / 'webops'
    monkeypatch.setattr(monitor, 'PROFILE_ROOT', root)
    monkeypatch.setattr(monitor, 'STATE_FILE', root / 'cron_state/software_update_monitor.json')
    monkeypatch.setattr(monitor, 'DATA_ROOT', root / 'data/software_update_monitor')
    monkeypatch.setattr(monitor, 'TRANSLATION_CACHE_FILE', root / 'data/software_update_monitor/translation_cache.json')
    monkeypatch.setattr(monitor, 'ENRICHMENT_CACHE_FILE', root / 'data/software_update_monitor/enrichment_cache.json')
    monkeypatch.setattr(monitor, 'ITEMS_DIR', root / 'data/software_update_monitor/items')
    monkeypatch.setattr(monitor, 'CUSTOM_SOURCES_FILE', root / 'config/software_update_monitor_sources.json')
    monkeypatch.setattr(monitor, 'TRANSLATION_CONFIG_FILE', root / 'config/software_update_translation.json')
    return root


def event(body='Fixed a connection issue and added ARM64 support.'):
    return {
        'source': {'id': 'claude_code', 'name': 'Claude Code'},
        'item': {
            'id': 'release:1.2.3',
            'title': 'Claude Code 1.2.3',
            'url': 'https://example.com/releases/1.2.3',
            'body': body,
        },
    }


def digest(importance='high'):
    return {
        'title_zh': 'Claude Code 1.2.3',
        'importance': importance,
        'summary_zh': ['修复连接问题。'] if importance == 'low' else ['修复连接问题。', '增加 ARM64 支持。'],
        'impact_zh': '涉及 Linux ARM64 兼容性。',
        'action_zh': '可安排更新。',
        'change_types': ['bugfix', 'compatibility'],
        'evidence': ['Fixed a connection issue', 'added ARM64 support'],
        'confidence': 0.92,
    }


def test_validate_digest_enforces_low_value_one_line():
    assert monitor.validate_grok_digest(digest('high'))['importance'] == 'high'
    try:
        monitor.validate_grok_digest(digest('low') | {'summary_zh': ['一', '二']})
    except monitor.GrokDigestError as exc:
        assert 'one summary' in str(exc)
    else:
        raise AssertionError('low digest with two summaries was accepted')
    try:
        monitor.validate_grok_digest(digest('normal') | {'summary_zh': ['一', '二', '三']})
    except monitor.GrokDigestError as exc:
        assert 'at most two' in str(exc)
    else:
        raise AssertionError('normal digest with three summaries was accepted')


def test_grok_prompt_is_bounded_and_marks_priority_source():
    source = {'id': 'github_anthropics_claude_code_releases', 'name': 'Claude Code'}
    item = event('x' * 10000)['item']
    prompt = monitor.build_grok_digest_prompt(source, item)
    assert len(prompt) <= monitor.GROK_DIGEST_INPUT_CHARS
    assert '重点监控软件' in prompt


def test_grok_digest_is_cached_by_item_and_prompt_version(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    with mock.patch.object(monitor, 'request_grok_digest', return_value=(digest(), 'req-1')) as request:
        state = monitor.default_state()
        first, provider, request_id, error = monitor.enrich_event(event(), state=state)
        second, provider2, request_id2, error2 = monitor.enrich_event(event(), state=state)

    assert first == second == digest()
    assert provider == 'grok' and request_id == 'req-1'
    assert provider2 == 'cache' and request_id2 is None
    assert error is None and error2 is None
    request.assert_called_once()
    assert json.loads(monitor.ENRICHMENT_CACHE_FILE.read_text())


def test_public_fallback_preserves_urls_and_does_not_fail_task(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monitor.TRANSLATION_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    monitor.TRANSLATION_CONFIG_FILE.write_text(json.dumps({'endpoints': [{'url': 'https://translate.example/translate'}]}))
    response = httpx.Response(200, json={'translatedText': '访问 __ph0__，版本 __ph1__'})
    with mock.patch.object(monitor, 'request_grok_digest', side_effect=monitor.GrokDigestError('HTTP 429')), \
         mock.patch.object(monitor.httpx, 'post', return_value=response):
        result, provider, request_id, error = monitor.enrich_event(
            event('Visit https://example.com/help in version 1.2.3.'),
            state=monitor.default_state(),
        )

    assert provider == 'libretranslate'
    assert error and '429' in error
    assert 'https://example.com/help' in result['summary_zh'][0]
    assert '1.2.3' in result['summary_zh'][0]


def test_public_fallback_tries_second_endpoint_after_first_backoff(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monitor.TRANSLATION_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    monitor.TRANSLATION_CONFIG_FILE.write_text(json.dumps({'endpoints': [
        {'url': 'https://first.example/translate'},
        {'url': 'https://second.example/translate'},
    ]}))
    responses = [httpx.Response(429), httpx.Response(200, json={'translatedText': '你好'})]
    with mock.patch.object(monitor.httpx, 'post', side_effect=responses) as post:
        client = monitor.LibreTranslateClient(state={})
        assert client.translate('Hello') == '你好'
    assert post.call_count == 2


def test_process_pending_enrichment_publishes_idempotent_message(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    state = monitor.default_state()
    queued = event()
    queued['enrichment_status'] = 'pending'
    state['pending_events'] = [queued]
    monitor.save_json(monitor.STATE_FILE, state)
    with mock.patch.object(monitor, 'request_grok_digest', return_value=(digest(), 'req-2')), \
         mock.patch.object(monitor.notification_events, 'publish', return_value='event-1') as publish:
        summary = monitor.process_pending_enrichment(max_items=4)

    assert summary == {'processed': 1, 'sent': 1, 'pending': 0, 'errors': []}
    publish.assert_called_once()
    assert '高价值' in publish.call_args.args[0]
    saved = monitor.load_state()
    assert saved['pending_events'] == []


def test_pi_news_url_is_canonicalized_and_hermes_is_removed():
    source = monitor.normalize_source({
        'id': 'pi_releases', 'name': 'Pi Releases', 'url': 'https://pi.dev/news.xml', 'kind': 'atom',
    })
    assert source['url'] == 'https://pi.dev/changelog.xml'
    assert 'hermes_agent_releases' not in {value['id'] for value in monitor.SOURCES}


def test_legacy_monitor_state_is_upgraded_to_version_two(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monitor.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    monitor.STATE_FILE.write_text(json.dumps({'version': 1, 'sources': {}}))
    state = monitor.load_state()
    assert state['version'] == 2
