from __future__ import annotations

import copy
import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from unittest.mock import Mock

import httpx
import pytest

from pylon import transport
from pylon.events import text_key
from pylon.outbox import Outbox
from pylon.tasks.intelligence import pipeline as intelligence_pipeline
from pylon.tasks.intelligence import policy as intelligence_policy
from pylon.tasks.intelligence.store import Store as IntelligenceStore
from pylon.tasks.linuxdo import helper_collect, http, judge, pipeline, policy, sources, trade
from pylon.tasks.linuxdo.common import JOB_ID, digest
from pylon.tasks.linuxdo.helper import SOURCE as HELPER_SOURCE
from pylon.tasks.linuxdo.helper_store import HelperStore
from pylon.tasks.linuxdo.store import Store


CHANNELS = ("LinuxDoNew", "linuxdo_tg", "linux_do_channel")
PUBLISHED_AT = "2026-01-02T03:04:05+00:00"
EXPECTED_ALLOWED = {
    "A1", "A2", "A3", "A4",
    "B1", "B2", "B3", "B4", "B5",
    "C1", "C2", "C3", "C4",
    "D1", "D2", "D3", "D4", "D5",
    "E2", "E3", "G1", "G2", "H2",
}
EXPECTED_EXCLUDED = {
    "E1", "E4", "F1", "F2", "F3", "F4", "G3",
    "H1", "H3", "H4", "H5", "H6", "H7", "H8",
}


def message_html(channel: str, message_id: int, text_html: str, *, published_at=PUBLISHED_AT) -> str:
    return f"""
<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{channel}/{message_id}">
  <div class="tgme_widget_message_text js-message_text" dir="auto">{text_html}</div>
  <a class="tgme_widget_message_date" href="https://t.me/{channel}/{message_id}">
    <time datetime="{published_at}"></time>
  </a>
</div>
"""


def structured_message(channel: str, topic_id: str, *, title="合成工具部署指南", post_number: int = 1) -> str:
    topic_url = f"https://linux.do/t/topic/{topic_id}"
    if post_number != 1:
        topic_url += f"/{post_number}"
    body = '提供 <b>可复现步骤</b>，入口：<a href="https://example.invalid/resource">资源页</a>。'
    if channel == "LinuxDoNew":
        return f"""
标题：{title}<br>
作者：#fixture_author<br>
板块：#开发调优 #自动化<br>
编号：{topic_id}<br>
帖子：<a href="{topic_url}">原帖</a><br>
时间：2026-01-02 11:00:00<br>
摘要：{body}
"""
    if channel == "linuxdo_tg":
        return f"""
主 题 ：{title}<br>
发 布 者 ：#fixture_author<br>
标签分类：#开发调优，#自动化<br>
时 间 ：2026-01-02 11:00:00<br>
内容预览：{body}<br>
直达链接：<a href="{topic_url}">{topic_url}</a>
"""
    return f'合成作者 在 <a href="{topic_url}">{title}</a> 中发帖<br>{body}'


@pytest.mark.parametrize("channel", CHANNELS)
def test_each_channel_retains_message_identity_and_topic_content(channel):
    page = sources.parse_page(message_html(channel, 41, structured_message(channel, "9101")), channel)

    assert page.message_ids == [41]
    assert len(page.observations) == 1
    post = page.observations[0]
    assert post.channel == channel
    assert post.message_id == 41
    assert post.topic_id == "9101"
    assert post.post_number == 1
    assert post.title == "合成工具部署指南"
    assert "可复现步骤" in post.body
    assert "资源页" in post.body
    assert "<b>" not in post.body
    assert post.tags == ([] if channel == "linux_do_channel" else ["开发调优", "自动化"])
    assert post.published_at == PUBLISHED_AT
    assert post.source_url == f"https://t.me/{channel}/41"


@pytest.mark.parametrize("channel", CHANNELS)
def test_reply_number_belongs_to_explicit_topic_link(channel):
    page = sources.parse_page(
        message_html(channel, 42, structured_message(channel, "9102", post_number=7)),
        channel,
    )

    assert [(post.topic_id, post.post_number) for post in page.observations] == [("9102", 7)]


@pytest.mark.parametrize(
    ("channel", "text_html"),
    [
        (
            "LinuxDoNew",
            """
标题：当前资源更新<br>
作者：#fixture_author<br>
板块：#开发调优<br>
编号：9103<br>
时间：2026-01-02 11:00:00<br>
摘要：先引用 <a href="https://linux.do/t/topic/8001/3">另一主题</a>，当前资源已更新。<br>
帖子：<a href="https://linux.do/t/topic/9103/8">原帖</a>
""",
        ),
        (
            "linuxdo_tg",
            """
主 题：当前资源更新<br>
发 布 者：#fixture_author<br>
标签分类：#开发调优<br>
时 间：2026-01-02 11:00:00<br>
内容预览：先引用 <a href="https://linux.do/t/topic/8001/3">另一主题</a>，当前资源已更新。<br>
直达链接：<a href="https://linux.do/t/topic/9103/8">https://linux.do/t/topic/9103/8</a>
""",
        ),
        (
            "linux_do_channel",
            """
<blockquote>参考 <a href="https://linux.do/t/topic/8001/3">另一主题</a></blockquote><br>
合成作者 在 <a href="https://linux.do/t/topic/9103/8">当前资源更新</a> 中发帖<br>
当前资源已更新。
""",
        ),
    ],
)
def test_quoted_topic_before_permalink_never_steals_current_topic(channel, text_html):
    page = sources.parse_page(message_html(channel, 43, text_html), channel)

    assert len(page.observations) == 1
    post = page.observations[0]
    assert (post.topic_id, post.post_number) == ("9103", 8)
    assert post.title == "当前资源更新"
    assert "当前资源已更新" in post.body


@pytest.mark.parametrize("separator", [":", "："])
def test_preview_original_topic_label_cannot_override_the_footer_permalink(separator):
    markup = f"""
主 题：当前资源更新<br>
发 布 者：#fixture_author<br>
标签分类：#开发调优<br>
时 间：2026-01-02 11:00:00<br>
内容预览：引用说明中的原帖{separator}<a href="https://linux.do/t/topic/8002/3">https://linux.do/t/topic/8002/3</a>，当前资源已更新。<br>
直达链接：<a href="https://linux.do/t/topic/9108/6">https://linux.do/t/topic/9108/6</a>
"""

    page = sources.parse_page(message_html("linuxdo_tg", 51, markup), "linuxdo_tg")

    assert page.message_ids == [51]
    assert len(page.observations) == 1
    post = page.observations[0]
    assert (post.topic_id, post.post_number) == ("9108", 6)
    assert post.title == "当前资源更新"
    assert "https://linux.do/t/topic/8002/3" in post.body
    assert post.source_url == "https://t.me/linuxdo_tg/51"


def test_full_width_punctuation_and_spaced_labels_preserve_preview_and_tags():
    markup = """
主  题　：工具 &amp; 自动化<br>
发 布 者　：#fixture_author<br>
标签分类　：#开发调优，#自动化<br>
时　间　：2026-01-02 11:00:00<br>
内容预览　：第一行包含 <b>操作步骤</b>。<br>第二行保留必要限制。<br>
直达链接　：<a href="https://linux.do/t/topic/9104">https://linux.do/t/topic/9104</a>
"""
    page = sources.parse_page(message_html("linuxdo_tg", 44, markup), "linuxdo_tg")

    assert len(page.observations) == 1
    post = page.observations[0]
    assert post.title == "工具 & 自动化"
    assert post.tags == ["开发调优", "自动化"]
    assert "第一行包含 操作步骤" in post.body
    assert "第二行保留必要限制" in post.body
    assert "直达链接" not in post.body
    assert post.published_at == PUBLISHED_AT


@pytest.mark.parametrize("channel", CHANNELS)
def test_distinct_messages_for_the_same_topic_remain_distinct_observations(channel):
    markup = message_html(channel, 45, structured_message(channel, "9105"))
    markup += message_html(channel, 46, structured_message(channel, "9105", post_number=2))

    page = sources.parse_page(markup, channel)

    assert set(page.message_ids) == {45, 46}
    assert {(post.message_id, post.topic_id, post.post_number) for post in page.observations} == {
        (45, "9105", 1),
        (46, "9105", 2),
    }


def test_same_numeric_message_id_in_three_channels_has_three_source_identities():
    observations = [
        sources.parse_page(message_html(channel, 47, structured_message(channel, "9106")), channel).observations[0]
        for channel in CHANNELS
    ]

    assert {(post.channel, post.message_id) for post in observations} == {(channel, 47) for channel in CHANNELS}
    assert len({post.source_url for post in observations}) == 3


