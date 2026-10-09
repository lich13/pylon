"""Frozen recent-post rechecks with fresh source evidence and isolated state."""

import json
import socket
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from pylon.tasks.marketplace import pipeline, reconsider, sources
from pylon.tasks.marketplace.store import Store


NOW = 2_000_000_000.0
TITLE = "出售 iPhone 15 Pro 256G"
BODY = "自用 iPhone 15 Pro 256G，售价4500元，可邮寄。"
URLS = {
    "v2ex": "https://www.v2ex.com/t/",
    "nga": "https://bbs.nga.cn/read.php?tid=",
    "chiphell": "https://www.chiphell.com/thread-",
}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("PYLON_HOME", str(tmp_path))
    monkeypatch.setenv("PYLON_OUTBOX", str(tmp_path / "reconsider.sqlite3"))
    monkeypatch.setattr("pylon.tasks.quotes.health.inspect", lambda *_: {"available": False})

    def forbidden(*_args, **_kwargs):
        pytest.fail("reconsider fixtures must not make network requests")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr("pylon.tasks.software.monitor._request_grok_structured", forbidden)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(reconsider, "time", SimpleNamespace(
        time=lambda: NOW,
        monotonic=lambda: clock.now,
        sleep=lambda seconds: setattr(clock, "now", clock.now + seconds),
    ))


@pytest.fixture
def market():
    return Store()


