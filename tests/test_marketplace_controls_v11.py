"""Owner controls exercised with synthetic cards, receipts, and SQLite state."""

import copy
import json
import sqlite3

import pytest

from pylon import config, inline
from pylon.outbox import Outbox
from pylon.tasks.marketplace import classifier, control, interface, novelty, policy, taxonomy
from pylon.tasks.marketplace.enrich import AnalysisError
from pylon.tasks.marketplace.store import Store


CATEGORY = "phone.smartphone"
OWNER = 123


@pytest.fixture
def market(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_HOME", str(tmp_path))
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "data/outbox.sqlite3"))
    environment = lambda profile: {
        "TELEGRAM_HOME_CHANNEL": str(OWNER), "TELEGRAM_BOT_TOKEN": "fixture-" + profile,
    }
    monkeypatch.setattr(config, "profile_env", environment)
    monkeypatch.setattr(inline, "profile_env", environment)
    settings = tmp_path / "config"
    settings.mkdir()
    (settings / "marketplace.json").write_text(json.dumps({
        "version": 3, "enabled": True, "categories": [], "groups": [],
        "types": [], "blocked_groups": [], "blocked_categories": [], "filters": {},
    }))
    (settings / "marketplace-wanted.json").write_text(json.dumps({
        "version": 2, "enabled": False, "targets": [], "blocked_groups": [],
    }))
    prior_extensions = copy.deepcopy(taxonomy.EXTENSIONS)
    taxonomy.refresh_extensions({})
    store = Store()
    with store.box.connect() as db:
        control.baseline(db)
    yield store
    taxonomy.refresh_extensions(prior_extensions)


def active(store):
    with store.box.connect() as db:
        return control.current(db)


def current_draft(store):
    with store.box.connect() as db:
        return interface.draft(db)


def replace_draft(store, **updates):
    with store.box.connect() as db:
        draft = interface.draft(db)
        for key, value in updates.items():
            draft["content"][key].update(value)
        interface.save(db, draft)
        return interface.draft(db, draft["id"])


def deliver(store, card, label, *, profile="webops", receipt=None):
    matches = [b for row in card["reply_markup"]["inline_keyboard"] for b in row if b["text"] == label]
    assert len(matches) == 1
    with store.box.connect() as db:
        message_id = 100 + db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    event_id = store.box.enqueue(profile=profile, job_id="test-control", kind="text",
                                 payload=card, key=f"card:{message_id}")
    store.box.finish(event_id, "delivered", receipt=receipt or [{"message_id": message_id, "chat_id": OWNER}])
    return {
        "data": matches[0]["callback_data"], "from": {"id": OWNER},
        "message": {"message_id": message_id, "chat": {"id": OWNER, "type": "private"}},
    }


def click(store, card, label):
    return interface.callback("webops", deliver(store, card, label), store=store)


def action_payload(store, action, **values):
    with store.box.connect() as db:
        version, _ = control.baseline(db)
        button = interface.button(db, "测试操作", action, values, version)
    return deliver(store, interface.card("测试卡片", [[button]]), "测试操作")


def action(store, name, **values):
    return interface.callback("webops", action_payload(store, name, **values), store=store)


def apply_preview(store, draft=None):
    preview = action(store, "preview", draft=(draft or current_draft(store))["id"])
    return click(store, preview, "确认应用")


def sample_item(*, category=CATEGORY, facts=None):
    return {"category": category, "status": "available", "name": "测试物品",
            "text": "测试物品在售", "price": 100, "attributes": {"facts": facts or {}}}


def add_post(store, source_id="1", *, item=None, history=False):
    with store.box.connect() as db:
        db.execute("""INSERT INTO marketplace_posts
            (source,source_id,url,title,body,body_complete,first_seen,updated_at,items,status,history)
            VALUES('v2ex',?,?,?,'合成正文',1,1,1,?,?,?)""",
            (source_id, "https://example.invalid/post/" + source_id, "合成商品",
             json.dumps([item or sample_item()], ensure_ascii=False),
             "historical" if history else "ready", int(history)))


