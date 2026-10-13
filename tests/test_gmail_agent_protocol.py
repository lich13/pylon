"""Exercise the Gmail request protocol without workers or production state."""

import json
import time

import pytest

from pylon.admin import agent_state as state
from pylon.admin import agents


def _forbidden(*_args, **_kwargs):
    pytest.fail("the isolated protocol must not launch or submit work")


@pytest.fixture(autouse=True)
def isolated_agent_state(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "ROOT", tmp_path / "agents")
    config = tmp_path / "agents.json"
    config.write_text(json.dumps({"waiting_seconds": 3600}))
    config.chmod(0o600)
    monkeypatch.setattr(state, "CONFIG", config)
    monkeypatch.setattr(agents, "kick", _forbidden)
    monkeypatch.setattr(agents.software, "capture", _forbidden)
    monkeypatch.setattr(agents.runtime, "run_turn", _forbidden)


def _gmail_task(profile, key):
    bundle = json.dumps({"prompt": "Review fixture messages.", "examples": []})
    return state.gmail_input("grok", profile, key, bundle)


def _snapshot():
    with state.database() as db:
        return {
            table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in ("tasks", "requests", "inputs", "events")
        }


def _read_only_lookup(monkeypatch, capsys, profile, key):
    before = _snapshot()
    with monkeypatch.context() as lookup:
        for name in ("submit", "gmail_input", "reply", "cancel"):
            lookup.setattr(state, name, _forbidden)
        assert agents.main(["gmail-request", profile, key]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert _snapshot() == before
    return json.loads(captured.out)


@pytest.mark.parametrize("profile", ["default", "webops"])
@pytest.mark.parametrize("identity", ["telegram", "cli"])
def test_gmail_request_recovers_original_task_without_submitting(
    monkeypatch, capsys, profile, identity
):
    key = f"telegram:{profile}:42" if identity == "telegram" else "cli:" + "a" * 32
    original = _gmail_task(profile, key)

    recovered = _read_only_lookup(monkeypatch, capsys, profile, key)
    repeated = _read_only_lookup(monkeypatch, capsys, profile, key)

    assert recovered == original == repeated
    assert recovered["purpose"] == "gmail_review"
    assert len(_snapshot()["tasks"]) == 1


@pytest.mark.parametrize("profile", ["default", "webops"])
def test_gmail_request_missing_key_returns_null(monkeypatch, capsys, profile):
    _gmail_task(profile, "cli:" + "a" * 32)

    assert _read_only_lookup(monkeypatch, capsys, profile, "cli:" + "b" * 32) is None


@pytest.mark.parametrize("profile", ["default", "webops"])
def test_gmail_request_does_not_return_another_profiles_task(monkeypatch, capsys, profile):
    key = "cli:" + "a" * 32
    other_profile = "webops" if profile == "default" else "default"
    _gmail_task(other_profile, key)

    assert _read_only_lookup(monkeypatch, capsys, profile, key) is None


@pytest.mark.parametrize("profile", ["default", "webops"])
@pytest.mark.parametrize("provider", ["codex", "grok"])
def test_gmail_request_does_not_expose_general_agent_task(
    monkeypatch, capsys, profile, provider
):
    key = "cli:" + "a" * 32
    general = state.submit(provider, profile, key, "Fixture-only general agent task.")
    with state.database() as db:
        state.finish(db, general["id"], "waiting", {"summary": "general-fixture-result"})

    assert _read_only_lookup(monkeypatch, capsys, profile, key) is None


@pytest.mark.parametrize(
    ("profile", "key"),
    [
        ("unknown", "cli:" + "a" * 32),
        ("", "cli:" + "a" * 32),
        ("default", "telegram:webops:42"),
        ("webops", "telegram:default:42"),
        ("webops", "telegram:webops:-1"),
        ("webops", "cli:" + "a" * 31),
        ("webops", "cli:" + "A" * 32),
        ("webops", "cli:" + "a" * 32 + "\n"),
        ("webops", "' OR 1=1 --"),
        ("webops", ""),
    ],
)
def test_gmail_request_rejects_invalid_identity_before_reading_state(
    monkeypatch, capsys, profile, key
):
    monkeypatch.setattr(state, "database", _forbidden)

    with pytest.raises(ValueError, match="invalid agent request identity"):
        agents.main(["gmail-request", profile, key])

    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize("status", ["queued", "running", "waiting", "succeeded"])
def test_generic_reply_directs_gmail_tasks_to_the_web_without_writing(status):
    original = _gmail_task("webops", "cli:" + "a" * 32)
    with state.database() as db:
        db.execute(
            "UPDATE tasks SET status=?, waiting_since=? WHERE id=?",
            (status, time.time(), original["id"]),
        )
    before = _snapshot()

    with pytest.raises(ValueError, match="Gmail 审核已移到网页.* /gmail"):
        state.reply(original["id"], "webops", "cli:" + "b" * 32, "Fixture reply.")

    assert _snapshot() == before


@pytest.mark.parametrize("provider", ["codex", "grok"])
@pytest.mark.parametrize("status", ["queued", "running", "waiting"])
def test_generic_reply_keeps_general_agent_tasks_and_request_idempotency(provider, status):
    original = state.submit(provider, "webops", "cli:" + "a" * 32, "Fixture task.")
    with state.database() as db:
        db.execute(
            "UPDATE tasks SET status=?, waiting_since=? WHERE id=?",
            (status, time.time(), original["id"]),
        )
    reply_key = "cli:" + "b" * 32

    reply = state.reply(original["id"], "webops", reply_key, "  Fixture reply.  ")
    after = _snapshot()
    repeated = state.reply(original["id"], "webops", reply_key, "  Fixture reply.  ")

    assert reply["id"] == repeated["id"] == original["id"]
    assert reply["purpose"] == "general"
    assert reply["status"] == ("queued" if status == "waiting" else status)
    assert [row["text"] for row in after["inputs"]] == ["Fixture task.", "Fixture reply."]
    assert len(after["tasks"]) == 1
    assert len(after["requests"]) == 2
    assert after["tasks"][0]["waiting_since"] is None
    assert _snapshot() == after
