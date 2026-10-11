from __future__ import annotations

import copy
import json

import pytest

from pylon.outbox import Outbox
from pylon.shared.model_requests import emit
from pylon.tasks.intelligence import attempts, pipeline
from pylon.tasks.intelligence.store import Store
from pylon.tasks.linuxdo import trade


def intelligence_item(source="v2ex", source_id="fixture", fingerprint="fixture-fingerprint"):
    return {"source": source, "source_id": source_id, "fingerprint": fingerprint}


def test_store_initialization_creates_attempt_ledger_and_semantic_errors_allow_one_correction(
    tmp_path, monkeypatch
):
    box = Outbox(tmp_path / "attempts.sqlite3")
    intel_store = Store(box)
    item = intelligence_item()
    clock = [1000.0]
    monkeypatch.setattr(attempts.time, "time", lambda: clock[0])
    requests = []

    def malformed_request():
        requests.append(clock[0])
        emit("start", request_id=f"fixture-request-{len(requests)}")
        raise ValueError("invalid response https://example.invalid/private")

    with pytest.raises(ValueError, match="invalid response"):
        attempts.call(intel_store, item, "value", malformed_request, lambda value: value)

    with box.connect() as db:
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('intel_attempts','intel_retry_rounds')"
            )
        }
        first = attempts.history(db, item, "value")[0]
    assert tables == {"intel_attempts", "intel_retry_rounds"}
    assert first["number"] == 1
    assert first["round"] == 0
    assert first["status"] == "failed"
    assert first["semantic"] == 1
    assert first["request_id"] == "fixture-request-1"
    assert "https://example.invalid" not in first["error"]
    assert "[url]" in first["error"]
    assert attempts.correction(intel_store, item, "value").startswith("ValueError:")

    clock[0] = 1061.0
    with pytest.raises(ValueError):
        attempts.call(intel_store, item, "value", malformed_request, lambda value: value)
    assert len(requests) == 2

    clock[0] = 1362.0
    with pytest.raises(attempts.BudgetError, match="已用尽") as exhausted:
        attempts.call(intel_store, item, "value", malformed_request, lambda value: value)
    assert exhausted.value.next_attempt == 0
    assert len(requests) == 2
    with box.connect() as db:
        rows = attempts.history(db, item, "value")
    assert [row["number"] for row in rows] == [1, 2]
    assert sum(row["semantic"] for row in rows) == 2


def test_timeout_records_request_metadata_and_second_phase_has_an_independent_budget(
    tmp_path, monkeypatch
):
    box = Outbox(tmp_path / "timeout.sqlite3")
    intel_store = Store(box)
    item = intelligence_item(source_id="timeout")
    phase = "trade_extraction"
    clock = [2000.0]
    monkeypatch.setattr(attempts.time, "time", lambda: clock[0])

    def timeout_request():
        emit("start", request_id="fixture-timeout")
        raise TimeoutError("request timed out at https://example.invalid/secret")

    with pytest.raises(TimeoutError):
        attempts.call(intel_store, item, phase, timeout_request, lambda value: value)
    with box.connect() as db:
        first = attempts.history(db, item, phase)[0]
    assert first["semantic"] == 0
    assert first["request_id"] == "fixture-timeout"
    assert first["elapsed"] >= 0
    assert "https://example.invalid" not in first["error"]

    clock[0] = 2061.0
    assert attempts.call(
        intel_store,
        item,
        phase,
        lambda: {"phase": phase},
        lambda value: value,
    ) == {"phase": phase}

    other_phase = "value"
    assert attempts.call(
        intel_store,
        item,
        other_phase,
        lambda: {"phase": other_phase},
        lambda value: value,
    ) == {"phase": other_phase}
    with box.connect() as db:
        assert [row["number"] for row in attempts.history(db, item, phase)] == [1, 2]
        assert [row["number"] for row in attempts.history(db, item, other_phase)] == [1]


def test_restarted_started_attempt_counts_and_rounds_are_explicit(tmp_path, monkeypatch):
    box = Outbox(tmp_path / "restarted.sqlite3")
    intel_store = Store(box)
    item = intelligence_item(source_id="restarted")
    clock = [1000.0]
    monkeypatch.setattr(attempts.time, "time", lambda: clock[0])

    with box.connect() as db:
        db.execute(
            """INSERT INTO intel_attempts
               (id,source,source_id,fingerprint,phase,number,status,started_at,round)
               VALUES(?,?,?,?,?,?,'started',?,?)""",
            ("started-fixture", item["source"], item["source_id"], item["fingerprint"], "value", 1, 1000.0, 0),
        )

    clock[0] = 1061.0
    assert attempts.call(
        intel_store,
        item,
        "value",
        lambda: {"ok": True},
        lambda value: value,
    ) == {"ok": True}

    with box.connect() as db:
        rows = attempts.history(db, item, "value")
    assert [row["number"] for row in rows] == [1, 2]
    assert [row["round"] for row in rows] == [0, 0]
    assert rows[0]["status"] == "started"
    assert rows[1]["status"] == "success"