def proposal(kind="光场投射器", *, parent="display", terms=None):
    return {"kind": kind, "parent": parent, "terms": terms or [kind],
            "evidence": "出售光场投射器，裸眼多视点成像。",
            "difference": "使用裸眼多视点成像，与现有普通屏幕品种不同。"}


def record_kind(store, value=None, *, source_id="11", status="available", **post_overrides):
    value = value or proposal()
    post = {"source": "v2ex", "source_id": source_id,
            "url": "https://example.invalid/post/" + source_id,
            "body_complete": True, "history": False, **post_overrides}
    with store.box.connect() as db:
        novelty.record(db, post, {"name": value["kind"], "status": status,
                                 "attributes": {"novelty": value}})
        rows = db.execute("SELECT * FROM market_novelty ORDER BY first_seen").fetchall()
        return [dict(row) for row in rows]


def test_default_profile_cannot_open_or_change_market(market):
    before = active(market)
    result = interface.command("default", store=market)
    assert "/market" in result["text"] and "库库干" in result["text"]
    assert interface.reply("default", 0, "包含 Aster", store=market) is None
    assert active(market) == before
    with market.box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM market_drafts").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM market_buttons").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    "sender", "bot", "chat", "group", "message", "message_type", "profile",
    "missing_receipt", "receipt_owner", "other_card",
])
def test_signed_callback_requires_owner_and_original_delivered_card(market, change):
    draft = current_draft(market)
    payload = action_payload(market, "toggle", draft=draft["id"], category=CATEGORY)
    assert inline.owns_callback("webops", payload, box=market.box)
    profile = "webops"
    if change == "sender":
        payload["from"]["id"] = 456
    elif change == "bot":
        payload["from"]["is_bot"] = True
    elif change == "chat":
        payload["message"]["chat"]["id"] = 456
    elif change == "group":
        payload["message"]["chat"]["type"] = "group"
    elif change == "message":
        payload["message"]["message_id"] += 1
    elif change == "message_type":
        payload["message"]["message_id"] = str(payload["message"]["message_id"])
    elif change == "profile":
        profile = "default"
    else:
        with market.box.connect() as db:
            if change == "missing_receipt":
                db.execute("UPDATE events SET receipt=NULL")
            elif change == "receipt_owner":
                db.execute("UPDATE events SET receipt=?", (json.dumps([{
                    "message_id": payload["message"]["message_id"], "chat_id": 456,
                }]),))
            else:
                db.execute("UPDATE events SET payload=?", (json.dumps(interface.card("无此按钮")),))
    assert not inline.owns_callback(profile, payload, box=market.box)
    with pytest.raises(ValueError, match="本人会话|原消息"):
        interface.callback(profile, payload, store=market)
    assert current_draft(market) == draft
    assert active(market)[0] == 1


def test_a_delivered_forged_signature_is_still_rejected(market):
    draft = current_draft(market)
    payload = action_payload(market, "toggle", draft=draft["id"], category=CATEGORY)
    assert len(payload["data"].encode()) <= 64
    original = payload["data"]
    payload["data"] = original[:-1] + ("a" if original[-1] != "a" else "b")
    with market.box.connect() as db:
        db.execute("UPDATE events SET payload=replace(payload,?,?)", (original, payload["data"]))
    assert inline.owns_callback("webops", payload, box=market.box)
    with pytest.raises(ValueError, match="签名无效"):
        interface.callback("webops", payload, store=market)
    assert current_draft(market) == draft


@pytest.mark.parametrize("receipt_list", [False, True])
def test_receipt_object_and_last_message_in_receipt_list_are_accepted(market, receipt_list):
    home = interface.command("webops", store=market)
    receipt = {"message_id": 100, "chat_id": OWNER}
    if receipt_list:
        receipt = [{"message_id": 99, "chat_id": OWNER}, receipt]
    payload = deliver(market, home, "出售筛选", receipt=receipt)
    assert inline.owns_callback("webops", payload, box=market.box)
    assert "出售筛选" in interface.callback("webops", payload, store=market)["text"]


