"""A known benefit may have unknown eligibility; its amount must remain grounded."""
import copy

import pytest

from pylon.tasks.linuxdo import judge, policy


def case(category="I2", body="平台搜索10元话费，银行卡支付，部分账号有立减。"):
    config = policy.default_policy()
    config.update(groups=[], categories=[category], excluded_categories=[])
    snapshot = {
        "topic_id": "fixture", "fingerprint": "fixture-content", "title": "合成线上活动",
        "lead_mode": True, "notifications": [], "previously_notified": False,
        "evidence": [{"ref": "fixture:1", "origin": "telegram", "post_number": 1,
                      "url": "https://example.invalid/topic", "title": "合成线上活动",
                      "body": body, "links": [], "complete": False}],
    }
    result = {
        "decision": "push", "categories": [category], "content_type": "offer",
        "reason": "原帖称部分账号银行卡支付可立减。", "action": "支付前核对是否出现减免。",
        "missing_evidence": ["具体减免额", "命中账号条件", "适用地区"],
        "expires_at": "", "change_kind": "initial", "provider": "", "region_scope": "unknown",
        "facts": [{"category": category, "kind": "method", "subject": "线上话费活动",
                   "value": body, "evidence": [{"ref": "fixture:1", "quote": body}]}],
    }
    return snapshot, config, result


def test_conditional_online_saving_keeps_unknown_details_without_suppressing_value():
    snapshot, config, result = case()
    checked = judge.validate_result(result, snapshot, config)
    assert checked["decision"] == "push"
    assert checked["missing_evidence"] == result["missing_evidence"]
    assert checked["region_scope"] == "unknown"
    assert checked["facts"][0]["value"] == snapshot["evidence"][0]["body"]


def test_topup_face_value_cannot_be_invented_as_saving_amount():
    snapshot, config, result = case()
    result["facts"][0]["kind"] = "price"
    result["facts"][0]["value"] = "立减10元"
    with pytest.raises(judge.JudgeError, match="fact value absent"):
        judge.validate_result(result, snapshot, config)


def test_unknown_model_identity_still_holds_even_when_model_says_push():
    snapshot, config, result = case("A1", "未知模型X以0倍率开放，期限未说明。")
    result["facts"][0].update(kind="resource", subject="未知模型X")
    result["missing_evidence"] = ["模型归属"]
    checked = judge.validate_result(result, snapshot, config)
    assert checked["decision"] == "hold"
    assert checked["reason"] == "目标模型资源缺少原文依据"


def test_new_prompt_does_not_reuse_old_value_judgement_cache(monkeypatch):
    snapshot, config, _ = case()
    current = judge.cache_key(snapshot, config, "fixture-model")
    assert current == judge.cache_key(copy.deepcopy(snapshot), copy.deepcopy(config), "fixture-model")
    monkeypatch.setattr(judge, "PROMPT_VERSION", "intelligence-value-v4-evidenced-purpose")
    assert current != judge.cache_key(snapshot, config, "fixture-model")
