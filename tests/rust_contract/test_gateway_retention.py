"""Public delivery contracts after terminal bodies have been reclaimed."""
import hashlib
import json
import sqlite3
import time

from test_gateway_contract import (
    Gateway, assert_rpc_rejected, event_row, open_context, rpc, rpc_value,
    seed_home, telegram, workdir,
)


def retained(home, event_id, kind, payload, receipt, *, cancelled=False):
    value = {
        "version": 1, "id": event_id, "module": "alpha", "profile": "bot_one",
        "kind": kind, "cancelled": cancelled,
        "status": "suppressed" if cancelled else "delivered", "attempts": 1,
        "created_at": 1.0, "updated_at": 2.0, "next_attempt": 0.0,
        "expires_at": time.time() + 90 * 86400,
        "payload_sha256": hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "receipt": receipt, "error": None, "callbacks": [],
    }
    with sqlite3.connect(home / "gateway/state.sqlite3") as db:
        db.execute("INSERT INTO kv VALUES(?,?)", (f"event-gone:{event_id}", json.dumps(value)))
    return value


def test_retained_results_support_outbox_reconciliation_and_button_clearing(workdir, telegram):
    home = workdir / "home"
    tokens = seed_home(home, telegram, [("bot_one", "101")], [
        {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
        {"id": "beta", "topic": "topic_beta", "job_id": "job_beta"},
    ], {"topic_alpha": "bot_one", "topic_beta": "bot_one"})
    with Gateway(home, telegram):
        alpha = open_context(home, "alpha", "job_alpha")["token"]
        beta = open_context(home, "beta", "job_beta")["token"]
        cases = [
            ("text", {"text": "fixture old text"}, {"message_id": 17, "chat_id": 101}),
            ("document", {"path": "missing/fixture.txt", "caption": "fixture"}, {"message_id": 18, "chat_id": 101}),
            ("bark", {"api_base": "https://example.invalid", "body": "fixture"}, {"code": 200}),
        ]
        initial = telegram.count(tokens["bot_one"], "sendMessage")
        expected_keys = {"id", "profile", "status", "attempts", "next_attempt", "updated_at", "receipt", "error", "module"}
        for kind, payload, receipt in cases:
            event_id = f"retained-{kind}"
            retained(home, event_id, kind, payload, receipt)
            submit = {"id": event_id, "kind": kind, "topic": "topic_alpha", "payload": payload}
            result = rpc_value(home, "delivery.submit", submit, alpha)
            assert set(result) == expected_keys
            assert result["status"] == "delivered" and result["receipt"] == receipt
            assert rpc_value(home, "delivery.get", {"id": event_id}, alpha) == result
            assert rpc_value(home, "delivery.cancel", {"id": event_id, "if_missing": True}, alpha) == result
            assert event_row(home, event_id) is None
            assert_rpc_rejected(rpc(home, "delivery.submit", submit, beta))
            assert_rpc_rejected(rpc(home, "delivery.submit", dict(submit, payload={"text": "changed"}), alpha))
            assert_rpc_rejected(rpc(home, "delivery.get", {"id": event_id}, beta))
            if kind != "bark":
                assert rpc_value(home, "delivery.lookup", {"message_id": receipt["message_id"]}, alpha) == []
                rpc_value(home, "delivery.clear_buttons", {"id": event_id}, alpha)
                assert_rpc_rejected(rpc(home, "delivery.clear_buttons", {"id": event_id}, beta))
        assert telegram.count(tokens["bot_one"], "editMessageReplyMarkup") == 2
        assert telegram.count(tokens["bot_one"], "sendMessage") == initial
        diagnostics = rpc_value(home, "diagnostics")
        metrics = diagnostics["metrics"]
        assert metrics["retention"]["placeholder_hits"] > 0
        assert metrics["retention"]["status"] == "not_run"
        assert metrics["retention"]["deleted"] == {}
        for key in ("command_empty_claims", "command_wakeups"):
            assert type(metrics["optimization"][key]) is int
        storage = metrics["storage"]
        assert storage["database"]["bytes"] > 0
        assert storage["database"]["error"] is None
        assert storage["run_dir_entries"] is None
        assert storage["run_dir_entries_sampled_at"] is None
        assert storage["run_dir_entries_error"] == "not_sampled"
        public_metrics = json.dumps(metrics)
        assert str(home) not in public_metrics
        assert all(token not in public_metrics for token in tokens.values())


def test_expired_cancelled_and_malformed_history_cannot_accidentally_replay(workdir, telegram):
    home = workdir / "home"
    seed_home(home, telegram, [("bot_one", "101")], [
        {"id": "alpha", "topic": "topic_alpha", "job_id": "job_alpha"},
    ], {"topic_alpha": "bot_one"})
    with Gateway(home, telegram):
        context = open_context(home, "alpha", "job_alpha")["token"]
        retained(home, "cancelled", "text", {}, None, cancelled=True)
        request = {"id": "cancelled", "kind": "text", "topic": "topic_alpha", "payload": {"text": "late fixture"}}
        assert rpc_value(home, "delivery.submit", request, context)["status"] == "suppressed"
        expired = retained(home, "expired", "text", {}, None, cancelled=True)
        expired["expires_at"] = time.time() - 1
        with sqlite3.connect(home / "gateway/state.sqlite3") as db:
            db.execute("UPDATE kv SET value=? WHERE key='event-gone:expired'", (json.dumps(expired),))
            db.execute("INSERT INTO kv VALUES('event-gone:broken','{}')")
        assert_rpc_rejected(rpc(home, "delivery.get", {"id": "expired"}, context))
        new = rpc_value(home, "delivery.submit", dict(request, id="expired", next_attempt=time.time() + 60), context)
        assert new["status"] == "pending" and event_row(home, "expired") is not None
        for method, params in [
            ("delivery.submit", dict(request, id="broken")),
            ("delivery.get", {"id": "broken"}),
            ("delivery.cancel", {"id": "broken", "if_missing": True}),
        ]:
            assert_rpc_rejected(rpc(home, method, params, context))
        assert event_row(home, "broken") is None