@pytest.mark.parametrize("kind", ["toggle", "value"])
def test_repeated_selection_does_not_toggle_back(market, kind):
    draft = current_draft(market)
    values = {"draft": draft["id"], "category": CATEGORY}
    if kind == "value":
        values.update(field="brand", mode="include", value="Aster")
    payload = action_payload(market, kind, **values)
    first = interface.callback("webops", payload, store=market)
    after = current_draft(market)
    restarted = Store(Outbox(market.box.path))
    assert interface.callback("webops", payload, store=restarted) == first
    assert current_draft(restarted) == after
    if kind == "toggle":
        assert policy.selected(CATEGORY, after["content"]["sale"])
    else:
        assert after["content"]["sale"]["filters"][CATEGORY]["brand"]["include"] == ["Aster"]
    assert active(market)[1]["sale"]["categories"] == []


def test_draft_and_pending_input_survive_restart_without_changing_active_policy(market):
    baseline = active(market)
    draft = current_draft(market)
    action(market, "toggle", draft=draft["id"], category=CATEGORY)
    prompt = action(market, "input", draft=draft["id"], category=CATEGORY, field="price")
    payload = deliver(market, prompt, "取消输入")
    before = current_draft(market)
    restarted = Store(Outbox(market.box.path))
    assert current_draft(restarted) == before
    assert interface.reply("webops", 99999, "最低 10；最高 300", store=restarted) is None
    result = interface.reply("webops", payload["message"]["message_id"], "最低 10；最高 300", store=restarted)
    assert "最低 10" in result["text"] and "最高 300" in result["text"]
    draft = current_draft(restarted)
    assert draft["content"]["sale"]["filters"][CATEGORY]["price"] == {"min": 10, "max": 300}
    assert draft["revision"] == before["revision"] + 1
    assert active(restarted) == baseline
    with restarted.box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM market_inputs").fetchone()[0] == 0


def test_apply_is_explicit_atomic_and_idempotent(market):
    baseline = active(market)
    draft = replace_draft(market, sale={"categories": [CATEGORY]})
    add_post(market, history=True)
    preview = action(market, "preview", draft=draft["id"])
    assert "保留 1 件" in preview["text"]
    assert active(market) == baseline
    payload = deliver(market, preview, "确认应用")
    first = interface.callback("webops", payload, store=market)
    assert "已应用版本 2" in first["text"]
    restarted = Store(Outbox(market.box.path))
    assert interface.callback("webops", payload, store=restarted) == first
    version, content = active(restarted)
    assert version == 2 and content["sale"]["version"] == baseline[1]["sale"]["version"] + 1
    assert policy.selected(CATEGORY, policy.load())
    with restarted.box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM market_versions").fetchone()[0] == 2
        assert db.execute("SELECT status FROM market_drafts WHERE id=?", (draft["id"],)).fetchone()[0] == "applied"
        assert db.execute("SELECT status FROM marketplace_posts").fetchone()[0] == "historical"
        assert db.execute("SELECT COUNT(*) FROM events WHERE job_id!='test-control'").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["draft", "items", "updated_at", "new_post"])
def test_preview_drift_requires_a_new_preview_before_apply(market, change):
    draft = replace_draft(market, sale={"categories": [CATEGORY]})
    add_post(market)
    preview = action(market, "preview", draft=draft["id"])
    payload = deliver(market, preview, "确认应用")
    if change == "draft":
        replace_draft(market, sale={"categories": []})
    elif change == "new_post":
        add_post(market, "2")
    else:
        with market.box.connect() as db:
            if change == "items":
                db.execute("UPDATE marketplace_posts SET items=?", (json.dumps([sample_item(facts={"brand": "Helio"})]),))
            else:
                db.execute("UPDATE marketplace_posts SET updated_at=updated_at+1")
    result = interface.callback("webops", payload, store=market)
    assert "已变化" in result["text"]
    assert active(market)[0] == 1
    assert current_draft(market)["status"] == "draft"
    updated = click(market, result, "重新预览")
    assert "已应用版本 2" in click(market, updated, "确认应用")["text"]


