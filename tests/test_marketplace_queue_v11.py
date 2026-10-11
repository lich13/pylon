"""Marketplace queue boundaries with source evidence and a structured provider stub."""

import copy
import json
import socket
from types import SimpleNamespace

import pytest

from pylon.outbox import Outbox
from pylon.tasks.marketplace import classifier, enrich, pipeline, wanted
from pylon.tasks.marketplace.store import Store
from pylon.tasks.software import monitor


CATEGORY = "phone.smartphone"


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=2_000_000_000.0)
    monkeypatch.setattr(enrich.time, "time", lambda: clock.now)
    return clock


@pytest.fixture
def market(tmp_path, monkeypatch, clock):
    monkeypatch.setenv("PYLON_HOME", str(tmp_path))
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "queue.sqlite3"))
    monkeypatch.setattr(enrich, "model_name", lambda: "fixture-model")
    monkeypatch.setattr("pylon.tasks.quotes.health.inspect", lambda *_: {"available": False})

    def forbidden(*_args, **_kwargs):
        pytest.fail("queue fixtures must not make network requests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(monitor, "_request_grok_structured", forbidden)
    return Store(Outbox(tmp_path / "queue.sqlite3"))


def sales(filters=None, *, version=1):
    return {"version": version, "enabled": True, "categories": [CATEGORY],
            "filters": {CATEGORY: filters or {}}}


def requests(filters=None, *, enabled=False):
    return wanted.validate({
        "version": 1, "enabled": enabled,
        "targets": [{"category": CATEGORY, "model": "iPhone 15 Pro"}],
        "filters": {CATEGORY: filters or {}},
    })


def process(store, sale=None, request=None):
    return pipeline.process(store, current_policy=sales() if sale is None else sale,
                            wanted_policy=requests() if request is None else request)


def ingest(store, title, body, *, source="v2ex", source_id="42", platform_category=""):
    assert store.ingest(source, [{
        "id": source_id, "url": "https://example.invalid/post/" + source_id,
        "title": title, "body_full": body, "body_complete": True,
        "platform_category": platform_category,
    }]) == 1


def rows(store, query, params=()):
    with store.box.connect() as db:
        return [dict(row) for row in db.execute(query, params).fetchall()]


def post(store, source_id="42"):
    return rows(store, "SELECT * FROM marketplace_posts WHERE source_id=?", (source_id,))[0]


def analyses(store):
    return rows(store, "SELECT * FROM marketplace_analyses ORDER BY updated_at,fingerprint")


def events(store):
    return rows(store, "SELECT * FROM events ORDER BY id")


def provider(monkeypatch, *responses):
    calls = []
    responses = iter(responses)

    def request(prompt, *, schema, name, **_options):
        calls.append(prompt)
        assert name == "marketplace_items"
        assert "category_evidence" in schema["properties"]["items"]["items"]["required"]
        try:
            value = next(responses)
        except StopIteration:
            pytest.fail("unexpected extra provider request")
        if isinstance(value, BaseException):
            raise value
        return ({"status": "completed", "output": [{"type": "message", "content": [
            {"type": "output_text", "text": json.dumps(value, ensure_ascii=False)},
        ]}]}, "fixture-request-" + str(len(calls)))

    monkeypatch.setattr(monitor, "_request_grok_structured", request)
    return calls


def item(name, category, line, price, *, status="available", attributes=None, novelty=None):
    return {
        "name": name, "category": category, "start_line": line, "end_line": line,
        "price": price, "price_line": line, "status": status, "link_ids": [],
        "category_evidence": [name], "category_reason": "原文明确出售该主体。",
        "attributes": attributes or [], "novelty": novelty,
    }


def table(store):
    ingest(store, "闲置出售", "商品 | 名称 | 售价 |\n"
           "手机 | iPhone 15 Pro 512G | 4498元 | 成色极好\n可邮寄。")
    return {"items": [item("iPhone 15 Pro 512G", CATEGORY, 2, 4498)],
            "common_lines": [3], "shared_link_ids": [], "ignored_lines": [1]}


def next_attempt(store, clock):
    pending = [row for row in analyses(store) if row["status"] == "pending"]
    assert len(pending) == 1
    assert pending[0]["next_attempt"] > clock.now
    clock.now = pending[0]["next_attempt"] + 1


@pytest.mark.parametrize("source", ["v2ex", "chiphell", "nga"])
def test_store_platform_category_survives_pipeline_reclassification(market, source):
    ingest(market, "出 澜星 Q8 Ultra 12+512G",
           "自用整机，镜头膜一直贴着，机身无磕碰，售价4498元，可邮寄。",
           source=source, platform_category="手机")
    stored = post(market)
    assert stored["platform_category"] == "手机"
    items = json.loads(stored["items"])
    assert [(entry["category"], entry["price"]) for entry in items] == [(CATEGORY, 4498)]
    # Simulate a durable row awaiting reclassification after a parser upgrade.
    items[0]["attributes"]["classifier_version"] = "fixture-previous-version"
    with market.box.connect() as db:
        db.execute("UPDATE marketplace_posts SET items=?", (json.dumps(items),))

    assert process(market) == 1

    stored = post(market)
    current = json.loads(stored["items"])[0]
    assert stored["status"] == "queued"
    assert current["category"] == CATEGORY
    assert current["price"] == 4498
    assert current["attributes"]["platform_category"] == "手机"
    assert current["attributes"]["classifier_version"] == classifier.VERSION
    assert analyses(market) == []
    assert len(events(market)) == 1


def test_known_sale_notifies_once_while_separate_unknown_finishes_analysis(market, monkeypatch):
    ingest(market, "两件闲置分别出售",
           "1. 出 iPhone 15 Pro 512G，镜头膜一直贴着，售价4498元，可邮寄。\n"
           "2. 出自制旋纹摆件，售价80元，可邮寄。")
    initial = json.loads(post(market)["items"])
    assert [(entry["category"], entry["price"]) for entry in initial] == [
        (CATEGORY, 4498), ("unknown", 80),
    ]
    novelty = {"kind": "旋纹摆件", "parent": "life", "difference": "供桌面静态陈列的手工旋纹装饰物",
               "evidence": "出自制旋纹摆件", "terms": ["旋纹摆件"]}
    calls = provider(monkeypatch, {
        "items": [item("iPhone 15 Pro 512G", CATEGORY, 1, 4498),
                  item("自制旋纹摆件", "unknown", 2, 80, novelty=novelty)],
        "common_lines": [], "shared_link_ids": [], "ignored_lines": [],
    })

    assert process(market) == 1
    first = events(market)
    message = json.loads(first[0]["payload"])["text"]
    assert "4498 元" in message and "旋纹摆件" not in message and "80 元" not in message
    assert post(market)["status"] == "queued"
    assert analyses(market)[0]["status"] == "pending"
    assert len(json.loads(post(market)["items"])) == 2
    assert process(market) == 0

    restarted = Store(Outbox(market.box.path))
    assert enrich.run_pending(restarted) == 1
    assert analyses(restarted)[0]["status"] == "complete"
    assert post(restarted)["status"] == "queued"
    assert process(restarted) == 0
    assert enrich.run_pending(restarted) == 0
    assert events(restarted) == first
    assert len(calls) == 1
    proposals = rows(restarted, "SELECT * FROM market_novelty")
    assert len(proposals) == 1 and proposals[0]["kind"] == "旋纹摆件"
    assert proposals[0]["status"] == "pending"
    assert json.loads(proposals[0]["posts"])[0]["id"] == "42"
    final = json.loads(post(restarted)["items"])
    assert [(entry["category"], entry["price"]) for entry in final] == [(CATEGORY, 4498), ("unknown", 80)]


@pytest.mark.parametrize("corrected", [True, False], ids=["correction-succeeds", "second-semantic-failure"])
def test_semantic_error_gets_one_reasoned_correction_then_finishes(market, monkeypatch, clock, corrected):
    valid = table(market)
    invalid = copy.deepcopy(valid)
    invalid["items"][0]["price"] = 9999
    calls = provider(monkeypatch, invalid, valid if corrected else invalid)
    assert process(market) == 0

    assert enrich.run_pending(market) == 0
    first = analyses(market)[0]
    assert (first["status"], first["attempts"], first["semantic_failures"], first["network_failures"]) == (
        "pending", 1, 1, 0,
    )
    assert first["error"] == "AnalysisError: price_without_evidence"
    assert post(market)["status"] == "pending_analysis"
    assert enrich.run_pending(market) == 0
    assert len(calls) == 1
    next_attempt(market, clock)

    assert enrich.run_pending(market) == int(corrected)
    assert "本轮校验修正要求：无。" in calls[0]
    assert "本轮校验修正要求：AnalysisError: price_without_evidence。" in calls[1]
    final = analyses(market)[0]
    assert final["attempts"] == 2 and final["network_failures"] == 0
    if corrected:
        assert final["status"] == "complete" and final["semantic_failures"] == 1
        assert post(market)["status"] == "ready"
        assert process(market) == 1
        assert "4498 元" in json.loads(events(market)[0]["payload"])["text"]
    else:
        assert final["status"] == "error" and final["semantic_failures"] == 2
        assert post(market)["status"] == "needs_review"
        reviews = rows(market, "SELECT * FROM market_reviews WHERE status='pending' AND item_index=-1")
        assert len(reviews) == 1 and "AnalysisError: price_without_evidence" in reviews[0]["reason"]
        assert process(market) == 0 and events(market) == []
    clock.now += 10000
    assert enrich.run_pending(market) == 0
    assert len(calls) == 2


def test_attempt_budget_is_three_even_after_a_semantic_failure(market, monkeypatch, clock):
    invalid = table(market)
    invalid["items"][0]["price"] = 9999
    private_message = "fixture transport diagnostic must not be persisted"
    calls = provider(monkeypatch, invalid, *[TimeoutError(private_message) for _ in range(2)])
    assert process(market) == 0
    assert enrich.run_pending(market) == 0
    assert analyses(market)[0]["semantic_failures"] == 1

    for number in range(1, 3):
        next_attempt(market, clock)
        assert enrich.run_pending(market) == 0
        job = analyses(market)[0]
        assert job["network_failures"] == number and job["semantic_failures"] == 1
        assert job["attempts"] == number + 1
        assert job["status"] == ("error" if number == 2 else "pending")
        assert job["error"] == "TimeoutError"
        assert post(market)["status"] == ("needs_review" if number == 2 else "pending_analysis")
    reviews = rows(market, "SELECT * FROM market_reviews WHERE status='pending' AND item_index=-1")
    assert len(reviews) == 1 and "TimeoutError" in reviews[0]["reason"]
    assert private_message not in json.dumps([analyses(market), post(market), reviews])
    clock.now += 10000
    assert enrich.run_pending(market) == 0 and process(market) == 0
    assert len(calls) == 3 and events(market) == []


@pytest.mark.parametrize("require_condition,provided", [(False, False), (True, True), (True, False)],
                         ids=["unrestricted-missing-field", "fills-only-missing-field", "still-missing-review"])
def test_cached_analysis_rechecks_new_required_fields(market, monkeypatch, require_condition, provided):
    valid = table(market)
    completed = copy.deepcopy(valid)
    if provided:
        completed["items"][0]["attributes"] = [{"field": "condition", "value": "极好", "evidence": "成色极好"}]
    calls = provider(monkeypatch, valid, completed)
    original_policy = sales({"price": {"min": 4000, "max": 5000}, "storage_gb": {"include": [512]}})
    assert process(market, original_policy) == 0
    assert enrich.run_pending(market) == 1
    cached = analyses(market)[0]
    assert cached["status"] == "complete" and json.loads(cached["requirements"]) == []
    cached_item = json.loads(post(market)["items"])[0]
    assert "condition" not in cached_item["attributes"]["facts"]
    assert cached_item["attributes"]["facts"]["storage_gb"] == 512
    assert post(market)["status"] == "ready"

    active_policy = copy.deepcopy(original_policy)
    active_policy["version"] = 2
    if require_condition:
        active_policy["filters"][CATEGORY]["condition"] = {"include": ["极好"]}
        assert process(market, active_policy) == 0
        jobs = analyses(market)
        assert len(jobs) == 2
        supplement = next(job for job in jobs if job["status"] == "pending")
        assert supplement["fingerprint"] != cached["fingerprint"]
        assert json.loads(supplement["requirements"]) == ["condition"]
        assert post(market)["status"] == "pending_analysis" and events(market) == []
        assert enrich.run_pending(market) == 1
        assert "需要从原文补全的筛选属性：['condition']。" in calls[1]
        assert json.loads(post(market)["items"])[0]["attributes"]["analysis_fields"] == ["condition"]

    matched = not require_condition or provided
    assert process(market, active_policy) == int(matched)
    assert post(market)["status"] == ("queued" if matched else "needs_review")
    assert len(events(market)) == int(matched)
    assert len(calls) == (2 if require_condition else 1)
    assert len(analyses(market)) == (2 if require_condition else 1)
    if not matched:
        reviews = rows(market, "SELECT * FROM market_reviews WHERE status='pending' AND item_index=0")
        assert len(reviews) == 1 and reviews[0]["reason"] == "condition"
    assert enrich.run_pending(market) == 0
    assert process(market, active_policy) == 0


@pytest.mark.parametrize("interruptions", [1, 3], ids=["resumes-interrupted-read", "repeated-interruption-review"])
def test_interrupted_analysis_resumes_or_enters_review(market, monkeypatch, clock, interruptions):
    valid = table(market)
    responses = [KeyboardInterrupt() for _ in range(interruptions)]
    if interruptions == 1:
        responses.append(valid)
    calls = provider(monkeypatch, *responses)
    assert process(market) == 0
    for attempt in range(1, interruptions + 1):
        with pytest.raises(KeyboardInterrupt):
            enrich.run_pending(market)
        current = analyses(market)[0]
        assert current["status"] == "running" and current["attempts"] == attempt
        assert post(market)["status"] == "pending_analysis"
        clock.now += 181

    restarted = Store(Outbox(market.box.path))
    assert enrich.run_pending(restarted) == int(interruptions == 1)
    final = analyses(restarted)[0]
    if interruptions == 1:
        assert final["status"] == "complete" and final["attempts"] == 2
        assert post(restarted)["status"] == "ready"
        assert process(restarted) == 1
    else:
        assert final["status"] == "error" and final["attempts"] == 3
        assert final["error"] == "interrupted_read_only_analysis"
        assert post(restarted)["status"] == "needs_review"
        reviews = rows(restarted, "SELECT * FROM market_reviews WHERE status='pending' AND item_index=-1")
        assert len(reviews) == 1 and "多次中断" in reviews[0]["reason"]
        assert process(restarted) == 0 and events(restarted) == []
    assert enrich.run_pending(restarted) == 0
    assert len(calls) == (2 if interruptions == 1 else 3)


@pytest.mark.parametrize("excluded_scope", [None, "sale", "wanted"])
def test_pipeline_applies_sale_and_wanted_price_filters_independently(market, excluded_scope):
    ingest(market, "出售 iPhone 15 Pro", "自用，售价4498元，可邮寄。", source_id="42")
    ingest(market, "求购 iPhone 15 Pro", "预算3500元，可邮寄。", source_id="43")
    sale = sales({"price": {"min": 4000, "max": 5000}})
    request = requests({"price": {"min": 3000, "max": 4000}}, enabled=True)
    if excluded_scope == "sale":
        sale["filters"][CATEGORY]["price"] = {"max": 4000}
    elif excluded_scope == "wanted":
        request["filters"][CATEGORY]["price"] = {"min": 4000}

    expected = 2 if excluded_scope is None else 1
    assert process(market, sale, request) == expected
    assert len(events(market)) == expected
    assert analyses(market) == []
    for source_id, scope, price, intent in [("42", "sale", 4498, "sale"), ("43", "wanted", 3500, "wanted")]:
        stored = post(market, source_id)
        parsed = json.loads(stored["items"])[0]
        assert parsed["price"] == price and parsed["attributes"]["intent"] == intent
        matched = scope != excluded_scope
        assert stored["status"] == ("queued" if matched else "skipped")
        assert json.loads(stored["decision"])["items"][0]["state"] == ("match" if matched else "exclude")
        assert stored["policy_version"] == sale["version"]
        assert stored["wanted_policy_version"] == request["version"]
    assert process(market, sale, request) == 0