@pytest.mark.parametrize("channel", CHANNELS)
def test_unparseable_message_ids_are_retained_without_inventing_topic_observations(channel):
    markup = message_html(channel, 48, '仅有 <a href="https://example.invalid/info">频道说明</a>。')
    markup += f'<div class="tgme_widget_message" data-post="{channel}/49"><div class="tgme_widget_message_photo_wrap"></div></div>'
    markup += message_html(channel, 50, structured_message(channel, "9107"))

    page = sources.parse_page(markup, channel)

    assert set(page.message_ids) == {48, 49, 50}
    assert [(post.message_id, post.topic_id) for post in page.observations] == [(50, "9107")]


def test_default_policy_matches_the_approved_interest_boundary():
    config = policy.default_policy()

    assert config == {
        "version": 1,
        "groups": ["A", "B", "C", "D"],
        "categories": ["E2", "E3", "G1", "G2", "H2"],
        "excluded_categories": [
            "E1", "E4", "F1", "F2", "F3", "F4", "G3",
            "H1", "H3", "H4", "H5", "H6", "H7", "H8",
        ],
        "cloud_providers": ["tencent", "alibaba"],
        "cloud_regions": "all",
    }
    assert policy.allowed_categories(config) == EXPECTED_ALLOWED
    assert policy.allowed_categories(config).isdisjoint(EXPECTED_EXCLUDED)
    assert policy.CATEGORIES["B3"] == "Grok Build"


def test_validation_preserves_the_approved_default_policy_meaning():
    config = policy.default_policy()
    original = copy.deepcopy(config)

    validated = policy.validate(config)

    assert isinstance(validated, dict)
    assert policy.allowed_categories(validated) == EXPECTED_ALLOWED
    assert set(validated["cloud_providers"]) == {"tencent", "alibaba"}
    assert validated["cloud_regions"] == "all"
    assert config == original


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("groups", ["Z"]),
        ("groups", ["A", "unknown"]),
        ("categories", ["A0"]),
        ("categories", ["H9"]),
        ("excluded_categories", ["Z1"]),
        ("excluded_categories", ["E1", "unknown"]),
    ],
)
def test_unknown_group_or_category_cannot_silently_change_the_boundary(field, bad_value):
    config = policy.default_policy()
    config[field] = bad_value

    with pytest.raises(ValueError):
        policy.validate(config)
    with pytest.raises(ValueError):
        policy.allowed_categories(config)


@pytest.mark.parametrize("group", ["A", "B", "C", "D", "E", "F", "G", "H"])
def test_explicit_exclusions_override_enabled_groups_and_explicit_categories(group):
    config = policy.default_policy()
    config["groups"] = [group]
    config["categories"] = ["B3", "E1"]
    config["excluded_categories"] = sorted(EXPECTED_EXCLUDED | {"B3"})

    allowed = policy.allowed_categories(config)

    assert "B3" not in allowed
    assert allowed.isdisjoint(EXPECTED_EXCLUDED)


def test_policy_can_disable_all_interest_groups_without_restoring_defaults():
    config = policy.default_policy()
    config.update(groups=[], categories=[])

    assert policy.allowed_categories(config) == set()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "LEGACY_STATE", tmp_path / "unused-legacy-state.json")
    monkeypatch.delenv("PYLON_RUN_ID", raising=False)
    return Store(Outbox(tmp_path / "outbox.sqlite3"))