def test_old_version_card_cannot_overwrite_new_policy(market):
    draft = replace_draft(market, sale={"categories": [CATEGORY]})
    old = action_payload(market, "group_set", draft=draft["id"], group="phone", mode="block")
    apply_preview(market, draft)
    before = active(market)
    assert "策略已更新" in interface.callback("webops", old, store=market)["text"]
    assert active(market) == before


def test_rollback_previews_and_commits_a_new_version(market):
    original = active(market)[1]
    assert "没有可恢复" in action(market, "rollback_preview")["text"]
    draft = replace_draft(market, sale={"categories": [CATEGORY]}, wanted={
        "enabled": True, "targets": [{"category": CATEGORY, "model": "iPhone 15 Pro"}],
    })
    apply_preview(market, draft)
    before = active(market)
    preview = action(market, "rollback_preview")
    assert active(market) == before
    assert "已应用版本 3" in click(market, preview, "确认应用")["text"]
    version, restored = active(market)
    assert version == 3
    for key in ("sale", "wanted"):
        assert {k: v for k, v in restored[key].items() if k != "version"} == {
            k: v for k, v in original[key].items() if k != "version"
        }
        assert restored[key]["version"] == before[1][key]["version"] + 1
    with market.box.connect() as db:
        assert [tuple(row) for row in db.execute("SELECT id,parent FROM market_versions ORDER BY id")] == [(1, None), (2, 1), (3, 2)]


def test_novelty_requires_a_genuine_kind_and_source_quote():
    value = proposal()
    assert novelty.validate_suggestion(value, "unknown", value["evidence"]) == value
    for invalid, category in ((value, CATEGORY), ({**value, "kind": "光场投射器二代2"}, "unknown"),
                              ({**value, "evidence": "原文没有这句话"}, "unknown"),
                              ({**value, "terms": ["不在原文"]}, "unknown")):
        with pytest.raises(AnalysisError):
            novelty.validate_suggestion(invalid, category, value["evidence"])


def test_kind_review_deduplicates_synonyms_and_repeated_posts(market):
    rows = record_kind(market)
    identity = rows[0]["id"]
    record_kind(market)
    synonym = proposal("裸眼光场装置", terms=["光场投射器"])
    rows = record_kind(market, synonym, source_id="12")
    assert len(rows) == 1 and rows[0]["id"] == identity
    assert [post["id"] for post in json.loads(rows[0]["posts"])] == ["11", "12"]


def test_blocked_novelty_parent_is_marked_announced_without_review_event(market):
    row = record_kind(market)[0]
    draft = replace_draft(market, sale={"blocked_groups": ["display"]})
    apply_preview(market, draft)

    novelty.main()

    with market.box.connect() as db:
        stored = db.execute("SELECT announced FROM market_novelty WHERE id=?", (row["id"],)).fetchone()
        review_events = db.execute(
            "SELECT COUNT(*) FROM events WHERE job_id='marketplace-review'"
        ).fetchone()[0]
    assert stored[0] == 1
    assert review_events == 0


@pytest.mark.parametrize("post_changes", [{"history": True}, {"body_complete": False}])
def test_history_and_incomplete_bodies_do_not_create_novelty_reviews(market, post_changes):
    assert record_kind(market, **post_changes) == []


@pytest.mark.parametrize("mode", ["ignored", "later", "classify", "follow"])
def test_novelty_decision_is_draft_only_until_apply_and_repeated_click_is_idempotent(market, mode):
    row = record_kind(market)[0]
    draft = current_draft(market)
    payload = action_payload(market, "novel_action", draft=draft["id"], id=row["id"], mode=mode)
    first = interface.callback("webops", payload, store=market)
    after = current_draft(market)
    assert interface.callback("webops", payload, store=market) == first
    assert current_draft(market) == after
    with market.box.connect() as db:
        assert db.execute("SELECT status FROM market_novelty").fetchone()[0] == "pending"
    assert active(market)[1]["taxonomy"] == {}
    assert classifier.category_for("光场投射器")[0] == "unknown"
    apply_preview(market, after)
    rows = record_kind(market, source_id="13")
    assert len(rows) == 1
    assert rows[0]["status"] == ("classified" if mode in {"classify", "follow"} else mode)
    assert (classifier.category_for("光场投射器")[0].startswith("display.ext_")
            if mode in {"classify", "follow"} else classifier.category_for("光场投射器")[0] == "unknown")


