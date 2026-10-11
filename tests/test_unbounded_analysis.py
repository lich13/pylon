from __future__ import annotations

import json
import threading

from unittest import mock

from pylon.tasks.software import monitor


def fixture_event(item_id="release:1.2.3"):
    return {
        "source": {"id": "fixture-source", "name": "Fixture Tool"},
        "item": {
            "id": item_id,
            "title": "Fixture Tool 1.2.3",
            "url": "https://example.invalid/releases/1.2.3",
            "body": "Fixed a connection issue and added ARM64 support.",
        },
    }


def fixture_digest():
    return {
        "title_zh": "Fixture Tool 1.2.3",
        "importance": "high",
        "summary_zh": ["修复连接问题。", "增加 ARM64 支持。"],
        "impact_zh": "涉及 Linux ARM64 兼容性。",
        "action_zh": "可安排更新。",
        "change_types": ["bugfix", "compatibility"],
        "evidence": ["Fixed a connection issue", "added ARM64 support"],
        "confidence": 0.92,
    }


def configure(tmp_path, monkeypatch):
    root = tmp_path / "webops"
    monkeypatch.setattr(monitor, "PROFILE_ROOT", root)
    monkeypatch.setattr(monitor, "STATE_FILE", root / "cron_state/software_update_monitor.json")
    monkeypatch.setattr(monitor, "LOCK_PATH", root / "cron_state/software_update_monitor.lock")
    monkeypatch.setattr(monitor, "DATA_ROOT", root / "data/software_update_monitor")
    monkeypatch.setattr(monitor, "ENRICHMENT_CACHE_FILE", root / "data/software_update_monitor/enrichment_cache.json")
    monkeypatch.setattr(monitor, "TRANSLATION_CACHE_FILE", root / "data/software_update_monitor/translation_cache.json")
    monkeypatch.setattr(monitor, "ITEMS_DIR", root / "data/software_update_monitor/items")
    monkeypatch.setattr(monitor, "CUSTOM_SOURCES_FILE", root / "config/software_update_monitor_sources.json")
    monkeypatch.setattr(monitor, "TRANSLATION_CONFIG_FILE", root / "config/software_update_translation.json")
    return root


def test_grok_structured_request_has_no_http_or_total_deadline(monkeypatch):
    captured = {}
    clock = [0.0]
    monkeypatch.setattr(monitor.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(monitor, "read_env", lambda _path: {
        "GROK_MODELS_BASE_URL": "https://example.invalid/v1",
        "XAI_API_KEY": "fixture-not-a-credential",
    })

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            captured["response_closed"] = True

        def iter_lines(self):
            # Passing the historical 1140-second cutoff must not abort a live
            # stream; the caller owns the wait until the provider completes.
            clock[0] = 1141.0
            yield "data: " + json.dumps({
                "type": "response.completed", "response": {"ok": True},
            })
            yield ""

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, *_args, **kwargs):
            captured["payload"] = kwargs["json"]
            return Response()

    monkeypatch.setattr(monitor.httpx, "Client", Client)
    response, request_id = monitor._request_grok_structured(
        "fixture", schema={"type": "object"}, name="fixture",
    )

    assert response == {"ok": True}
    assert request_id
    assert captured["client"] == {"timeout": None, "trust_env": False}
    assert captured["payload"]["model"] == "grok-4.7"
    assert captured["payload"]["reasoning"] == {"effort": "medium"}
    assert captured["payload"]["tools"] == []
    assert captured["payload"]["store"] is False
    assert captured["response_closed"] is True


def test_software_enrichment_allows_only_three_explicit_grok_attempts(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(monitor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(monitor, "_translate_for_fallback", lambda _client, text: text)
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        raise monitor.GrokDigestError("invalid response")

    event = fixture_event()
    with mock.patch.object(monitor, "request_grok_digest", side_effect=fail):
        result, provider, _request_id, error = monitor.enrich_event(
            event, state=monitor.default_state(),
        )

    assert len(calls) == 3
    assert len(event["enrichment_requests"]) == 3
    assert all(row["status"] == "failed" for row in event["enrichment_requests"])
    assert provider == "libretranslate"
    assert error and "GrokDigestError" in error
    assert result["title_zh"]


def test_software_enrichment_semantic_errors_stop_after_two_attempts(tmp_path, monkeypatch):
    configure(tmp_path, monkeypatch)
    monkeypatch.setattr(monitor.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(monitor, "_translate_for_fallback", lambda _client, text: text)
    calls = []

    def fail(*_args, **_kwargs):
        calls.append(True)
        error = monitor.GrokDigestError("invalid schema")
        error.semantic = True
        raise error

    event = fixture_event("release:semantic")
    with mock.patch.object(monitor, "request_grok_digest", side_effect=fail):
        monitor.enrich_event(event, state=monitor.default_state())

    assert len(calls) == 2
    assert len([row for row in event["enrichment_requests"] if row.get("semantic")]) == 2


def test_long_enrichment_does_not_reclaim_live_runner_or_drop_new_item(tmp_path, monkeypatch):
    root = configure(tmp_path, monkeypatch)
    first = fixture_event()
    newer = fixture_event("release:1.2.4")
    state = monitor.default_state()
    state["pending_events"] = [first]
    monitor.save_json(monitor.STATE_FILE, state)

    started = threading.Event()
    release = threading.Event()

    def slow_request(*_args, **_kwargs):
        started.set()
        assert release.wait(5)
        latest = monitor.load_state()
        latest.setdefault("pending_events", []).append(newer)
        monitor.save_json(monitor.STATE_FILE, latest)
        return fixture_digest(), "fixture-request"

    monkeypatch.setattr(monitor, "request_grok_digest", slow_request)
    monkeypatch.setattr(monitor, "_translate_for_fallback", lambda _client, text: text)
    results = []
    with mock.patch.object(monitor.notification_events, "publish", return_value="fixture-event"):
        worker = threading.Thread(target=lambda: results.append(monitor.process_pending_enrichment()))
        worker.start()
        assert started.wait(5)

        # A second runner must wait for the owner lock, regardless of how old
        # the model row's heartbeat appears to be.
        second = monitor.process_pending_enrichment()
        assert second["processed"] == 0
        assert second["sent"] == 0
        assert second["pending"] == 1

        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert results[0]["sent"] == 1
    saved = monitor.load_state()
    assert [item["item"]["id"] for item in saved["pending_events"]] == ["release:1.2.4"]
    assert root.exists()

