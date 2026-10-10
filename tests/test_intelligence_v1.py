"""Synthetic v1 contracts for source-aware intelligence storage and policy."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from pylon.outbox import Outbox
import pytest

from pylon.tasks.intelligence import health, migrate, pipeline, policy, routing
from pylon.tasks.intelligence.store import Store
from pylon.tasks.forums import v2ex
from pylon.tasks.linuxdo import http as linuxdo_http
from pylon.tasks.linuxdo.helper import SOURCE as HELPER_SOURCE
from pylon.tasks.linuxdo.sources import Observation, Page
from pylon.tasks.linuxdo.store import Store as LinuxStore
from pylon.tasks.xianbao import poll as xianbao_poll
from pylon.tasks.xianbao import worker as xianbao_worker


def rows(box, query, parameters=()):
    with box.connect() as db:
        return [dict(row) for row in db.execute(query, parameters)]


def fixture_config(category="B1", source="linuxdo"):
    return {
        "version": 1,
        "enabled": True,
        "categories": [category],
        "blocked_groups": [],
        "category_sources": {
            key: ([source] if key == category else []) for key in policy.CATALOG
        },
        "exclude_terms": {},
        "include_terms": {},
        "linuxdo_focus": {
            "version": 1,
            "groups": [],
            "categories": [],
            "excluded_categories": [],
            "cloud_providers": [],
            "cloud_regions": "all",
        },
        "value_mode": "evidenced_lead",
    }


def test_intelligence_store_deduplicates_identical_facts_and_claims_only_live_items(
    tmp_path,
):
    box = Outbox(tmp_path / "intelligence.sqlite3")
    store = Store(box)
    value = {
        "source": "linuxdo",
        "source_id": "fixture-topic-1",
        "title": "合成摘要：Codex 试用",
        "body": "Codex 试用期 30 天，限制条件未说明。",
        "url": "https://example.invalid/intel/fixture-topic-1",
        "published_at": "2026-10-10T00:00:00Z",
        "evidence_scope": "excerpt",
        "context": {"evidence_scope": "helper_excerpt", "kind": "summary"},
    }

    assert store.ingest(**value) is True
    assert store.ingest(**value) is False
    assert store.ingest(
        **(value | {"source_id": "fixture-history-1", "historical": True})
    ) is True

    claimed = store.claim("linuxdo", limit=2)

    assert [item["source_id"] for item in claimed] == ["fixture-topic-1"]
    assert claimed[0]["evidence_scope"] == "excerpt"
    assert json.loads(claimed[0]["context"])["evidence_scope"] == "helper_excerpt"
    assert store.claim("linuxdo", limit=2) == []


def test_short_intelligence_failures_do_not_notify_before_the_sixty_minute_episode(
    tmp_path,
):
    box = Outbox(tmp_path / "health.sqlite3")

    health.record(box, "helper-summary", backlog=2, error="synthetic temporary failure", now=1000)
    health.record(box, "helper-summary", backlog=2, error="synthetic temporary failure", now=4599)

    assert rows(box, "SELECT * FROM events") == []


def test_persistent_backlog_opens_one_alert_and_restarts_do_not_duplicate_it(tmp_path):
    path = tmp_path / "health.sqlite3"
    box = Outbox(path)

    health.record(box, "helper-summary", backlog=3, error="synthetic blocked", now=1000)
    health.record(box, "helper-summary", backlog=3, error="synthetic blocked", now=4600)
    restarted = Outbox(path)
    health.record(restarted, "helper-summary", backlog=3, error="synthetic blocked", now=8200)

    events = rows(box, "SELECT * FROM events ORDER BY created_at")
    assert len(events) == 1
    assert events[0]["job_id"] == "intelligence-review"
    assert "helper-summary" in json.loads(events[0]["payload"])["text"]
    assert "60" in json.loads(events[0]["payload"])["text"]
    assert rows(box, "SELECT open,backlog FROM intel_health") == [
        {"open": 1, "backlog": 3}
    ]


def test_backlog_without_a_current_error_still_opens_one_episode_alert(tmp_path):
    box = Outbox(tmp_path / "health-no-error.sqlite3")

    health.record(box, "delivery", backlog=1, now=1000)
    health.record(box, "delivery", backlog=1, now=4600)
    health.record(box, "delivery", backlog=1, now=8200)

    alerts = rows(box, "SELECT * FROM events WHERE job_id='intelligence-review'")
    assert len(alerts) == 1


def test_progress_or_empty_backlog_closes_the_episode_without_a_recovery_notice(tmp_path):
    box = Outbox(tmp_path / "health.sqlite3")

    health.record(box, "helper-summary", backlog=2, error="synthetic blocked", now=1000)
    health.record(box, "helper-summary", backlog=2, error="synthetic blocked", now=4600)
    assert len(rows(box, "SELECT * FROM events")) == 1

    health.record(box, "helper-summary", backlog=2, progress=True, now=4700)
    health.record(box, "helper-summary", backlog=0, now=4800)

    assert len(rows(box, "SELECT * FROM events")) == 1
    assert rows(box, "SELECT blocked_since,open,backlog FROM intel_health") == [
        {"blocked_since": None, "open": 0, "backlog": 0}
    ]


def test_intelligence_policy_pushes_evidenced_value_and_keeps_unknown_note():
    item = {
        "source": "linuxdo",
        "title": "合成福利摘要",
        "body": "Codex 试用信息",
    }
    result = {
        "decision": "push",
        "categories": ["B1"],
        "content_type": "tutorial",
        "facts": [
            {
                "category": "B1",
                "subject": "Codex 试用",
                "value": "30 天，限制条件未知",
            }
        ],
        "reason": "有明确期限；额度与限制条件未知",
    }

    assert policy.decide(item, result, fixture_config()) == (
        "push",
        "有明确期限；额度与限制条件未知",
    )


def test_intelligence_policy_filters_pure_question_or_chitchat_even_if_model_says_push():
    item = {
        "source": "linuxdo",
        "title": "有人知道 Codex 怎么用吗？",
        "body": "请问这个功能在哪里开，纯提问交流。",
    }
    result = {
        "decision": "push",
        "categories": ["B1"],
        "content_type": "question",
        "facts": [
            {
                "category": "B1",
                "subject": "有人知道 Codex 怎么用吗？",
                "value": "请问这个功能在哪里开",
            }
        ],
        "reason": "模型判定有价值",
    }

    decision, reason = policy.decide(item, result, fixture_config())

    assert decision == "skip"
    assert reason


def test_policy_re_evaluates_the_raw_judgment_after_an_exclusion_is_relaxed():
    item = {
        "source": "linuxdo",
        "title": "合成教程摘要",
        "body": "可复现步骤",
    }
    result = {
        "decision": "push",
        "content_type": "tutorial",
        "categories": ["B1"],
        "facts": [{"category": "B1", "subject": "合成方法", "value": "可复现步骤"}],
        "reason": "原始判断保留",
    }
    excluded = fixture_config()
    excluded["exclude_terms"] = {"B1": ["可复现"]}
    assert policy.decide(item, result, excluded) == ("skip", "命中排除条件")

    relaxed = fixture_config()
    assert policy.decide(item, result, relaxed) == ("push", "原始判断保留")
    assert result["decision"] == "push"


def test_trade_intel_segments_are_contiguous_same_evidence_and_leave_public_residual():
    snapshot = {
        "title": "合成混合主题",
        "evidence": [
            {
                "ref": "tg:linux_do_helper:fixture-1",
                "body": "教程步骤：先创建目录。价格 20 元，付款后发送。",
            }
        ],
    }
    extracted = {
        "intent": "offer",
        "offers": [
            {
                "price": {"ref": snapshot["evidence"][0]["ref"], "quote": "价格 20 元"},
                "term": {"ref": snapshot["evidence"][0]["ref"], "quote": "付款后发送"},
            }
        ],
        "intel_segments": [
            {"ref": snapshot["evidence"][0]["ref"], "quote": "教程步骤：先创建目录。"},
            {"ref": snapshot["evidence"][0]["ref"], "quote": "价格 20 元"},
            {"ref": snapshot["evidence"][0]["ref"], "quote": "教目录"},
        ],
    }

    residual = routing.residual(snapshot, extracted)

    assert [row["body"] for row in residual["evidence"]] == ["教程步骤：先创建目录。"]
    assert all("20 元" not in row["body"] and "付款后" not in row["body"] for row in residual["evidence"])


def test_linuxdo_helper_evidence_never_fetches_original_or_external_links():
    assert linuxdo_http.topic_evidence("fixture-topic", [1], getter=lambda *_args, **_kwargs: pytest.fail("no fetch")) == []
    assert linuxdo_http.official_evidence(
        {"evidence": [{"url": "https://example.invalid/resource"}]},
        getter=lambda *_args, **_kwargs: pytest.fail("no fetch"),
    ) == []
    with pytest.raises(linuxdo_http.FetchError):
        linuxdo_http.fetch("https://linux.do/t/topic/fixture-topic")


def test_intelligence_store_migration_is_idempotent_and_unchanged_summary_is_not_retried(tmp_path):
    path = tmp_path / "migration.sqlite3"
    first = Store(Outbox(path))
    with first.box.connect() as db:
        version, _ = pipeline.active(db)
        versions = db.execute("SELECT count(*) FROM intel_versions").fetchone()[0]
    second = Store(Outbox(path))
    with second.box.connect() as db:
        same_version, _ = pipeline.active(db)
        same_count = db.execute("SELECT count(*) FROM intel_versions").fetchone()[0]

    assert (version, versions) == (same_version, same_count) == (1, 1)
    value = {
        "source": "linuxdo",
        "source_id": "fixture-unchanged",
        "title": "合成摘要",
        "body": "原文摘要",
        "url": "https://example.invalid/intel/fixture-unchanged",
        "evidence_scope": "excerpt",
    }
    assert second.ingest(**value) is True
    assert second.ingest(**value) is False


def test_legacy_receipt_migration_keeps_one_outbox_event(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sqlite3"
    monkeypatch.setenv("PYLON_OUTBOX", str(path))

    first = pipeline.preserve_legacy_notice("fixture-receipt", "合成旧回执")
    second = pipeline.preserve_legacy_notice("fixture-receipt", "合成旧回执")

    assert first == second
    assert rows(Outbox(path), "SELECT id,payload FROM events") == [
        {"id": first, "payload": json.dumps({"text": "合成旧回执"}, ensure_ascii=False)}
    ]


def test_notification_delivery_episode_alerts_once_after_sixty_minutes_without_progress(
    tmp_path, monkeypatch
):
    box = Outbox(tmp_path / "delivery-health.sqlite3")
    box.enqueue(
        profile="webops",
        job_id="intelligence-review",
        kind="text",
        payload={"text": "合成待投递"},
        key="fixture-delivery",
    )
    clock = [1000]
    monkeypatch.setattr(health.time, "time", lambda: clock[0])

    pipeline.review(Store(box), limit=1)
    clock[0] = 4600
    pipeline.review(Store(box), limit=1)
    clock[0] = 8200
    pipeline.review(Store(box), limit=1)

    alerts = rows(
        box,
        "SELECT * FROM events WHERE job_id='intelligence-review' AND payload LIKE '%持续受阻超过 60 分钟%'",
    )
    assert len(alerts) == 1


def test_intelligence_cutover_is_idempotent_and_preserves_legacy_delivery_identity(
    tmp_path,
):
    path = tmp_path / "cutover.sqlite3"
    box = Outbox(path)
    legacy = LinuxStore(box)
    now = 1_800_000_000
    recent_at = datetime.fromtimestamp(now - 120, timezone.utc).isoformat()
    old_at = datetime.fromtimestamp(now - 2 * 86400, timezone.utc).isoformat()

    def observation(topic_id: str, message_id: int, published_at: str) -> Observation:
        return Observation(
            HELPER_SOURCE,
            message_id,
            topic_id,
            1,
            f"合成迁移主题 {topic_id}",
            "合成摘要正文与连续证据。",
            [],
            published_at,
            f"https://example.invalid/linuxdo/{topic_id}",
            [],
        )

    legacy.save_page(
        HELPER_SOURCE,
        Page([observation("recent", 1, recent_at)]),
        {"cursor": 1},
    )
    legacy.save_page(
        HELPER_SOURCE,
        Page([observation("historical", 2, old_at)]),
        {"cursor": 2},
        baseline=True,
    )
    protected_ids = {}
    for offset, event_status in enumerate(("sending", "uncertain", "delivered"), start=3):
        topic_id = f"protected-{event_status}"
        legacy.save_page(
            HELPER_SOURCE,
            Page([observation(topic_id, offset, recent_at)]),
            {"cursor": offset},
        )
        event_id = box.enqueue(
            profile="webops",
            job_id="linuxdo-notify",
            kind="text",
            payload={"text": f"合成旧回执 {event_status}"},
            key=f"fixture-{event_status}",
        )
        with box.connect() as db:
            db.execute("UPDATE events SET status=? WHERE id=?", (event_status, event_id))
            db.execute(
                "INSERT INTO linuxdo_notifications VALUES(?,?,?,?,?)",
                (topic_id, f"fixture-{event_status}", "[]", event_id, now),
            )
        protected_ids[event_status] = event_id

    first = migrate.migrate(box, now=now)
    second = migrate.migrate(box, now=now + 60)

    assert first["recent"] == 1
    assert first["historical"] == 4
    assert first["protected"] == 3
    assert second == {"already_applied": True}
    claimed = Store(box).claim("linuxdo", limit=10)
    assert [row["source_id"] for row in claimed] == ["recent"]
    assert rows(box, "SELECT source_id,historical,status FROM intel_items ORDER BY source_id") == [
        {"source_id": "historical", "historical": 1, "status": "historical"},
        {"source_id": "protected-delivered", "historical": 1, "status": "historical"},
        {"source_id": "protected-sending", "historical": 1, "status": "historical"},
        {"source_id": "protected-uncertain", "historical": 1, "status": "historical"},
        {"source_id": "recent", "historical": 0, "status": "pending"},
    ]
    assert rows(
        box,
        "SELECT id,status FROM events WHERE id IN (?,?,?) ORDER BY id",
        tuple(protected_ids.values()),
    ) == sorted(
        ({"id": event_id, "status": status} for status, event_id in protected_ids.items()),
        key=lambda row: row["id"],
    )


def test_xianbao_poll_wrapper_only_records_health_for_a_nonzero_worker_exit(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_PROFILE_ROOT", str(tmp_path / "profile"))
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "poll.sqlite3"))
    monkeypatch.setattr(xianbao_poll, "read_env", lambda _path: {})
    monkeypatch.setattr(
        xianbao_poll.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout="fallback used"),
    )

    assert xianbao_poll.main() == 0
    assert rows(Outbox(tmp_path / "poll.sqlite3"),
                "SELECT name FROM sqlite_master WHERE type='table' AND name='intel_health'") == []


def test_v2ex_success_clears_collection_episode_and_dry_run_is_read_only(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "v2ex.sqlite3"))
    monkeypatch.setattr(v2ex, "load_state", lambda: {"last_seen_id": 1})
    monkeypatch.setattr(v2ex, "collect_recent_pages", lambda *_args, **_kwargs: ([], {"fetch_error": "", "hit_last_seen": True}))
    monkeypatch.setattr(v2ex, "save_state", lambda _state: pytest.fail("empty success should not need a state write"))

    assert v2ex.run_once() == 0
    assert rows(Outbox(tmp_path / "v2ex.sqlite3"), "SELECT * FROM intel_health") == [
        {"component": "v2ex:collection", "blocked_since": None, "last_progress": pytest.approx(v2ex.time.time(), abs=5), "open": 0, "error": "", "backlog": 0}
    ]

    dry_path = tmp_path / "v2ex-dry.sqlite3"
    monkeypatch.setenv("PYLON_OUTBOX", str(dry_path))
    assert v2ex.run_once(dry_run=True) == 0
    assert rows(Outbox(dry_path),
                "SELECT name FROM sqlite_master WHERE type='table' AND name='intel_health'") == []


def test_xianbao_source_fallback_opens_and_recovers_its_health_episode(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "fallback.sqlite3"))
    data_dir = tmp_path / "xianbao"
    data_dir.mkdir()
    xianbao_worker.update_source_unavailable_state(
        data_dir,
        {"poll_json_fallback": True, "poll_json_fallback_url": "https://example.invalid/feed"},
    )
    assert rows(Outbox(tmp_path / "fallback.sqlite3"), "SELECT backlog,open FROM intel_health") == [
        {"backlog": 1, "open": 0}
    ]
    xianbao_worker.update_source_unavailable_state(data_dir, {})
    assert rows(Outbox(tmp_path / "fallback.sqlite3"), "SELECT backlog,open FROM intel_health") == [
        {"backlog": 0, "open": 0}
    ]