def test_explicit_retry_round_resets_attempt_number_without_erasing_history(tmp_path, monkeypatch):
    box = Outbox(tmp_path / "retry-round.sqlite3")
    intel_store = Store(box)
    source_id = "retry-round"
    intel_store.ingest(
        "v2ex",
        source_id,
        title="Retry round fixture",
        body="A synthetic retry round body retained in the public evidence ledger.",
        url=f"https://example.invalid/intel/{source_id}",
        context={"route": "candidate", "first_seen": "2026-10-11T10:00:00+08:00"},
    )
    with box.connect() as db:
        row = db.execute(
            "SELECT source,source_id,fingerprint,phase FROM intel_items WHERE source_id=?", (source_id,)
        ).fetchone()
        db.execute(attempts.SCHEMA)
        db.execute(
            """INSERT INTO intel_attempts
               (id,source,source_id,fingerprint,phase,number,status,started_at,finished_at,error,semantic,round)
               VALUES(?,?,?,?,?,?,'failed',?,?,?,?,?)""",
            (
                "old-round-attempt",
                row["source"],
                row["source_id"],
                row["fingerprint"],
                "value",
                1,
                1000.0,
                1060.0,
                "ValueError: old round fixture",
                1,
                0,
            ),
        )
        db.execute(
            "UPDATE intel_items SET status='failed',phase='value',observed_at=? WHERE source_id=?",
            (attempts.time.time(), source_id),
        )
        attempts.retry(db, "v2ex", source_id)
        retry_row = db.execute(
            "SELECT round FROM intel_retry_rounds WHERE source_id=?", (source_id,)
        ).fetchone()
        item = dict(row)

    assert retry_row["round"] == 1
    monkeypatch.setattr(attempts.time, "time", lambda: 5000.0)
    assert attempts.call(
        intel_store,
        item,
        "value",
        lambda: {"ok": True},
        lambda value: value,
    ) == {"ok": True}
    with box.connect() as db:
        rows = db.execute(
            "SELECT number,round,status FROM intel_attempts WHERE source_id=? ORDER BY round,number",
            (source_id,),
        ).fetchall()
        current = attempts.history(db, item, "value")
    assert [(row["number"], row["round"], row["status"]) for row in rows] == [
        (1, 0, "failed"),
        (1, 1, "success"),
    ]
    assert [(row["number"], row["round"], row["status"]) for row in current] == [(1, 1, "success")]


def test_exhausted_model_failure_clears_result_keeps_review_ledger_and_is_not_backlog(
    tmp_path, monkeypatch
):
    box = Outbox(tmp_path / "pipeline-budget.sqlite3")
    intel_store = Store(box)
    source_id = "pipeline-budget"
    intel_store.ingest(
        "v2ex",
        source_id,
        title="Synthetic intelligence fixture",
        body="A synthetic public body for a model failure contract.",
        url=f"https://example.invalid/intel/{source_id}",
        context={"route": "candidate", "first_seen": "2026-10-10T10:00:00+08:00"},
    )
    clock = [1000.0]
    monkeypatch.setattr(attempts.time, "time", lambda: clock[0])
    monkeypatch.setattr(pipeline.time, "time", lambda: clock[0])
    import pylon.tasks.intelligence.store as intelligence_store_module

    monkeypatch.setattr(intelligence_store_module.time, "time", lambda: clock[0])

    def model_failure(_snapshot, _focus):
        raise ValueError("synthetic schema mismatch https://example.invalid/model")

    item = intel_store.claim("v2ex", limit=1)[0]
    first = pipeline.process_item(intel_store, item, value_request=model_failure)
    assert first["exhausted"] is False
    clock[0] = 1061.0
    item = intel_store.claim("v2ex", limit=1)[0]
    second = pipeline.process_item(intel_store, item, value_request=model_failure)
    assert second["exhausted"] is True

    with box.connect() as db:
        current = db.execute(
            "SELECT status,result,phase,attempts,semantic_attempts FROM intel_items WHERE source_id=?",
            (source_id,),
        ).fetchone()
        attempt_rows = db.execute(
            "SELECT number,status,semantic FROM intel_attempts WHERE source_id=? ORDER BY number",
            (source_id,),
        ).fetchall()
        ledger = db.execute(
            "SELECT status FROM review_decisions WHERE entry_id=(SELECT entry_id FROM review_origins WHERE source_id=?) ORDER BY id",
            (source_id,),
        ).fetchall()
    assert current["status"] == "failed"
    assert current["result"] is None
    assert current["phase"] == "value"
    assert current["attempts"] == 2
    assert current["semantic_attempts"] == 2
    assert [(row["number"], row["status"], row["semantic"]) for row in attempt_rows] == [
        (1, "failed", 1),
        (2, "failed", 1),
    ]
    assert [row["status"] for row in ledger] == ["pending", "pending", "failed"]

    pipeline.review_source(intel_store, "v2ex", limit=1)
    with box.connect() as db:
        health_rows = [
            dict(row)
            for row in db.execute(
                "SELECT component,backlog,open FROM intel_health WHERE component LIKE 'v2ex:%' ORDER BY component"
            )
        ]
        alerts = db.execute(
            "SELECT count(*) FROM events WHERE job_id='intelligence-review'"
        ).fetchone()[0]
    assert {row["component"] for row in health_rows} == {
        "v2ex:analysis",
        "v2ex:trade_extraction",
        "v2ex:value",
    }
    assert all(row["backlog"] == 0 and row["open"] == 0 for row in health_rows)
    assert alerts == 0


