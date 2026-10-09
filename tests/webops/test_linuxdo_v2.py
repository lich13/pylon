from __future__ import annotations

import copy
import json
import sqlite3
from datetime import datetime
from unittest.mock import Mock

import pytest

from pylon.events import text_key
from pylon.outbox import Outbox
from pylon.tasks.linuxdo import http, judge, pipeline, policy, sources
from pylon.tasks.linuxdo.common import JOB_ID
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


def observation(*, channel="LinuxDoNew", message_id=61, topic_id="9201", post_number=1, body="合成工具开放申请。"):
    return sources.Observation(
        channel=channel,
        message_id=message_id,
        topic_id=topic_id,
        post_number=post_number,
        title="合成工具开放申请",
        body=body,
        tags=["开发调优"],
        published_at=PUBLISHED_AT,
        source_url=f"https://t.me/{channel}/{message_id}",
        links=["https://example.invalid/resource"],
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


def test_one_failed_source_does_not_stop_the_other_two_sources(store):
    def getter(url, **kwargs):
        channel = url.rsplit("/", 1)[-1]
        if channel == "linuxdo_tg":
            raise http.FetchError("fixture source unavailable")
        return channel_page(channel, 111)

    result = pipeline.collect(store, config=collector_config(), getter=getter)

    rows = {row["channel"]: row for row in result["sources"]}
    assert result["changed"] == 2
    assert rows["linuxdo_tg"]["error"] == "fixture source unavailable"
    assert rows["LinuxDoNew"]["error"] == rows["linux_do_channel"]["error"] == ""
    assert store.checkpoint("LinuxDoNew")["cursor"] == 111
    assert store.checkpoint("linux_do_channel")["cursor"] == 111
    assert store.stats()["observations"] == 2


def test_judge_failure_retains_collected_evidence_and_allows_later_collection(store, monkeypatch):
    clock = [2_000_000_000.0]
    monkeypatch.setattr(pipeline.time, "time", lambda: clock[0])
    channel = "LinuxDoNew"
    store.save_page(channel, sources.Page(), {
        "cursor": 70, "baseline": True, "backfill_before": None, "backfill_until": None,
    })
    config = collector_config(channels=[channel], max_pages=1)
    first = pipeline.collect(store, config=config, getter=lambda url, **kwargs: channel_page(channel, 71))
    assert first["changed"] == 1
    clock[0] += 31

    request = Mock(side_effect=judge.JudgeError("injected judgment failure"))
    reviewed = pipeline.review(
        store, config=policy.default_policy(), model="fixture-model", request=request,
        topic_fetch=lambda topic_id, numbers: [], official_fetch=lambda snapshot: [],
    )

    assert reviewed == {"processed": 1, "errors": 1, "queued": 0}
    assert request.call_count == 1
    reopened = Store(Outbox(store.box.path))
    retained = reopened.snapshot("10071")
    assert retained["status"] == "hold"
    assert retained["evidence"]
    assert reopened.checkpoint(channel)["cursor"] == 71
    second = pipeline.collect(reopened, config=config, getter=lambda url, **kwargs: channel_page(channel, 72))
    assert second["changed"] == 1
    assert reopened.checkpoint(channel)["cursor"] == 72
    assert reopened.stats()["observations"] == 2
    assert reopened.box.counts() == {}


def test_invented_expiration_cannot_close_a_held_candidate(store, monkeypatch):
    now = datetime.fromisoformat("2026-01-03T00:00:00+00:00").timestamp()
    monkeypatch.setattr(pipeline.time, "time", lambda: now)
    save_observation(store, observation(message_id=71, body=judge_snapshot()["evidence"][0]["body"]))
    held = judge_result()
    held.update(decision="hold", missing_evidence=["完整操作步骤"], expires_at="2026-01-02T00:00:00+08:00")
    request = Mock(return_value=held)

    result = pipeline.process_topic(
        store, "9201", policy.default_policy(), model="fixture-model", request=request,
        topic_fetch=lambda topic_id, numbers: [], official_fetch=lambda snapshot: [],
    )

    assert result["decision"] == "hold"
    assert result["error"] == "JudgeError"
    request.assert_called_once()
    reopened = Store(Outbox(store.box.path))
    retained = reopened.snapshot("9201")
    assert retained["status"] == "hold"
    assert retained["expires_at"] is None
    assert retained["notifications"] == []
    assert reopened.claim(now=retained["next_attempt"]) == ["9201"]
    assert reopened.box.counts() == {}


def test_grounded_future_expiration_is_preserved_for_a_held_candidate(store, monkeypatch):
    now = datetime.fromisoformat("2026-01-01T00:00:00+00:00").timestamp()
    monkeypatch.setattr(pipeline.time, "time", lambda: now)
    body = judge_snapshot()["evidence"][0]["body"] + "领取截止时间：2026-01-02 00:00（北京时间）。"
    save_observation(store, observation(message_id=71, body=body))
    held = judge_result()
    held.update(decision="hold", missing_evidence=["完整操作步骤"], expires_at="2026-01-02T00:00:00+08:00")

    result = pipeline.process_topic(
        store, "9201", policy.default_policy(), model="fixture-model", request=Mock(return_value=held),
        topic_fetch=lambda topic_id, numbers: [], official_fetch=lambda snapshot: [],
    )

    assert result["decision"] == "hold"
    assert result["error"] == ""
    assert result["event_id"] is None
    reopened = Store(Outbox(store.box.path))
    retained = reopened.snapshot("9201")
    assert retained["status"] == "hold"
    assert retained["expires_at"] == datetime.fromisoformat(held["expires_at"]).timestamp()
    assert reopened.claim(now=retained["next_attempt"]) == ["9201"]
    assert reopened.box.counts() == {}


def test_grounded_expired_push_does_not_enqueue_a_notification(store, monkeypatch):
    now = datetime.fromisoformat("2026-01-03T00:00:00+00:00").timestamp()
    monkeypatch.setattr(pipeline.time, "time", lambda: now)
    body = judge_snapshot()["evidence"][0]["body"] + "领取截止时间：2026-01-02 00:00（北京时间）。"
    save_observation(store, observation(message_id=71, body=body))
    value = judge_result()
    value["expires_at"] = "2026-01-02T00:00:00+08:00"

    result = pipeline.process_topic(
        store, "9201", policy.default_policy(), model="fixture-model", request=Mock(return_value=value),
        topic_fetch=lambda topic_id, numbers: [], official_fetch=lambda snapshot: [],
    )

    assert result["decision"] == "skip"
    assert result["error"] == ""
    assert result["event_id"] is None
    retained = store.snapshot("9201")
    assert retained["status"] == "skip"
    assert retained["notifications"] == []
    assert store.box.counts() == {}


def test_cached_hold_is_reused_until_new_evidence_requires_a_fresh_judgment(store):
    save_observation(store, observation(message_id=71, body=judge_snapshot()["evidence"][0]["body"]))
    held = judge_result()
    held.update(decision="hold", missing_evidence=["完整操作步骤"])
    ready = judge_result()
    ready["facts"][0]["evidence"][0]["ref"] = "topic:9201:1"
    request = Mock(side_effect=[held, ready])
    topic_fetch = Mock(return_value=[])
    official_fetch = Mock(return_value=[])
    kwargs = {
        "model": "fixture-model", "request": request,
        "topic_fetch": topic_fetch, "official_fetch": official_fetch,
    }

    first = pipeline.process_topic(store, "9201", policy.default_policy(), **kwargs)
    second = pipeline.process_topic(Store(Outbox(store.box.path)), "9201", policy.default_policy(), **kwargs)

    assert first["decision"] == second["decision"] == "hold"
    assert first["error"] == second["error"] == ""
    assert request.call_count == 1
    assert topic_fetch.call_count == 1
    assert store.box.counts() == {}

    evidence = complete_evidence(body="合成脚本可按顺序执行三个操作。完整步骤：先创建目录，再写入配置，最后启动脚本。")
    store.add_evidence("9201", [evidence])
    third = pipeline.process_topic(store, "9201", policy.default_policy(), **kwargs)

    assert third["decision"] == "push"
    assert third["error"] == ""
    assert third["event_id"]
    assert request.call_count == 2
    assert store.box.counts() == {"pending": 1}