@pytest.fixture
def intelligence_store(store, monkeypatch):
    monkeypatch.setattr(judge, "model_name", lambda: "fixture-model")
    intelligence = IntelligenceStore(store.box)
    with intelligence.box.connect() as db:
        version, _ = intelligence_pipeline.active(db)
        config = {
            "version": 1,
            "enabled": True,
            "categories": ["C2"],
            "blocked_groups": [],
            "category_sources": {
                key: (["linuxdo"] if key == "C2" else [])
                for key in intelligence_policy.CATALOG
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
        db.execute(
            "UPDATE intel_versions SET content=? WHERE id=?",
            (json.dumps(config, ensure_ascii=False), version),
        )
    return intelligence


def observation(*, channel="LinuxDoNew", message_id=61, topic_id="9201", post_number=1,
                body="合成工具开放申请。", published_at=PUBLISHED_AT):
    return sources.Observation(
        channel=channel,
        message_id=message_id,
        topic_id=topic_id,
        post_number=post_number,
        title="合成工具开放申请",
        body=body,
        tags=["开发调优"],
        published_at=published_at,
        source_url=f"https://t.me/{channel}/{message_id}",
        links=["https://example.invalid/resource"],
    )


def helper_observation(*, message_id=71, topic_id="9201", post_number=1,
                       body="合成脚本可按顺序执行三个操作。详情见资源页。",
                       published_at=PUBLISHED_AT):
    return observation(
        channel=HELPER_SOURCE,
        message_id=message_id,
        topic_id=topic_id,
        post_number=post_number,
        body=body,
        published_at=published_at,
    )


def save_observation(store, post=None, *, baseline=False):
    post = post or observation()
    return store.save_page(
        post.channel,
        sources.Page(observations=[post], message_ids=[post.message_id]),
        {"cursor": post.message_id, "baseline": True, "backfill_before": None, "backfill_until": None},
        baseline=baseline,
    )


def push_decision():
    return {
        "decision": "push",
        "facts": [
            {"kind": "resource", "subject": "合成工具", "value": "https://example.invalid/resource"},
            {"kind": "availability", "subject": "申请", "value": "开放"},
        ],
        "expires_at": "",
    }


def complete_evidence(*, body="合成工具开放申请，说明包含完整操作步骤。"):
    return {
        "ref": "topic:9201:1",
        "origin": "topic",
        "post_number": 1,
        "url": "https://linux.do/t/topic/9201/1",
        "title": "合成工具开放申请",
        "body": body,
        "links": ["https://example.invalid/resource"],
        "complete": True,
    }


def prompt_data(snapshot):
    return json.loads(judge.build_prompt(snapshot, policy.default_policy()).splitlines()[-1])


def intelligence_item(store, source_id="9201"):
    with store.box.connect() as db:
        row = db.execute(
            "SELECT * FROM intel_items WHERE source='linuxdo' AND source_id=?",
            (source_id,),
        ).fetchone()
    assert row is not None
    return dict(row)


def capture_helper(legacy, intelligence, post):
    save_observation(legacy, post)
    return intelligence_pipeline.capture_linuxdo(intelligence, legacy.snapshot(post.topic_id))


def intelligence_value(*, ref="tg:linux_do_helper_bot:71", decision="push",
                       expires_at="", categories=None, facts=None,
                       body_quote="合成脚本可按顺序执行三个操作。"):
    categories = ["C2"] if categories is None and decision in {"push", "hold"} else (categories or [])
    facts = facts if facts is not None else ([{
        "category": "C2",
        "kind": "method",
        "subject": "合成脚本",
        "value": "按顺序执行三个操作",
        "evidence": [{"ref": ref, "quote": body_quote}],
    }] if decision == "push" else [])
    return {
        "decision": decision,
        "categories": categories,
        "content_type": "tutorial",
        "reason": "脚本提供可复用的顺序操作方法。" if decision == "push" else "缺少可复用步骤。",
        "action": "阅读原帖中的操作步骤。",
        "missing_evidence": [] if decision != "hold" else ["完整操作步骤"],
        "expires_at": expires_at,
        "change_kind": "initial",
        "provider": "",
        "region_scope": "unknown",
        "facts": facts,
    }


def test_three_channel_publication_times_reach_snapshot_and_prompt_by_source_ref(store, monkeypatch):
    observed_iso = "2026-10-09T04:00:00+00:00"
    observed_at = datetime.fromisoformat(observed_iso).timestamp()
    monkeypatch.setattr(judge.time, "time", lambda: observed_at)
    publication_times = dict(zip(CHANNELS, [
        "2026-01-02T03:04:05+00:00",
        "2026-02-03T12:13:14+08:00",
        "2026-03-04T05:06:07Z",
    ]))
    for channel, published_at in publication_times.items():
        page = sources.parse_page(
            message_html(channel, 71, structured_message(channel, "9201"), published_at=published_at),
            channel,
        )
        assert len(page.observations) == 1
        assert save_observation(store, page.observations[0]) == 1
    store.add_evidence("9201", [complete_evidence()])
    snapshot = Store(Outbox(store.box.path)).snapshot("9201")
    expected_times = {f"tg:{channel}:71": value for channel, value in publication_times.items()}
    expected_times["topic:9201:1"] = ""

    assert {row["ref"]: row["source_published_at"] for row in snapshot["evidence"]} == expected_times
    assert {row["observed_at"] for row in snapshot["evidence"]} == {observed_at}
    data = prompt_data(snapshot)
    assert {row["ref"]: row["source_published_at"] for row in data["evidence"]} == expected_times
    assert {row["observed_at"] for row in data["evidence"]} == {observed_iso}


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("published_at", [
    pytest.param(None, id="missing-time-element"),
    pytest.param("", id="empty"),
    pytest.param("not-a-date", id="malformed"),
    pytest.param("2026-02-30T03:04:05+00:00", id="invalid-calendar-date"),
    pytest.param("2026-01-02T03:04:05", id="missing-timezone"),
])
def test_unknown_publication_time_never_falls_back_to_collection_time(store, monkeypatch, channel, published_at):
    observed_iso = "2026-10-09T04:00:00+00:00"
    observed_at = datetime.fromisoformat(observed_iso).timestamp()
    monkeypatch.setattr(judge.time, "time", lambda: observed_at)
    markup = message_html(channel, 71, structured_message(channel, "9201"), published_at=published_at)
    if published_at is None:
        markup = markup.replace('<time datetime="None"></time>', "")
    page = sources.parse_page(markup, channel)
    assert len(page.observations) == 1
    save_observation(store, page.observations[0])

    snapshot = Store(Outbox(store.box.path)).snapshot("9201")
    assert snapshot["evidence"][0]["source_published_at"] == ""
    assert snapshot["evidence"][0]["observed_at"] == observed_at
    row = prompt_data(snapshot)["evidence"][0]
    assert row["source_published_at"] == ""
    assert row["observed_at"] == observed_iso


def test_page_save_rolls_back_observations_if_checkpoint_write_fails(store):
    save_observation(store)
    old_checkpoint = store.checkpoint("LinuxDoNew")
    with store.box.connect() as db:
        db.execute("""
            CREATE TRIGGER reject_checkpoint BEFORE UPDATE ON linuxdo_sources
            BEGIN SELECT RAISE(ABORT, 'injected checkpoint failure'); END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="injected checkpoint failure"):
        save_observation(store, observation(message_id=62, topic_id="9202"))

    reopened = Store(Outbox(store.box.path))
    assert reopened.checkpoint("LinuxDoNew") == old_checkpoint
    assert reopened.stats()["observations"] == 1
    with pytest.raises(ValueError, match="unknown"):
        reopened.snapshot("9202")
    with store.box.connect() as db:
        assert db.execute("SELECT count(*) FROM linuxdo_evidence WHERE topic_id='9202'").fetchone()[0] == 0


def test_replayed_message_keeps_one_observation_and_the_same_evidence_version(store):
    assert save_observation(store) == 1
    first = store.snapshot("9201")
    reopened = Store(Outbox(store.box.path))

    assert save_observation(reopened) == 0

    replayed = reopened.snapshot("9201")
    assert replayed["version"] == first["version"]
    assert replayed["fingerprint"] == first["fingerprint"]
    assert len(replayed["evidence"]) == 1
    assert reopened.stats()["observations"] == 1
    assert reopened.checkpoint("LinuxDoNew")["cursor"] == 61


def test_two_channels_keep_separate_evidence_for_the_same_topic_and_message_id(store):
    save_observation(store)
    before = store.snapshot("9201")
    save_observation(store, observation(channel="linuxdo_tg"))

    snapshot = Store(Outbox(store.box.path)).snapshot("9201")

    assert snapshot["version"] > before["version"]
    assert snapshot["fingerprint"] == before["fingerprint"]
    assert {item["ref"] for item in snapshot["evidence"]} == {"tg:LinuxDoNew:61", "tg:linuxdo_tg:61"}
    assert {item["url"] for item in snapshot["evidence"]} == {
        "https://t.me/LinuxDoNew/61", "https://t.me/linuxdo_tg/61",
    }
    assert store.stats()["observations"] == 2


def test_baseline_stays_historical_until_a_new_live_reply_arrives(store):
    save_observation(store, baseline=True)
    historical = store.snapshot("9201")
    assert historical["status"] == "historical"
    assert historical["historical"] == 1
    assert store.claim(now=historical["next_attempt"] + 3600) == []

    assert save_observation(store) == 0
    assert store.snapshot("9201")["status"] == "historical"

    save_observation(store, observation(message_id=62, post_number=2, body="新增申请名额已开放。"))
    reopened = Store(Outbox(store.box.path))
    live = reopened.snapshot("9201")
    assert live["status"] == "pending"
    assert live["historical"] == 0
    assert len(live["evidence"]) == 2
    assert reopened.claim(now=live["next_attempt"]) == ["9201"]


def test_legacy_import_preserves_cursor_and_previously_delivered_topics(store):
    state = {
        "channels": {"LinuxDoNew": {"last_seen_id": 60}},
        "delivered_topics": {"9201": {"delivered_at": PUBLISHED_AT}},
        "delivery_queue": [],
    }

    assert store.import_legacy(state) == {"channels": 1, "delivered": 1, "queued": 0}
    assert store.checkpoint("LinuxDoNew")["cursor"] == 60
    save_observation(store, baseline=True)
    reopened = Store(Outbox(store.box.path))

    assert reopened.snapshot("9201")["previously_notified"] is True
    assert reopened.import_legacy(state) == {"channels": 0, "delivered": 0, "queued": 0}
    assert reopened.checkpoint("LinuxDoNew")["cursor"] == 61
    assert reopened.box.counts() == {}


def test_legacy_queue_reuses_original_text_key_across_import_and_restart(store):
    message = "合成资源通知 [原帖](https://linux.do/t/topic/9201)"
    existing_id = store.box.enqueue(
        profile="webops", job_id=JOB_ID, kind="text",
        payload={"text": message}, key=text_key(message, JOB_ID),
    )
    state = {"delivery_queue": [{"message": message}]}

    assert store.import_legacy(state)["queued"] == 1
    reopened = Store(Outbox(store.box.path))
    assert reopened.import_legacy(state)["queued"] == 0
    save_observation(reopened, baseline=True)

    notifications = reopened.snapshot("9201")["notifications"]
    assert len(notifications) == 1
    assert notifications[0]["event_id"] == existing_id
    assert notifications[0]["delivery_status"] == "pending"
    assert reopened.box.counts() == {"pending": 1}


def test_claim_lease_survives_restart_and_allows_recovery_after_expiry(store):
    save_observation(store)
    ready_at = store.snapshot("9201")["next_attempt"]

    assert store.claim(now=ready_at) == ["9201"]
    reopened = Store(Outbox(store.box.path))
    lease_until = reopened.snapshot("9201")["lease_until"]
    assert lease_until > ready_at
    assert reopened.claim(now=lease_until - 1) == []
    assert reopened.claim(now=lease_until) == ["9201"]


def test_new_evidence_rejects_a_stale_push_and_makes_the_topic_claimable(store):
    save_observation(store)
    assert store.claim(now=store.snapshot("9201")["next_attempt"]) == ["9201"]
    judged_snapshot = store.snapshot("9201")
    store.add_evidence("9201", [complete_evidence(body="申请已关闭，请勿继续领取。")])

    event_id = store.finish(judged_snapshot, push_decision(), message="旧判断仍声称开放。")

    current = Store(Outbox(store.box.path)).snapshot("9201")
    assert event_id is None
    assert current["version"] > judged_snapshot["version"]
    assert current["status"] == "pending"
    assert current["lease_until"] == 0
    assert current["notifications"] == []
    assert store.box.counts() == {}
    assert store.claim(now=current["next_attempt"]) == ["9201"]


def test_same_core_facts_from_another_source_do_not_enqueue_changed_wording_twice(store):
    save_observation(store)
    first_message = "合成工具已开放申请。"
    first_id = store.finish(store.snapshot("9201"), push_decision(), message=first_message)
    assert first_id is not None
    save_observation(store, observation(channel="linuxdo_tg", body="工具现在可以申请。"))
    same_facts = push_decision()
    same_facts["facts"].reverse()
    reopened = Store(Outbox(store.box.path))

    second_id = reopened.finish(reopened.snapshot("9201"), same_facts, message="现在可申请合成工具。")

    assert second_id == first_id
    assert reopened.box.counts() == {"pending": 1}
    assert len(reopened.snapshot("9201")["notifications"]) == 1
    with reopened.box.connect() as db:
        payload = json.loads(db.execute("SELECT payload FROM events WHERE id=?", (first_id,)).fetchone()[0])
    assert payload["text"] == first_message


def test_finish_rolls_back_outbox_enqueue_if_notification_binding_fails(store):
    save_observation(store)
    snapshot = store.snapshot("9201")
    with store.box.connect() as db:
        db.execute("""
            CREATE TRIGGER reject_notification BEFORE INSERT ON linuxdo_notifications
            BEGIN SELECT RAISE(ABORT, 'injected notification failure'); END
        """)

    with pytest.raises(sqlite3.IntegrityError, match="injected notification failure"):
        store.finish(snapshot, push_decision(), message="合成工具已开放申请。")

    reopened = Store(Outbox(store.box.path))
    assert reopened.snapshot("9201")["status"] == snapshot["status"]
    assert reopened.snapshot("9201")["notifications"] == []
    assert reopened.box.counts() == {}


@pytest.mark.parametrize("delivery_status", ["pending", "sending", "uncertain", "failed"])
def test_delivery_sync_requires_an_actual_delivered_receipt(store, delivery_status):
    save_observation(store)
    event_id = store.finish(store.snapshot("9201"), push_decision(), message="合成工具已开放申请。")
    assert event_id is not None
    store.box.finish(event_id, delivery_status)
    reopened = Store(Outbox(store.box.path))

    reopened.sync_deliveries()

    assert reopened.snapshot("9201")["status"] == "queued"
    store.box.finish(event_id, "delivered", receipt={"message_id": 101})
    reopened.sync_deliveries()
    assert reopened.snapshot("9201")["status"] == "delivered"


def test_delivery_sync_waits_for_every_fact_notification(store):
    save_observation(store)
    first_id = store.finish(store.snapshot("9201"), push_decision(), message="首轮名额开放。")
    second_decision = push_decision()
    second_decision["facts"][1]["value"] = "追加名额开放"
    second_id = store.finish(store.snapshot("9201"), second_decision, message="追加名额开放。")
    assert first_id and second_id and first_id != second_id

    store.box.finish(first_id, "delivered", receipt={"message_id": 101})
    store.sync_deliveries()
    assert store.snapshot("9201")["status"] == "queued"

    store.box.finish(second_id, "delivered", receipt={"message_id": 102})
    store.sync_deliveries()
    assert store.snapshot("9201")["status"] == "delivered"


def test_deferred_topic_survives_restart_and_becomes_claimable_after_retry_time(store):
    save_observation(store)
    assert store.claim(now=store.snapshot("9201")["next_attempt"]) == ["9201"]

    store.defer("9201", "temporary fixture failure")

    reopened = Store(Outbox(store.box.path))
    deferred = reopened.snapshot("9201")
    assert deferred["status"] == "hold"
    assert deferred["attempts"] == 1
    assert deferred["lease_until"] == 0
    assert deferred["error"] == "temporary fixture failure"
    assert deferred["evidence"]
    assert reopened.claim(now=deferred["next_attempt"] - 1) == []
    assert reopened.claim(now=deferred["next_attempt"]) == ["9201"]


def judge_snapshot():
    return {
        "topic_id": "9301",
        "title": "合成自动化方法",
        "previously_notified": False,
        "notifications": [],
        "evidence": [{
            "ref": "tg:LinuxDoNew:71",
            "origin": "telegram",
            "post_number": 1,
            "url": "https://t.me/LinuxDoNew/71",
            "title": "合成自动化方法",
            "body": "合成脚本可按顺序执行三个操作。详情见资源页。",
            "links": ["https://example.invalid/resource"],
            "complete": False,
        }],
    }


def judge_result():
    return {
        "decision": "push",
        "categories": ["C2"],
        "content_type": "tutorial",
        "reason": "脚本提供可复用的顺序操作方法。",
        "action": "阅读资源页中的操作步骤。",
        "missing_evidence": [],
        "expires_at": "",
        "change_kind": "initial",
        "provider": "",
        "region_scope": "unknown",
        "facts": [{
            "category": "C2", "kind": "method", "subject": "合成脚本",
            "value": "按顺序执行三个操作",
            "evidence": [{"ref": "tg:LinuxDoNew:71", "quote": "合成脚本可按顺序执行三个操作。"}],
        }],
    }


def validate_judgment(value=None, snapshot=None):
    return judge.validate_result(value or judge_result(), snapshot or judge_snapshot(), policy.default_policy())


def test_grounded_reusable_method_passes_the_judge_boundary():
    value = judge_result()

    result = validate_judgment(value)

    assert result["decision"] == "push"
    assert result["facts"] == value["facts"]
    assert result["categories"] == ["C2"]


@pytest.mark.parametrize("decision", ["push", "hold", "skip"])
def test_expiration_requires_original_evidence_for_every_decision(decision):
    value = judge_result()
    value.update(decision=decision, expires_at="2026-01-02T00:00:00+08:00")

    with pytest.raises(judge.JudgeError, match="expiration date absent from evidence"):
        validate_judgment(value)


@pytest.mark.parametrize("field", ["title", "body"])
def test_expiration_can_be_grounded_in_the_source_title_or_body(field, monkeypatch):
    now = datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp()
    monkeypatch.setattr(judge.time, "time", lambda: now)
    snapshot = judge_snapshot()
    snapshot["evidence"][0][field] += "领取截止时间：2026-01-02 00:00（北京时间）。"
    value = judge_result()
    value["expires_at"] = "2026-01-02T00:00:00+08:00"

    result = validate_judgment(value, snapshot)

    assert result["decision"] == "push"
    assert result["expires_at"] == value["expires_at"]


@pytest.mark.parametrize("fault", ["unknown_ref", "invented_quote", "invented_value", "reason_url", "action_url"])
def test_judge_rejects_facts_and_links_not_grounded_in_supplied_evidence(fault):
    value = judge_result()
    if fault == "unknown_ref":
        value["facts"][0]["evidence"][0]["ref"] = "topic:unprovided:1"
    elif fault == "invented_quote":
        value["facts"][0]["evidence"][0]["quote"] = "不存在于证据中的操作说明。"
    elif fault == "invented_value":
        value["facts"][0]["value"] = "自动完成全部部署并保证零故障"
    else:
        field = "reason" if fault == "reason_url" else "action"
        value[field] += " https://example.invalid/unprovided"

    with pytest.raises(judge.JudgeError):
        validate_judgment(value)


@pytest.mark.parametrize("field", ["categories", "fact_category"])
def test_judge_rejects_unknown_categories(field):
    value = judge_result()
    if field == "categories":
        value["categories"] = ["Z1"]
    else:
        value["facts"][0]["category"] = "Z1"

    with pytest.raises(judge.JudgeError):
        validate_judgment(value)


@pytest.mark.parametrize("category", sorted(EXPECTED_EXCLUDED))
def test_closed_categories_cannot_push_even_when_facts_are_grounded(category):
    value = judge_result()
    value["categories"] = [category]
    value["facts"][0]["category"] = category

    result = validate_judgment(value)

    assert result["decision"] == "skip"
    assert result["facts"] == []
    assert result["categories"] == []


def test_mixed_topic_keeps_only_facts_in_the_allowed_categories():
    value = judge_result()
    snapshot = judge_snapshot()
    snapshot["evidence"][0]["body"] += "合成桌面工具提供窗口布局功能。"
    value["categories"].append("F1")
    value["facts"].append({
        "category": "F1", "kind": "capability", "subject": "合成桌面工具", "value": "窗口布局功能",
        "evidence": [{"ref": "tg:LinuxDoNew:71", "quote": "合成桌面工具提供窗口布局功能。"}],
    })

    result = validate_judgment(value, snapshot)

    assert result["decision"] == "push"
    assert result["categories"] == ["C2"]
    assert result["facts"] == [value["facts"][0]]


@pytest.mark.parametrize("content_type", ["question", "chatter", "news", "advertisement"])
def test_non_actionable_content_types_do_not_push(content_type):
    value = judge_result()
    value["content_type"] = content_type

    assert validate_judgment(value)["decision"] == "skip"


def test_missing_critical_evidence_keeps_a_potential_candidate_on_hold():
    value = judge_result()
    value["missing_evidence"] = ["完整操作步骤尚未提供"]

    result = validate_judgment(value)

    assert result["decision"] == "hold"
    assert result["facts"]


@pytest.mark.parametrize("change_kind", ["initial", "none"])
def test_previously_notified_topic_needs_a_substantive_change(change_kind):
    value = judge_result()
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    value["change_kind"] = change_kind

    assert validate_judgment(value, snapshot)["decision"] == "skip"


def test_relabeling_the_same_core_fact_as_new_does_not_repeat_a_notification():
    value = judge_result()
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    snapshot["notifications"] = [{"facts": copy.deepcopy(value["facts"])}]
    value["change_kind"] = "new_method"
    value["reason"] = "换一种文案描述同一操作方法。"

    assert validate_judgment(value, snapshot)["decision"] == "skip"


@pytest.mark.parametrize(("change_kind", "fact_kind"), [("restocked", "availability"), ("correction", "correction")])
def test_restock_or_correction_without_an_explicit_change_quote_is_held(change_kind, fact_kind):
    value = judge_result()
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    value["change_kind"] = change_kind
    value["facts"][0]["kind"] = fact_kind

    assert validate_judgment(value, snapshot)["decision"] == "hold"


@pytest.mark.parametrize(
    ("change_kind", "fact_kind", "quote", "fact_value"),
    [
        ("restocked", "availability", "合成工具补货后重新开放申请。", "重新开放申请"),
        ("correction", "correction", "更正：合成脚本需要先配置目录再运行。", "先配置目录再运行"),
    ],
)
def test_restock_or_correction_with_a_grounded_change_can_notify_again(change_kind, fact_kind, quote, fact_value):
    value = judge_result()
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    snapshot["notifications"] = [{"facts": copy.deepcopy(value["facts"])}]
    snapshot["evidence"][0]["body"] += quote
    value["change_kind"] = change_kind
    value["facts"][0].update(kind=fact_kind, value=fact_value)
    value["facts"][0]["evidence"][0]["quote"] = quote

    assert validate_judgment(value, snapshot)["decision"] == "push"


def availability_restock(*, subject="合成工具申请", url="https://example.invalid/resource",
                         ref="topic:2", quote="合成工具补货后重新开放申请，资源 https://example.invalid/resource。"):
    value = judge_result()
    value.update(change_kind="restocked", facts=[
        {
            "category": "C2", "kind": "resource", "subject": "合成工具", "value": url,
            "evidence": [{"ref": ref, "quote": quote}],
        },
        {
            "category": "C2", "kind": "availability", "subject": subject, "value": "开放",
            "evidence": [{"ref": ref, "quote": quote}],
        },
    ])
    return value


def restock_evidence(post_number, *, subject="合成工具申请", url="https://example.invalid/resource"):
    quote = f"合成工具补货后重新开放{subject}，资源 {url}。"
    return {
        "ref": f"topic:{post_number}", "origin": "topic", "post_number": post_number,
        "url": f"https://linux.do/t/topic/9201/{post_number}", "title": "合成工具补货",
        "body": quote, "links": [url], "complete": True,
    }


def test_same_quota_restock_at_reply_two_and_five_creates_separate_events(store):
    save_observation(store)
    store.add_evidence("9201", [restock_evidence(1)])
    initial = availability_restock(ref="topic:1", quote=restock_evidence(1)["body"])
    initial["change_kind"] = "initial"
    first_id = store.finish(store.snapshot("9201"), initial, message="首次开放申请。")

    event_ids = [first_id]
    for post_number in (2, 5):
        evidence = restock_evidence(post_number)
        store.add_evidence("9201", [evidence])
        snapshot = store.snapshot("9201")
        decision = availability_restock(ref=evidence["ref"], quote=evidence["body"])

        validated = validate_judgment(decision, snapshot)
        event_ids.append(store.finish(snapshot, validated, message=f"第 {post_number} 帖再次开放申请。"))

    assert all(event_ids)
    assert len(set(event_ids)) == 3
    assert len(Store(Outbox(store.box.path)).snapshot("9201")["notifications"]) == 3


def test_same_reply_restock_copy_after_restart_reuses_one_event(store):
    save_observation(store)
    quote = "合成工具补货后重新开放申请，资源 https://example.invalid/resource。"
    store.add_evidence("9201", [restock_evidence(1)])
    initial_evidence = restock_evidence(1)
    initial = availability_restock(ref="topic:1", quote=initial_evidence["body"])
    initial["change_kind"] = "initial"
    store.finish(store.snapshot("9201"), initial, message="首次开放申请。")

    for channel, message_id in (("LinuxDoNew", 62), ("linuxdo_tg", 63)):
        save_observation(store, observation(
            channel=channel, message_id=message_id, post_number=5, body=quote,
        ))

    first_snapshot = store.snapshot("9201")
    first_ref = "tg:LinuxDoNew:62"
    first = validate_judgment(availability_restock(ref=first_ref, quote=quote), first_snapshot)
    first_id = Store(Outbox(store.box.path)).finish(
        first_snapshot, first, message="第 5 帖再次开放申请。",
    )
    assert first["decision"] == "push"
    assert first_id

    reopened = Store(Outbox(store.box.path))
    second_snapshot = reopened.snapshot("9201")
    second_ref = "tg:linuxdo_tg:63"
    second = validate_judgment(availability_restock(ref=second_ref, quote=quote), second_snapshot)
    assert second["decision"] == "skip"

    repeated_push = availability_restock(ref=second_ref, quote=quote)
    repeated_id = reopened.finish(second_snapshot, repeated_push, message="转载再次开放申请。")

    assert repeated_id == first_id
    assert len(reopened.snapshot("9201")["notifications"]) == 2


def test_late_reply_three_cannot_repeat_restock_already_notified_at_reply_five():
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    first_fact = availability_restock(ref="tg:LinuxDoNew:70")["facts"]
    later_fact = availability_restock(ref="topic:5")["facts"]
    snapshot["notifications"] = [
        {"delivery_status": "delivered", "facts": first_fact},
        {"delivery_status": "delivered", "facts": later_fact},
    ]
    quote = "合成工具补货后重新开放申请，资源 https://example.invalid/resource。"
    snapshot["evidence"] = [{
        "ref": "topic:3", "origin": "topic", "post_number": 3,
        "url": "https://linux.do/t/topic/9301/3", "title": "合成工具补货",
        "body": quote, "links": ["https://example.invalid/resource"], "complete": True,
    }]
    snapshot["evidence_positions"] = {"tg:LinuxDoNew:70": 2, "topic:5": 5, "topic:3": 3}

    result = validate_judgment(availability_restock(ref="topic:3", quote=quote), snapshot)

    assert result["decision"] == "skip"


def test_later_restock_for_one_resource_is_not_blocked_by_another_resource_reply_number():
    quote_a = "合成工具甲补货后重新开放申请，资源 https://example.invalid/resource-a。"
    quote_b = "合成工具乙补货后重新开放申请，资源 https://example.invalid/resource-b。"
    prior_a = availability_restock(
        subject="合成工具甲申请", url="https://example.invalid/resource-a", ref="tg:LinuxDoNew:70",
        quote=quote_a,
    )["facts"]
    prior_b = availability_restock(
        subject="合成工具乙申请", url="https://example.invalid/resource-b", ref="topic:5", quote=quote_b,
    )["facts"]
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    snapshot["notifications"] = [
        {"delivery_status": "delivered", "facts": prior_a},
        {"delivery_status": "delivered", "facts": prior_b},
    ]
    snapshot["evidence"] = [{
        "ref": "topic:3", "origin": "topic", "post_number": 3,
        "url": "https://linux.do/t/topic/9301/3", "title": "合成工具甲补货",
        "body": quote_a, "links": ["https://example.invalid/resource-a"], "complete": True,
    }]
    snapshot["evidence_positions"] = {"tg:LinuxDoNew:70": 2, "topic:5": 5, "topic:3": 3}

    result = validate_judgment(availability_restock(
        subject="合成工具甲申请", url="https://example.invalid/resource-a", ref="topic:3", quote=quote_a,
    ), snapshot)

    assert result["decision"] == "push"


def test_missing_old_reply_position_does_not_allow_a_restock_to_bypass_duplicate_gate():
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    snapshot["notifications"] = [{
        "delivery_status": "delivered",
        "facts": availability_restock(ref="tg:LinuxDoNew:70")["facts"],
    }]
    quote = "合成工具补货后重新开放申请，资源 https://example.invalid/resource。"
    snapshot["evidence"] = [{
        "ref": "topic:3", "origin": "topic", "post_number": 3,
        "url": "https://linux.do/t/topic/9301/3", "title": "合成工具补货",
        "body": quote, "links": ["https://example.invalid/resource"], "complete": True,
    }]
    snapshot["evidence_positions"] = {"topic:3": 3}

    result = validate_judgment(availability_restock(ref="topic:3", quote=quote), snapshot)

    assert result["decision"] == "skip"


def cloud_judgment(*, provider="tencent", region="中国香港"):
    official_url = {
        "tencent": "https://cloud.tencent.com/fixture/offer",
        "alibaba": "https://aliyun.com/fixture/offer",
        "other": "https://example.invalid/cloud-offer",
    }[provider]
    fields = {
        "price": "人民币 100 元",
        "billing_period": "12 个月",
        "configuration": "2 核 4 GB",
        "region": region,
        "eligibility": "仅新用户参与",
    }
    snapshot = judge_snapshot()
    snapshot["title"] = "合成轻量服务器促销"
    snapshot["evidence"] = [{
        "ref": "official:fixture-offer", "origin": "official", "post_number": 1,
        "url": official_url, "title": "合成轻量服务器促销",
        "body": "合成限时促销：" + "；".join(fields.values()) + "。", "links": [], "complete": True,
    }]
    value = judge_result()
    value.update(
        categories=["H2"], content_type="offer", provider=provider, region_scope="overseas",
        reason="合成轻量服务器促销提供明确首期价格。", action="阅读官方条件并核对参与资格。",
        facts=[{
            "category": "H2", "kind": kind, "subject": "合成轻量服务器", "value": text,
            "evidence": [{"ref": "official:fixture-offer", "quote": snapshot["evidence"][0]["body"]}],
        } for kind, text in fields.items()],
    )
    return value, snapshot


@pytest.mark.parametrize("provider", ["tencent", "alibaba"])
@pytest.mark.parametrize("region", ["中国香港", "新加坡"])
def test_official_cloud_offers_with_all_five_conditions_allow_hong_kong_and_overseas(provider, region):
    value, snapshot = cloud_judgment(provider=provider, region=region)

    result = validate_judgment(value, snapshot)

    assert result["decision"] == "push"
    assert {fact["kind"] for fact in result["facts"]} >= {
        "price", "billing_period", "configuration", "region", "eligibility",
    }


@pytest.mark.parametrize("missing_kind", ["price", "billing_period", "configuration", "region", "eligibility"])
def test_cloud_offer_missing_any_key_condition_is_held(missing_kind):
    value, snapshot = cloud_judgment()
    value["facts"] = [fact for fact in value["facts"] if fact["kind"] != missing_kind]

    assert validate_judgment(value, snapshot)["decision"] == "hold"


def test_other_cloud_provider_is_outside_the_approved_boundary():
    value, snapshot = cloud_judgment(provider="other")

    assert validate_judgment(value, snapshot)["decision"] == "skip"


@pytest.mark.parametrize(
    ("provider", "url", "origin"),
    [
        ("tencent", "https://cloud.tencent.com.example.invalid/fixture/offer", "official"),
        ("alibaba", "https://aliyun.com.example.invalid/fixture/offer", "official"),
        ("tencent", "https://aliyun.com/fixture/offer", "official"),
        ("tencent", "https://cloud.tencent.com/fixture/offer", "telegram"),
    ],
)
def test_cloud_conditions_require_the_selected_providers_official_evidence(provider, url, origin):
    value, snapshot = cloud_judgment(provider=provider)
    snapshot["evidence"][0].update(url=url, origin=origin)

    assert validate_judgment(value, snapshot)["decision"] == "hold"


def test_unrelated_official_quote_cannot_confirm_conditions_found_only_in_a_repost():
    value, snapshot = cloud_judgment()
    repost = copy.deepcopy(snapshot["evidence"][0])
    repost.update(ref="tg:LinuxDoNew:72", origin="telegram", url="https://t.me/LinuxDoNew/72")
    snapshot["evidence"][0]["body"] = "这是合成产品信息页，具体优惠条件尚未公开。"
    snapshot["evidence"].append(repost)
    for fact in value["facts"]:
        fact["evidence"] = [
            {"ref": repost["ref"], "quote": fact["value"]},
            {"ref": "official:fixture-offer", "quote": snapshot["evidence"][0]["body"]},
        ]

    assert validate_judgment(value, snapshot)["decision"] == "hold"


def test_cloud_notification_explicitly_marks_unknown_renewal_price():
    value, snapshot = cloud_judgment()
    decision = validate_judgment(value, snapshot)

    message = judge.format_message(snapshot, decision)

    assert "续费价格：原文未说明" in message
    assert "人民币 100 元" in message
    assert "中国香港" in message


def collector_config(*, channels=CHANNELS, max_pages=2):
    return {
        "version": 1,
        "channels": list(channels),
        "max_pages": max_pages,
        "fetch_timeout_seconds": 8,
        "source_budget_seconds": 40,
    }


def channel_page(channel, *message_ids):
    return "".join(
        message_html(channel, number, structured_message(channel, str(10000 + number)))
        for number in message_ids
    )


def test_collection_page_budget_preserves_the_gap_for_the_next_run(store):
    channel = "LinuxDoNew"
    baseline = sources.parse_page(channel_page(channel, 100), channel)
    store.save_page(channel, baseline, {
        "cursor": 100, "baseline": True, "backfill_before": None, "backfill_until": None,
    }, baseline=True)
    base_url = f"https://t.me/s/{channel}"
    pages = {
        base_url: channel_page(channel, 105, 104),
        base_url + "?before=104": channel_page(channel, 103, 102),
    }
    getter = Mock(side_effect=lambda url, **kwargs: pages[url])

    first = pipeline.collect_source(store, channel, collector_config(), getter=getter)

    assert first == {"channel": channel, "pages": 2, "changed": 4, "caught_up": False, "error": ""}
    assert getter.call_count == 2
    assert store.checkpoint(channel) == {
        "cursor": 105, "baseline": True, "backfill_before": 102, "backfill_until": 100,
    }

    pages = {
        base_url: channel_page(channel, 106, 105),
        base_url + "?before=102": channel_page(channel, 101, 100),
    }
    getter.reset_mock()
    reopened = Store(Outbox(store.box.path))
    second = pipeline.collect_source(reopened, channel, collector_config(), getter=getter)

    assert second == {"channel": channel, "pages": 2, "changed": 2, "caught_up": True, "error": ""}
    assert getter.call_count == 2
    assert reopened.checkpoint(channel) == {
        "cursor": 106, "baseline": True, "backfill_before": None, "backfill_until": None,
    }
    with reopened.box.connect() as db:
        observed = {row[0] for row in db.execute("SELECT message_id FROM linuxdo_observations WHERE channel=?", (channel,))}
    assert observed == set(range(100, 107))
    assert reopened.snapshot("10100")["status"] == "historical"


def test_helper_collection_uses_only_the_helper_source_and_never_calls_legacy_getter(
        store, monkeypatch):
    class FixtureHelperAccount:
        identity = {"account_id": 90001, "bot_id": 90002}

        def __init__(self):
            self.pages = [[{
                "message_id": 111,
                "incoming": True,
                "text": "【watching_first_post】 合成主题\n合成脚本可按顺序执行三个操作。",
                "published_at": PUBLISHED_AT,
                "edited_at": "",
                "links": [{
                    "label": "合成主题",
                    "url": "https://linux.do/t/topic/10111/1",
                    "header": True,
                }],
                "buttons": [],
            }], []]
            self.calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def page(self, **kwargs):
            self.calls.append(kwargs)
            return self.pages.pop(0)

    account = FixtureHelperAccount()
    identity = digest(account.identity)
    HelperStore(store).save_page([], {
        "identity": identity,
        "baseline": True,
        "baseline_id": 100,
        "cursor": 100,
        "retry_at": 0,
    })
    delegated = []

    def collect_helper(actual_store, config):
        delegated.append(config)
        return asyncio.run(helper_collect.collect_async(
            actual_store, config, account_factory=lambda: account,
        ))

    monkeypatch.setattr(helper_collect.following, "available", lambda: (None, ""))
    monkeypatch.setattr(helper_collect, "collect", collect_helper)
    legacy_getter = Mock(side_effect=AssertionError("legacy channel getter must not run"))

    result = pipeline.collect(store, config=collector_config(), getter=legacy_getter)

    assert result["source"] == HELPER_SOURCE
    assert result["changed"] == 1
    assert result["error"] == ""
    assert delegated and delegated[0]["mode"] == "helper"
    assert account.calls[0] == {"after": 100, "limit": 100}
    legacy_getter.assert_not_called()
    assert store.checkpoint(HELPER_SOURCE)["cursor"] == 111
    assert store.stats()["observations"] == 1


def test_judge_failure_retains_helper_evidence_and_allows_later_collection(
        store, intelligence_store):
    first_post = helper_observation(message_id=71, body="合成脚本可按顺序执行三个操作。")
    assert capture_helper(store, intelligence_store, first_post) is True
    request = Mock(side_effect=judge.JudgeError("injected judgment failure"))

    result = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=lambda snapshot: {"intent": "non_trade", "offers": []},
    )

    assert result["progress"] is False
    assert result["error"].startswith("value: JudgeError:")
    assert "injected judgment failure" in result["error"]
    assert result["exhausted"] is False
    assert result["queued"] == 0
    assert request.call_count == 1
    retained = intelligence_item(intelligence_store)
    assert retained["status"] == "retry"
    assert retained["attempts"] == 1
    assert retained["error"].startswith("JudgeError:")
    retained_snapshot = json.loads(retained["context"])["snapshot"]
    assert retained_snapshot["evidence"]
    assert retained_snapshot["evidence"][0]["ref"] == f"tg:{HELPER_SOURCE}:71"

    second_post = helper_observation(message_id=72, body="新增申请入口仍可按三个步骤执行。")
    assert capture_helper(store, intelligence_store, second_post) is True
    recollected = intelligence_item(intelligence_store)
    assert recollected["status"] == "pending"
    assert "新增申请入口" in recollected["body"]
    assert intelligence_store.box.counts() == {}


def test_invented_expiration_cannot_close_a_held_candidate(store, intelligence_store, monkeypatch):
    post = helper_observation(message_id=71)
    assert capture_helper(store, intelligence_store, post) is True
    now = datetime.fromisoformat("2026-01-03T00:00:00+00:00").timestamp()
    monkeypatch.setattr(intelligence_pipeline.time, "time", lambda: now)
    held = intelligence_value(
        ref=f"tg:{HELPER_SOURCE}:71",
        decision="hold",
        expires_at="2026-01-02T00:00:00+08:00",
        categories=["C2"],
    )
    request = Mock(return_value=held)

    result = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=lambda snapshot: {"intent": "non_trade", "offers": []},
    )

    assert result["progress"] is False
    assert result["error"].startswith("value: JudgeError:")
    assert "expiration date absent from evidence" in result["error"]
    assert result["exhausted"] is False
    assert result["queued"] == 0
    request.assert_called_once()
    retained = intelligence_item(intelligence_store)
    assert retained["status"] == "retry"
    assert retained["attempts"] == 1
    assert retained["result"] is None
    assert retained["error"].startswith("JudgeError:")
    assert intelligence_store.box.counts() == {}


def test_grounded_future_expiration_is_preserved_for_a_held_candidate(
        store, intelligence_store, monkeypatch):
    now = datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp()
    body = "合成脚本可按顺序执行三个操作。领取截止时间：2026-01-02 00:00（北京时间）。"
    post = helper_observation(message_id=71, body=body)
    assert capture_helper(store, intelligence_store, post) is True
    monkeypatch.setattr(intelligence_pipeline.time, "time", lambda: now)
    held = intelligence_value(
        ref=f"tg:{HELPER_SOURCE}:71",
        decision="hold",
        expires_at="2026-01-02T00:00:00+08:00",
        categories=["C2"],
    )

    result = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=Mock(return_value=held),
        trade_request=lambda snapshot: {"intent": "non_trade", "offers": []},
    )

    assert result == {"progress": True, "decision": "hold", "queued": 0}
    retained = intelligence_item(intelligence_store)
    assert retained["status"] == "needs_info"
    assert json.loads(retained["result"])["expires_at"] == held["expires_at"]
    assert retained["attempts"] == 0
    assert intelligence_store.box.counts() == {}


def test_grounded_expired_push_does_not_enqueue_a_notification(
        store, intelligence_store, monkeypatch):
    now = datetime.fromisoformat("2026-01-03T00:00:00+00:00").timestamp()
    body = "合成脚本可按顺序执行三个操作。领取截止时间：2026-01-02 00:00（北京时间）。"
    post = helper_observation(message_id=71, body=body)
    assert capture_helper(store, intelligence_store, post) is True
    monkeypatch.setattr(intelligence_pipeline.time, "time", lambda: now)
    value = intelligence_value(
        ref=f"tg:{HELPER_SOURCE}:71",
        expires_at="2026-01-02T00:00:00+08:00",
    )

    result = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=Mock(return_value=value),
        trade_request=lambda snapshot: {"intent": "non_trade", "offers": []},
    )

    assert result == {"progress": True, "decision": "skip", "queued": 0}
    retained = intelligence_item(intelligence_store)
    stored = json.loads(retained["result"])
    assert retained["status"] == "excluded"
    assert stored["policy_decision"] == "skip"
    assert retained["event_id"] is None
    assert intelligence_store.box.counts() == {}


def test_cached_hold_is_reused_until_new_helper_evidence_requires_a_fresh_judgment(
        store, intelligence_store):
    post = helper_observation(message_id=71)
    assert capture_helper(store, intelligence_store, post) is True
    held = intelligence_value(
        ref=f"tg:{HELPER_SOURCE}:71", decision="hold", categories=["C2"]
    )
    ready = intelligence_value(ref=f"tg:{HELPER_SOURCE}:71")
    request = Mock(side_effect=[held, ready])
    trade_request = lambda snapshot: {"intent": "non_trade", "offers": []}

    first = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=trade_request,
    )
    second = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=trade_request,
    )

    assert first["decision"] == second["decision"] == "hold"
    assert request.call_count == 1
    assert intelligence_store.box.counts() == {}

    updated = helper_observation(
        message_id=71,
        body="合成脚本可按顺序执行三个操作。完整步骤：先创建目录，再写入配置，最后启动脚本。",
    )
    assert capture_helper(store, intelligence_store, updated) is True
    third = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=trade_request,
    )

    assert third["decision"] == "push"
    assert third["queued"] == 1
    assert request.call_count == 2
    assert intelligence_store.box.counts() == {"pending": 1}


def test_restart_and_recollection_preserve_original_deadline_context_and_cached_skip(
        store, intelligence_store, monkeypatch):
    now = datetime.fromisoformat("2026-10-09T04:00:00+00:00").timestamp()
    body = "合成脚本可按顺序执行三个操作。领取截止时间：2026-10-09 09:00（北京时间）。"
    post = helper_observation(message_id=71, body=body)
    assert capture_helper(store, intelligence_store, post) is True
    monkeypatch.setattr(intelligence_pipeline.time, "time", lambda: now)
    expired = intelligence_value(
        ref=f"tg:{HELPER_SOURCE}:71",
        expires_at="2026-10-09T01:00:00+00:00",
    )
    request = Mock(return_value=expired)
    trade_request = lambda snapshot: {"intent": "non_trade", "offers": []}

    first = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=trade_request,
    )
    original = intelligence_item(intelligence_store)
    original_context = json.loads(original["context"])["snapshot"]
    assert first["decision"] == "skip"
    assert original["status"] == "excluded"
    assert original_context["evidence"][0]["source_published_at"] == PUBLISHED_AT

    reopened = Store(Outbox(store.box.path))
    reopened_intelligence = IntelligenceStore(Outbox(store.box.path))
    assert capture_helper(reopened, reopened_intelligence, post) is False
    second = intelligence_pipeline.process_item(
        reopened_intelligence,
        intelligence_item(reopened_intelligence),
        value_request=request,
        trade_request=trade_request,
    )

    assert second["decision"] == "skip"
    assert request.call_count == 1
    with reopened_intelligence.box.connect() as db:
        # Linux.do runs separate trade and value phases; both cache entries
        # are reused after reopening the same outbox.
        assert db.execute("SELECT count(*) FROM intel_model_cache").fetchone()[0] == 2
    replayed = intelligence_item(reopened_intelligence)
    assert replayed["status"] == "excluded"
    assert json.loads(replayed["context"])["snapshot"]["evidence"] == original_context["evidence"]
    assert reopened_intelligence.box.counts() == {}


@pytest.mark.parametrize("initial_published_at", [PUBLISHED_AT, ""], ids=["corrected-date", "newly-known-date"])
def test_changed_publication_time_invalidates_cached_time_judgment(
        store, intelligence_store, initial_published_at):
    body = judge_snapshot()["evidence"][0]["body"] + "本次活动今天截止。"
    initial = helper_observation(message_id=71, body=body, published_at=initial_published_at)
    assert capture_helper(store, intelligence_store, initial) is True
    held = intelligence_value(ref=f"tg:{HELPER_SOURCE}:71", decision="hold", categories=["C2"])
    request = Mock(return_value=held)
    trade_request = lambda snapshot: {"intent": "non_trade", "offers": []}
    first = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=trade_request,
    )
    before = intelligence_item(intelligence_store)

    corrected_date = "2026-01-03T03:04:05+00:00"
    corrected = helper_observation(message_id=71, body=body, published_at=corrected_date)
    assert capture_helper(store, intelligence_store, corrected) is True
    after = intelligence_item(intelligence_store)
    before_context = json.loads(before["context"])["snapshot"]
    after_context = json.loads(after["context"])["snapshot"]
    assert after_context["evidence"][0]["body"] == before_context["evidence"][0]["body"]
    assert after_context["evidence"][0]["source_published_at"] == corrected_date
    assert after["fingerprint"] != before["fingerprint"]

    second = intelligence_pipeline.process_item(
        intelligence_store,
        after,
        value_request=request,
        trade_request=trade_request,
    )

    assert first["decision"] == second["decision"] == "hold"
    assert request.call_count == 2
    judged_dates = [
        call.args[0]["evidence"][0]["source_published_at"]
        for call in request.call_args_list
    ]
    assert judged_dates == [initial_published_at, corrected_date]
    with intelligence_store.box.connect() as db:
        # Each Helper revision has an independent trade and value cache key.
        assert db.execute("SELECT count(*) FROM intel_model_cache").fetchone()[0] == 4
    assert intelligence_store.box.counts() == {}


def test_old_publication_date_does_not_automatically_expire_a_reusable_tutorial(
        store, intelligence_store):
    published_at = "2020-01-02T03:04:05+00:00"
    post = helper_observation(message_id=71, body="合成脚本可按顺序执行三个操作。", published_at=published_at)
    assert capture_helper(store, intelligence_store, post) is True
    request = Mock(return_value=intelligence_value(ref=f"tg:{HELPER_SOURCE}:71"))

    result = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=lambda snapshot: {"intent": "non_trade", "offers": []},
    )

    assert result["decision"] == "push"
    assert result["queued"] == 1
    request.assert_called_once()
    assert request.call_args.args[0]["evidence"][0]["source_published_at"] == published_at
    retained = intelligence_item(intelligence_store)
    assert retained["status"] == "queued"
    assert retained["event_id"]
    assert intelligence_store.box.counts() == {"pending": 1}


def test_expired_notification_is_terminal_and_a_new_deadline_can_notify_the_same_facts(
        store, intelligence_store, monkeypatch):
    clock = [datetime.fromisoformat("2026-10-09T00:00:00+00:00").timestamp()]
    monkeypatch.setattr(transport, "profile_env", lambda profile: {
        "TELEGRAM_BOT_TOKEN": "fixture-token", "TELEGRAM_HOME_CHANNEL": "123",
    })
    original_body = "合成脚本可按顺序执行三个操作。领取截止时间：2026-10-09 09:00（北京时间）。"
    post = helper_observation(message_id=71, body=original_body)
    assert capture_helper(store, intelligence_store, post) is True
    monkeypatch.setattr(intelligence_pipeline.time, "time", lambda: clock[0])
    first_decision = intelligence_value(
        ref=f"tg:{HELPER_SOURCE}:71", expires_at="2026-10-09T01:00:00+00:00"
    )
    next_decision = intelligence_value(
        ref=f"tg:{HELPER_SOURCE}:71", expires_at="2026-10-10T01:00:00+00:00"
    )
    request = Mock(side_effect=[first_decision, next_decision])
    trade_request = lambda snapshot: {"intent": "non_trade", "offers": []}
    first = intelligence_pipeline.process_item(
        intelligence_store,
        intelligence_item(intelligence_store),
        value_request=request,
        trade_request=trade_request,
    )
    assert first["decision"] == "push"
    first_id = intelligence_item(intelligence_store)["event_id"]
    assert first_id
    with intelligence_store.box.connect() as db:
        payload = json.loads(db.execute("SELECT payload FROM events WHERE id=?", (first_id,)).fetchone()[0])
    first_expiry = datetime.fromisoformat(first_decision["expires_at"]).timestamp()
    assert payload["expires_at"] == first_expiry
    assert isinstance(payload["expires_at"], float)

    clock[0] = first_expiry
    reopened = Store(Outbox(store.box.path))
    reopened_intelligence = IntelligenceStore(Outbox(store.box.path))
    requests = []

    def handler(http_request):
        requests.append(http_request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 81, "chat": {"id": 123}}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        first_event = reopened_intelligence.box.claim()
        assert first_event["id"] == first_id
        transport.deliver_event(reopened_intelligence.box, first_event, http_client)
        with reopened_intelligence.box.connect() as db:
            assert db.execute("SELECT status FROM events WHERE id=?", (first_id,)).fetchone()[0] == "suppressed"
        assert requests == []
        assert reopened_intelligence.box.claim() is None

        changed = helper_observation(
            message_id=71,
            body="合成脚本可按顺序执行三个操作。领取截止时间：2026-10-10 09:00（北京时间）。",
        )
        assert capture_helper(reopened, reopened_intelligence, changed) is True
        second = intelligence_pipeline.process_item(
            reopened_intelligence,
            intelligence_item(reopened_intelligence),
            value_request=request,
            trade_request=trade_request,
        )
        assert second["decision"] == "push"
        second_id = intelligence_item(reopened_intelligence)["event_id"]
        assert second_id and second_id != first_id
        assert request.call_count == 2
        assert request.call_args_list[1].args[0]["previously_notified"] is False
        second_event = reopened_intelligence.box.claim()
        assert second_event["id"] == second_id
        transport.deliver_event(reopened_intelligence.box, second_event, http_client)

    intelligence_pipeline.review(reopened_intelligence, limit=1)
    current = intelligence_item(reopened_intelligence)
    assert len(requests) == 1
    assert current["status"] == "delivered"
    with reopened_intelligence.box.connect() as db:
        statuses = {
            row["id"]: row["status"]
            for row in db.execute(
                "SELECT id,status FROM events WHERE id IN (?,?)", (first_id, second_id)
            )
        }
        assert statuses == {first_id: "suppressed", second_id: "delivered"}
        payload = json.loads(db.execute("SELECT payload FROM events WHERE id=?", (second_id,)).fetchone()[0])
    assert payload["expires_at"] == datetime.fromisoformat(next_decision["expires_at"]).timestamp()
    assert reopened_intelligence.box.claim() is None


def test_suppressed_facts_do_not_block_new_information_when_other_facts_were_delivered():
    value = judge_result()
    value["change_kind"] = "new_method"
    previous_facts = copy.deepcopy(value["facts"])
    previous_facts[0]["value"] = "已有的其他操作步骤"
    snapshot = judge_snapshot()
    snapshot["previously_notified"] = True
    snapshot["notifications"] = [
        {"delivery_status": "delivered", "facts": previous_facts},
        {"delivery_status": "suppressed", "facts": copy.deepcopy(value["facts"])},
    ]

    result = validate_judgment(value, snapshot)

    assert result["decision"] == "push"
    assert prompt_data(snapshot)["previous_facts"] == [previous_facts]


def test_equivalent_deadline_timezones_reuse_the_same_notification(store, monkeypatch):
    now = datetime.fromisoformat("2026-10-09T00:00:00+00:00").timestamp()
    monkeypatch.setattr(judge.time, "time", lambda: now)
    save_observation(store)
    first_decision = push_decision()
    first_decision["expires_at"] = "2026-10-10T01:00:00+00:00"
    first_id = store.finish(store.snapshot("9201"), first_decision, message="同一期合成优惠。")
    second_decision = push_decision()
    second_decision["expires_at"] = "2026-10-10T09:00:00+08:00"

    second_id = store.finish(store.snapshot("9201"), second_decision, message="同一期优惠使用北京时间。")

    assert first_id and second_id == first_id
    assert len(store.snapshot("9201")["notifications"]) == 1
    assert store.box.counts() == {"pending": 1}


@pytest.mark.parametrize(("expires_at", "expected_deadline"), [
    ("", None),
    ("2026-10-09T18:30:00+00:00", "截止：2026-10-10 02:30（北京时间）"),
    ("2026-10-10T02:30:00+08:00", "截止：2026-10-10 02:30（北京时间）"),
])
def test_notification_displays_only_the_explicit_deadline_in_beijing_time(expires_at, expected_deadline):
    decision = judge_result()
    decision["expires_at"] = expires_at

    message = judge.format_message(judge_snapshot(), decision)

    deadline_lines = [line for line in message.splitlines() if line.startswith("截止：")]
    assert deadline_lines == ([expected_deadline] if expected_deadline else [])