def test_failed_new_request_cannot_reuse_previous_success_result(tmp_path):
    box = Outbox(tmp_path / "stale-result.sqlite3")
    intel_store = Store(box)
    source_id = "stale-result"
    intel_store.ingest(
        "v2ex",
        source_id,
        title="Synthetic first revision",
        body="The first synthetic revision has a complete public body.",
        url=f"https://example.invalid/intel/{source_id}",
        context={"route": "candidate", "first_seen": "2026-10-11T10:00:00+08:00"},
    )

    def old_success(_snapshot, _focus):
        return {
            "decision": "skip",
            "categories": [],
            "content_type": "chatter",
            "reason": "previous synthetic decision",
            "action": "",
            "missing_evidence": [],
            "expires_at": "",
            "change_kind": "initial",
            "provider": "",
            "region_scope": "unknown",
            "facts": [],
        }

    item = intel_store.claim("v2ex", limit=1)[0]
    first = pipeline.process_item(intel_store, item, value_request=old_success)
    assert first["decision"] == "skip"
    with box.connect() as db:
        old = db.execute(
            "SELECT result FROM intel_items WHERE source_id=?", (source_id,)
        ).fetchone()[0]
    old_result = json.loads(old)
    assert old_result["reason"]

    intel_store.ingest(
        "v2ex",
        source_id,
        title="Synthetic changed revision",
        body="The changed synthetic revision now triggers a model failure.",
        url=f"https://example.invalid/intel/{source_id}",
        context={"route": "candidate", "first_seen": "2026-10-11T10:00:00+08:00"},
    )

    def new_failure(_snapshot, _focus):
        raise ValueError("synthetic changed request failed")

    item = intel_store.claim("v2ex", limit=1)[0]
    failed = pipeline.process_item(intel_store, item, value_request=new_failure)
    assert failed["error"].startswith("value: ValueError:")
    with box.connect() as db:
        current = db.execute(
            "SELECT result,status FROM intel_items WHERE source_id=?", (source_id,)
        ).fetchone()
        decisions = db.execute(
            "SELECT status,data FROM review_decisions WHERE entry_id=(SELECT entry_id FROM review_origins WHERE source_id=?) ORDER BY id",
            (source_id,),
        ).fetchall()
    assert current["result"] is None
    assert current["status"] == "retry"
    decision_values = [json.loads(row["data"]) for row in decisions if row["data"]]
    assert any(value.get("reason") == old_result["reason"] for value in decision_values)
    assert decisions[-1]["status"] == "pending"


def _proof(value="", ref="", quote=""):
    return {"value": value, "ref": ref, "quote": quote}


def _compact_offer(facts):
    return {
        "product": "gpt",
        "delivery": "account",
        "access": "full",
        "condition": "normal",
        "currency": "CNY",
        "term_unit": "month",
        "allocation": "unknown",
        "fees": [],
        "utility": "useful",
        "availability": "available",
        "change": "initial",
        "car_prices": [],
        "facts": facts,
    }


def test_compact_trade_expands_sparse_facts_to_the_legacy_full_contract():
    compact = {
        "intent": "offer",
        "intel_segments": [],
        "offers": [
            _compact_offer(
                [
                    {"field": "description", "value": "GPT Pro", "ref": "r1", "quote": "GPT Pro"},
                    {"field": "price", "value": "20", "ref": "r1", "quote": "售价 20 元"},
                    {"field": "tier", "value": "Pro", "ref": "r1", "quote": "GPT Pro"},
                ]
            )
        ],
    }
    expanded = trade.expand_compact(compact)
    offer = expanded["offers"][0]

    assert "facts" not in offer
    assert offer["price"] == _proof("20", "r1", "售价 20 元")
    assert offer["tier"] == _proof("Pro", "r1", "GPT Pro")
    assert offer["provider"] == _proof()
    assert compact["offers"][0]["facts"]