def read_post(store, source_id="1", source="v2ex"):
    with store.box.connect() as db:
        return dict(db.execute(
            "SELECT * FROM marketplace_posts WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone())


def seed(store, source_id="1", *, source="v2ex", first_seen=NOW - 60,
         status="ready", history=False, event_id=None):
    assert store.ingest(source, [{
        "id": source_id, "url": URLS[source] + source_id,
        "title": TITLE, "body_full": BODY, "body_complete": True,
        "posted_at": "2026-10-08T09:00:00+08:00",
    }], history=history) == 1
    with store.box.connect() as db:
        db.execute(
            "UPDATE marketplace_posts SET first_seen=?,status=?,event_id=? "
            "WHERE source=? AND source_id=?",
            (first_seen, status, event_id, source, source_id),
        )
    return read_post(store, source_id, source)


def fresh(**changes):
    return {
        "title": "出售 iPhone 15 Pro 512G", "title_refreshed": True,
        "body_full": "iPhone 15 Pro 512G，售价4300元，可邮寄。",
        "body_complete": True, "platform_category": "手机",
        "posted_at": "2026-10-09T09:00:00+08:00", **changes,
    }


def reviews(store):
    with store.box.connect() as db:
        return [dict(row) for row in db.execute("SELECT * FROM market_reviews")]


@pytest.mark.parametrize("status", [
    "ready", "skipped", "needs_review", "pending_analysis", "pending_body", "unavailable",
])
def test_stage_freezes_each_eligible_recent_status(market, status):
    before = seed(market, status=status)

    result = reconsider.stage(market, now=NOW)

    assert result == {"started_at": NOW, "cutoff": NOW - 86400, "staged": 1}
    assert read_post(market) == {**before, "status": "refresh_required"}
    assert market.checkpoint(reconsider.CHECKPOINT) == result


def test_stage_uses_inclusive_first_seen_window_and_never_stages_history_or_events(market):
    cases = [
        ({"first_seen": NOW - 86400}, True),
        ({"first_seen": NOW}, True),
        ({"first_seen": NOW - 86400.001}, False),
        ({"first_seen": NOW + 0.001}, False),
        ({"history": True}, False),
        ({"event_id": "fixture-delivered-event"}, False),
        ({"status": "queued"}, False),
        ({"status": "historical"}, False),
        ({"status": "refresh_required"}, False),
    ]
    before = [seed(market, str(index), **options)
              for index, (options, _eligible) in enumerate(cases, 1)]

    assert reconsider.stage(market, now=NOW)["staged"] == 2

    for index, (prior, (_options, eligible)) in enumerate(zip(before, cases), 1):
        expected = {**prior, "status": "refresh_required"} if eligible else prior
        assert read_post(market, str(index)) == expected


@pytest.mark.parametrize("initial_count", [0, 1])
def test_repeated_stage_after_restart_keeps_the_original_frozen_set(market, initial_count):
    if initial_count:
        seed(market)
    result = reconsider.stage(market, now=NOW)
    new_in_window = seed(market, "2", first_seen=NOW - 30)
    new_after_start = seed(market, "3", first_seen=NOW + 30)
    if initial_count:
        assert reconsider.accept_fresh(market, read_post(market), fresh())
    accepted = read_post(market) if initial_count else None

    assert reconsider.stage(Store(), now=NOW + 3600) == result

    assert result["staged"] == initial_count
    assert read_post(market, "2") == new_in_window
    assert read_post(market, "3") == new_after_start
    if initial_count:
        assert read_post(market) == accepted


@pytest.mark.parametrize("field,value", [
    ("title_refreshed", False), ("title_refreshed", None),
    ("body_complete", False), ("body_complete", None),
    ("body_full", ""), ("body_full", None),
])
def test_accept_requires_both_fresh_title_and_complete_nonempty_body(market, field, value):
    old = seed(market, status="refresh_required")
    refreshed = fresh(**{field: value})
    if value is None:
        del refreshed[field]

    with pytest.raises(sources.ContentUnavailable, match="fresh_title_or_body_missing"):
        reconsider.accept_fresh(market, old, refreshed)

    assert read_post(market) == old
    assert reviews(market) == []


def test_accept_reclassifies_the_fresh_title_body_category_and_date(market):
    old = seed(market, status="refresh_required")
    with market.box.connect() as db:
        db.execute("UPDATE marketplace_posts SET error='fixture stale error'")

    assert reconsider.accept_fresh(market, old, fresh()) is True

    current = read_post(market)
    assert current["title"] == fresh()["title"]
    assert current["body"] == fresh()["body_full"]
    assert current["body_complete"] == 1
    assert current["platform_category"] == "手机"
    assert current["posted_at"] == fresh()["posted_at"]
    assert current["status"] == "ready" and current["error"] == ""
    assert current["first_seen"] == old["first_seen"]
    assert current["event_id"] is None and current["history"] == 0
    items = json.loads(current["items"])
    assert len(items) == 1
    assert items[0]["price"] == 4300
    assert items[0]["attributes"]["facts"]["storage_gb"] == 512


@pytest.mark.parametrize("changes", [
    {"status": "ready"}, {"status": "queued"}, {"history": 1},
    {"event_id": "fixture-delivered-event"},
])
def test_accept_rechecks_persisted_guards_after_a_fetch_started(market, changes):
    old = seed(market, status="refresh_required")
    with market.box.connect() as db:
        for field, value in changes.items():
            db.execute(f"UPDATE marketplace_posts SET {field}=?", (value,))
    before = read_post(market)

    assert reconsider.accept_fresh(market, old, fresh()) is False

    assert read_post(market) == before


def test_a_fresh_sold_title_excludes_an_otherwise_available_stale_body(market):
    old = seed(market, status="refresh_required")
    assert reconsider.accept_fresh(market, old, fresh(
        title="[已售] iPhone 15 Pro 256G", body_full=BODY,
    ))
    assert {item["status"] for item in json.loads(read_post(market)["items"])} == {"sold"}

    assert pipeline.process(
        market, current_policy={"version": 1, "enabled": True, "categories": ["phone.smartphone"]},
        wanted_policy={"version": 1, "enabled": False, "targets": []},
    ) == 0

    current = read_post(market)
    assert current["status"] == "skipped" and current["event_id"] is None
    with market.box.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def install_http_client(monkeypatch, responses):
    ctx = Mock()
    ctx.get.side_effect = responses

    @contextmanager
    def client(source):
        assert source == "v2ex"
        yield ctx, None

    monkeypatch.setattr(reconsider, "client", client)
    return ctx


def response(status, *, html=None):
    return httpx.Response(
        status, request=httpx.Request("GET", "https://www.v2ex.com/t/1"),
        text=html if html is not None else f"<h1>{TITLE}</h1><div class='topic_content'>{BODY}</div>",
    )


@pytest.mark.parametrize("status,reason", [
    (401, "permission_required"), (403, "permission_required"),
    (404, "deleted_or_not_found"), (410, "deleted_or_not_found"),
    (500, "HTTPStatusError"),
])
def test_http_failure_moves_only_its_post_to_review_and_continues(market, monkeypatch, status, reason):
    first = seed(market, status="refresh_required", first_seen=NOW - 120)
    seed(market, "2", status="refresh_required")
    ctx = install_http_client(monkeypatch, [response(status), response(200)])

    assert reconsider.drain_source(market, "v2ex") == {
        "source": "v2ex", "total": 2, "fresh": 1, "review": 1, "limited": 0,
    }

    current = read_post(market)
    assert current["status"] == "needs_review"
    assert current["error"] == "fresh_recheck:" + reason
    assert current["title"] == first["title"] and current["body"] == first["body"]
    assert read_post(market, "2")["status"] == "ready"
    assert ctx.get.call_count == 2
    assert [(row["source_id"], row["item_index"], row["status"], row["reason"])
            for row in reviews(market)] == [("1", -1, "pending", "补发前无法重新确认原帖：" + reason)]


def test_rate_limit_preserves_current_and_remaining_rows_for_resume(market, monkeypatch):
    seed(market, "1", status="refresh_required", first_seen=NOW - 180)
    second = seed(market, "2", status="refresh_required", first_seen=NOW - 120)
    third = seed(market, "3", status="refresh_required")
    ctx = install_http_client(monkeypatch, [response(200), response(429)])

    assert reconsider.drain_source(market, "v2ex") == {
        "source": "v2ex", "total": 3, "fresh": 1, "review": 0, "limited": 1,
    }

    assert read_post(market, "1")["status"] == "ready"
    assert read_post(market, "2") == second
    assert read_post(market, "3") == third
    assert reviews(market) == [] and ctx.get.call_count == 2


def test_missing_live_heading_goes_to_review_even_with_complete_body(market, monkeypatch):
    seed(market, status="refresh_required")
    install_http_client(monkeypatch, [response(200, html=f"<div class='topic_content'>{BODY}</div>")])

    assert reconsider.drain_source(market, "v2ex")["review"] == 1

    assert read_post(market)["status"] == "needs_review"
    assert read_post(market)["error"] == "fresh_recheck:fresh_title_or_body_missing"


def test_main_drains_sources_in_parallel_with_independent_failure_states(market, monkeypatch, capsys):
    for source in reconsider.INTERVALS:
        seed(market, source=source)
    reconsider.stage(market, now=NOW)
    barrier = threading.Barrier(3)

    @contextmanager
    def client(source):
        yield source, None

    def detail(ctx, source, post, *, page):
        assert ctx == source and post["id"] == "1" and page is None
        barrier.wait(timeout=5)
        if source == "v2ex":
            response(500).raise_for_status()
        if source == "nga":
            raise sources.SourceRateLimited("fixture_source_limited")
        return fresh()

    monkeypatch.setattr(reconsider, "client", client)
    monkeypatch.setattr(sources, "detail", detail)
    monkeypatch.setattr(sys, "argv", ["reconsider"])

    reconsider.main()

    results = {row["source"]: row for row in map(json.loads, capsys.readouterr().out.splitlines())}
    assert results == {
        "v2ex": {"source": "v2ex", "total": 1, "fresh": 0, "review": 1, "limited": 0},
        "chiphell": {"source": "chiphell", "total": 1, "fresh": 1, "review": 0, "limited": 0},
        "nga": {"source": "nga", "total": 1, "fresh": 0, "review": 0, "limited": 1},
    }
    assert read_post(market, source="chiphell")["status"] == "ready"
    assert read_post(market, source="v2ex")["status"] == "needs_review"
    assert read_post(market, source="nga")["status"] == "refresh_required"


@pytest.mark.parametrize("heading", ["<h1>[已售] <span>iPhone 15 Pro 256G</span></h1>", "", "<h1> </h1>"])
@pytest.mark.parametrize("with_context", [False, True])
def test_v2ex_detail_marks_only_a_live_nonempty_heading_as_refreshed(monkeypatch, heading, with_context):
    old = {"title": TITLE, "url": URLS["v2ex"] + "1"}
    html = heading + f"<div class='topic_content'>{BODY}</div><div class='subtle'>附言：支持邮寄</div>"
    ctx = Mock() if with_context else None
    if ctx is not None:
        ctx.get.return_value = response(200, html=html)
    else:
        monkeypatch.setattr("pylon.tasks.forums.v2ex.fetch", Mock(return_value=html))

    result = sources.detail(ctx, "v2ex", old)

    live = "<span>" in heading
    assert result["title_refreshed"] is live
    assert result["title"] == ("[已售] iPhone 15 Pro 256G" if live else TITLE)
    assert result["body_complete"] is True
    assert BODY in result["body_full"] and "附言：支持邮寄" in result["body_full"]


@pytest.mark.parametrize("source", ["nga", "chiphell"])
@pytest.mark.parametrize("heading", [" [已售] iPhone 15 Pro 256G ", "", None])
def test_browser_source_detail_refreshes_only_the_first_post_heading(source, heading):
    old = {"title": TITLE, "url": URLS[source] + "1", "posted_at": ""}
    page = Mock(url=old["url"])
    page.goto.return_value = SimpleNamespace(status=200)

    def locator(selector):
        found = Mock()
        found.first = found
        found.count.return_value = 0
        found.all_text_contents.return_value = ["2026-10-09 09:00"]
        if selector in {"#postsubject0", "#thread_subject"}:
            found.count.return_value = int(heading is not None)
            found.inner_text.return_value = heading or ""
            found.evaluate.return_value = "手机"
        elif selector in {"#postcontent0", 'td[id^="postmessage_"]', "td.t_f"}:
            found.count.return_value = 1
            found.inner_html.return_value = f"<p>{BODY}</p>"
            found.evaluate.return_value = f"<p>{BODY}</p>"
        elif selector == "body":
            found.inner_text.return_value = BODY
        return found

    page.locator.side_effect = locator
    page.get_by_text.return_value.count.return_value = 0
    ctx = Mock()
    ctx.new_page.return_value = page

    result = sources.detail(ctx, source, old)

    assert result["title_refreshed"] is bool(heading)
    assert result["title"] == (heading.strip() if heading else TITLE)
    assert result["body_complete"] is True and BODY in result["body_full"]
    assert result["posted_at"] == "2026-10-09T09:00:00+08:00"
    page.close.assert_called_once_with()