def test_classify_without_follow_stays_excluded_even_under_selected_parent(market):
    draft = replace_draft(market, sale={"groups": ["display"]})
    apply_preview(market, draft)
    row = record_kind(market)[0]
    draft = current_draft(market)
    action(market, "novel_action", draft=draft["id"], id=row["id"], mode="classify")
    assert classifier.category_for("光场投射器")[0] == "unknown"
    apply_preview(market)
    restarted = Store(Outbox(market.box.path))
    category = classifier.category_for("光场投射器")[0]
    assert category.startswith("display.ext_")
    assert not policy.selected(category, active(restarted)[1]["sale"])
    assert policy.decision(sample_item(category=category), policy.load())[0] == "exclude"


def test_parent_block_cannot_be_bypassed_by_following_a_new_kind(market):
    draft = replace_draft(market, sale={"blocked_groups": ["display"]}, wanted={"blocked_groups": ["display"]})
    apply_preview(market, draft)
    row = record_kind(market)[0]
    assert row["announced"] == 1
    action(market, "novel_action", draft=current_draft(market)["id"], id=row["id"], mode="follow")
    apply_preview(market)
    category = classifier.category_for("光场投射器")[0]
    sale = active(market)[1]["sale"]
    assert category in sale["categories"]
    assert not policy.selected(category, sale)
    assert policy.decision(sample_item(category=category), sale) == ("exclude", ["大类已屏蔽"])


def test_rollback_apply_removes_extension_from_live_classifier(market):
    row = record_kind(market)[0]
    draft = current_draft(market)
    action(market, "novel_action", draft=draft["id"], id=row["id"], mode="classify")
    apply_preview(market)
    assert classifier.category_for("光场投射器")[0].startswith("display.ext_")
    rollback = action(market, "rollback_preview")
    assert classifier.category_for("光场投射器")[0].startswith("display.ext_")
    click(market, rollback, "确认应用")
    assert classifier.category_for("光场投射器")[0] == "unknown"


def test_daily_review_announces_new_kinds_once_and_is_silent_without_additions(market, capsys):
    assert novelty.main() == 0
    assert market.box.counts() == {}
    record_kind(market)
    assert novelty.main() == 0
    restarted = Store(Outbox(market.box.path))
    record_kind(restarted, source_id="12")
    assert novelty.main() == 0
    with restarted.box.connect() as db:
        events = db.execute("SELECT profile,job_id,payload FROM events").fetchall()
        assert len(events) == 1
        assert tuple(events[0][:2]) == ("webops", "marketplace-review")
        assert "1 种" in json.loads(events[0]["payload"])["text"]
        assert db.execute("SELECT announced FROM market_novelty").fetchone()[0] == 1
    record_kind(restarted, proposal("触觉反馈织物", parent="life"), source_id="14")
    assert novelty.main() == 0
    with restarted.box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
    assert capsys.readouterr().out.splitlines() == ['{"wakeAgent":false}'] * 4


def test_review_announcement_and_marker_commit_or_rollback_together(market):
    record_kind(market)
    with market.box.connect() as db:
        db.execute("""CREATE TRIGGER fail_review BEFORE UPDATE OF announced ON market_novelty
            WHEN NEW.announced=1 BEGIN SELECT RAISE(ABORT,'fixture failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="fixture failure"):
        novelty.main()
    assert market.box.counts() == {}
    with market.box.connect() as db:
        assert db.execute("SELECT announced FROM market_novelty").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM market_buttons").fetchone()[0] == 0