def test_compact_trade_preserves_more_than_twelve_offers_and_unbounded_send_schema():
    count = 13
    offers = []
    evidence = []
    expected = []
    for number in range(1, count + 1):
        label = f"合成商品{number:02d}套餐"
        price = str(20 + number)
        ref = f"offer-{number:02d}"
        body = f"{label}，售价 {price} 元，月付。"
        evidence.append({"ref": ref, "title": label, "body": body})
        offers.append(
            _compact_offer(
                [
                    {"field": "description", "value": label, "ref": ref, "quote": label},
                    {"field": "price", "value": price, "ref": ref, "quote": f"售价 {price} 元"},
                    {"field": "term", "value": "1", "ref": ref, "quote": "月付"},
                ]
            )
        )
        expected.append((label, price, ref))

    compact = {"intent": "offer", "intel_segments": [], "offers": offers}
    schema = trade.COMPACT_SCHEMA["properties"]["offers"]
    assert "maxItems" not in schema

    expanded = trade.expand_compact(compact)
    checked = trade.validate(
        expanded,
        {"title": "合成多套餐样本", "evidence": evidence},
    )

    assert len(checked["offers"]) == count
    assert [
        (offer["description"]["quote"], offer["price"]["value"], offer["price"]["ref"])
        for offer in checked["offers"]
    ] == expected


def test_compact_trade_keeps_quote_and_price_validation_bound_to_source_text():
    snapshot = {
        "title": "GPT Pro 合成报价",
        "evidence": [{"ref": "r1", "title": "GPT Pro 合成报价", "body": "GPT Pro，售价 20 元，月付。"}],
    }
    compact = {
        "intent": "offer",
        "intel_segments": [],
        "offers": [
            _compact_offer(
                [
                    {"field": "description", "value": "GPT Pro", "ref": "r1", "quote": "GPT Pro"},
                    {"field": "price", "value": "20", "ref": "r1", "quote": "售价 20 元"},
                    {"field": "term", "value": "1", "ref": "r1", "quote": "月付"},
                    {"field": "tier", "value": "Pro", "ref": "r1", "quote": "GPT Pro"},
                ]
            )
        ],
    }
    expanded = trade.expand_compact(compact)
    checked = trade.validate(expanded, snapshot)
    assert checked["offers"][0]["price"]["value"] == "20"
    assert trade._priced(checked["offers"][0]["price"], "CNY") is True
    assert trade._priced(_proof("20", "r1", "售价 20 美元"), "CNY") is False

    invalid = copy.deepcopy(expanded)
    invalid["offers"][0]["price"]["value"] = "21"
    with pytest.raises(trade.TradeError, match="price"):
        trade.validate(invalid, snapshot)


def test_trade_request_uses_unbounded_wait_and_strict_tool_free_schema(
    monkeypatch,
):
    from pylon.tasks.software import monitor

    captured = {}
    compact = {"intent": "unknown", "intel_segments": [], "offers": []}
    event = {
        "type": "response.completed",
        "response": {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "content": [
                        {"type": "output_text", "text": json.dumps(compact)},
                    ],
                }
            ],
        },
    }

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def iter_lines(self):
            yield "data: " + json.dumps(event)
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

    monkeypatch.setattr(
        monitor,
        "read_env",
        lambda _name: {
            "GROK_MODELS_BASE_URL": "https://example.invalid",
            "XAI_API_KEY": "fixture-not-a-credential",
        },
    )
    monkeypatch.setattr(monitor.httpx, "Client", Client)
    calls = []
    original = monitor._request_grok_structured

    def capture_request(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return original(prompt, **kwargs)

    monkeypatch.setattr(monitor, "_request_grok_structured", capture_request)
    result = trade.request(
        {
            "evidence": [
                {
                    "origin": "forum",
                    "post_number": 1,
                    "ref": "fixture-r1",
                    "title": "合成交易样本",
                    "body": "仅用于请求参数契约测试。",
                    "source_published_at": "",
                    "source_edited_at": "",
                }
            ],
            "previously_notified": False,
        }
    )

    assert result == compact
    assert trade.VERSION == "linuxdo-trade-3-compact"
    assert len(calls) == 1
    _, kwargs = calls[0]
    assert kwargs["name"] == "linuxdo_trade"
    assert kwargs["schema"] is trade.COMPACT_SCHEMA
    assert kwargs.get("timeout") is None
    assert kwargs.get("total_timeout") is None

    assert captured["client"] == {"timeout": None, "trust_env": False}
    payload = captured["payload"]
    assert payload["reasoning"] == {"effort": "medium"}
    assert payload["tools"] == []
    assert payload["store"] is False
    assert payload["text"]["format"] == {
        "type": "json_schema",
        "name": "linuxdo_trade",
        "strict": True,
        "schema": trade.COMPACT_SCHEMA,
    }
