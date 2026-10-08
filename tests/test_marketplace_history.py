from __future__ import annotations

import json
import sys
import time
import types
from contextlib import contextmanager
from unittest.mock import patch

import pytest

from pylon.outbox import Outbox
from pylon.tasks.marketplace import history, sources, request_gate
from pylon.tasks.marketplace.store import Store


@pytest.fixture(autouse=True)
def reset_nga_request_timing(monkeypatch, tmp_path):
    clock = {"now": 100.0}

    def fake_sleep(seconds):
        clock["now"] += seconds

    monkeypatch.setattr(request_gate, "home", lambda: tmp_path)
    monkeypatch.setattr(sources.time, "time", lambda: clock["now"])
    monkeypatch.setattr(sources.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(sources.time, "sleep", fake_sleep)


def make_store(tmp_path):
    box = Outbox(tmp_path / "outbox.sqlite3")
    return box, Store(box)


def history_post(source_id: str, posted_at: str, *, title: str = "出 iPhone 15 Pro") -> dict:
    return {
        "id": source_id,
        "title": title,
        "url": f"https://bbs.nga.cn/read.php?tid={source_id}",
        "posted_at": posted_at,
        "body_full": "",
        "body_complete": False,
    }


@contextmanager
def fake_browser(_source):
    yield object()


def _nga_ad_url(target: str) -> str:
    return f"https://bbs.nga.cn/?{target}"


class FakeNGALocator:
    def __init__(self, page, *, records=None, html="", text=None, missing=False):
        self.page = page
        self.records = records or []
        self.html = html
        self.text = text
        self.missing = missing
        self.first = self

    def count(self):
        return int(self.page.interstitial)

    def wait_for(self, **_kwargs):
        if self.missing:
            if self.page.body_on_missing is not None:
                self.page.body_text = self.page.body_on_missing
            raise TimeoutError("fixture selector missing")
        return None

    def evaluate_all(self, _script):
        return self.records

    def inner_html(self):
        return self.html

    def all_text_contents(self):
        return []

    def inner_text(self, *_args, **_kwargs):
        return self.page.body_text if self.text is None else self.text


class FakeNGAPage:
    def __init__(self, requested_url, *, records=None, body_html="", initial_redirect=True,
                 persistent_interstitial=False, body_text="", body_on_missing=None,
                 missing_selectors=(), response_status=None):
        self.requested_url = requested_url
        self.records = records or []
        self.body_html = body_html
        self.body_text = body_text
        self.body_on_missing = body_on_missing
        self.missing_selectors = set(missing_selectors)
        self.response_status = response_status
        self.initial_redirect = initial_redirect
        self.persistent_interstitial = persistent_interstitial
        self.url = requested_url if initial_redirect else _nga_ad_url(requested_url)
        self.interstitial = not initial_redirect
        self.goto_calls = []
        self.goto_times = []
        self.closed = False

    def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.goto_times.append(time.monotonic())
        is_initial_navigation = self.initial_redirect and len(self.goto_calls) == 1
        if is_initial_navigation:
            self.url = _nga_ad_url(url)
            self.interstitial = True
        elif self.persistent_interstitial:
            self.url = _nga_ad_url(url)
            self.interstitial = True
        else:
            self.url = url
            self.interstitial = False
        return types.SimpleNamespace(status=self.response_status) if self.response_status is not None else None

    def get_by_text(self, text, *, exact=False):
        assert text == "点此跳过广告"
        assert exact is True
        return FakeNGALocator(self)

    def locator(self, selector):
        if selector == "body":
            return FakeNGALocator(self)
        if selector.startswith("a.topic"):
            return FakeNGALocator(
                self, records=self.records, missing=selector in self.missing_selectors
            )
        if selector == "#postcontent0":
            return FakeNGALocator(
                self, html=self.body_html, missing=selector in self.missing_selectors
            )
        return FakeNGALocator(self, missing=selector in self.missing_selectors)

    def close(self):
        self.closed = True


class FakeNGAContext:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


def test_history_cursor_resumes_from_sqlite_after_store_restart(tmp_path):
    box, store = make_store(tmp_path)
    with (
        patch.object(history, "browser", side_effect=fake_browser),
        patch.object(history, "list_page", side_effect=[
            [history_post("11", "2099-10-08T00:00:00+00:00")],
            [history_post("12", "2099-10-07T00:00:00+00:00")],
        ]) as list_page,
    ):
        first = history.backfill(store, "nga", max_pages=1, delay=0, fetch_bodies=False)
        restarted = Store(Outbox(box.path))
        second = history.backfill(restarted, "nga", max_pages=1, delay=0, fetch_bodies=False)

    assert first["page"] == 2
    assert first["pages"] == 1
    assert second["page"] == 3
    assert second["pages"] == 2
    assert [call.args[2] for call in list_page.call_args_list] == [1, 2]


def test_repeated_page_signature_stops_as_limited(tmp_path):
    _, store = make_store(tmp_path)
    same_page = [history_post("21", "2099-10-08T00:00:00+00:00")]
    with (
        patch.object(history, "browser", side_effect=fake_browser),
        patch.object(history, "list_page", side_effect=[same_page, same_page]) as list_page,
    ):
        result = history.backfill(store, "nga", max_pages=4, delay=0, fetch_bodies=False)

    assert list_page.call_count == 2
    assert result["complete"] is True
    assert result["limited"] is True
    assert result["stop_reason"] == "pagination_repeated"


def test_chiphell_out_of_range_page_is_recorded_as_history_boundary(tmp_path):
    _, store = make_store(tmp_path)
    with (
        patch.object(history, "browser", side_effect=fake_browser),
        patch.object(history, "list_page", side_effect=sources.HistoryUnavailable("history_page_out_of_range")),
    ):
        result = history.backfill(store, "chiphell", max_pages=4, delay=0, fetch_bodies=False)

    assert result["complete"] is True
    assert result["limited"] is True
    assert result["stop_reason"] == "history_page_out_of_range"
    assert store.checkpoint("history:chiphell")["stop_reason"] == "history_page_out_of_range"


def test_one_old_post_does_not_end_the_90_day_scan_before_the_page_is_old(tmp_path):
    _, store = make_store(tmp_path)
    page_one = [
        history_post("31", "2000-01-01T00:00:00+00:00"),
        history_post("32", "2099-10-07T00:00:00+00:00"),
    ]
    page_two = [history_post("33", "2099-10-08T00:00:00+00:00")]
    with (
        patch.object(history, "browser", side_effect=fake_browser),
        patch.object(history, "list_page", side_effect=[page_one, page_two]) as list_page,
    ):
        result = history.backfill(store, "nga", max_pages=2, delay=0, fetch_bodies=False)

    assert list_page.call_count == 2
    assert result["complete"] is False
    assert result["pages"] == 2
    with store.box.connect() as db:
        ids = {row[0] for row in db.execute("SELECT source_id FROM marketplace_posts")}
    assert ids == {"32", "33"}


def test_list_then_body_refill_updates_posted_at_and_stays_out_of_live_queue(tmp_path):
    box, store = make_store(tmp_path)
    initial = history_post("44", "2099-10-08T00:00:00+00:00")
    detail = {
        **initial,
        "posted_at": "2100-01-02T03:04:00+00:00",
        "body_full": "iPhone 15 Pro 256G 4800元出售",
        "body_complete": True,
    }
    with (
        patch.object(history, "browser", side_effect=fake_browser),
        patch.object(history, "list_page", return_value=[initial]),
    ):
        history.backfill(store, "nga", max_pages=1, delay=0, fetch_bodies=False)

    with (
        patch.object(sources, "browser", side_effect=fake_browser),
        patch.object(sources, "detail", return_value=detail),
    ):
        sources.refill(store, history=True)

    with box.connect() as db:
        row = db.execute(
            "SELECT posted_at,body_complete,status,history FROM marketplace_posts WHERE source='nga' AND source_id='44'"
        ).fetchone()
    assert row["posted_at"] == "2100-01-02T03:04:00+00:00"
    assert row["body_complete"] == 1
    assert row["status"] == "historical"
    assert row["history"] == 1

    from pylon.tasks.marketplace.pipeline import process

    with patch("pylon.tasks.quotes.health.inspect", return_value={"available": False}):
        assert process(store, current_policy={"version": 1, "enabled": True, "categories": ["phone.smartphone"]}) == 0
    with box.connect() as db:
        assert db.execute("SELECT id FROM events WHERE job_id='marketplace-poll'").fetchall() == []


def test_nga_cookie_loader_rejects_cookie_for_unrelated_domain(tmp_path, monkeypatch):
    cookie_file = tmp_path / "nga-cookies.json"
    cookie_file.write_text(json.dumps({"cookies": [
        {"name": "session", "value": "fixture", "domain": ".example.invalid", "path": "/"},
    ]}))
    monkeypatch.setenv("PYLON_NGA_COOKIES", str(cookie_file))

    class FakeContext:
        closed = False

        def close(self):
            self.closed = True

        def add_cookies(self, _cookies):
            raise AssertionError("out-of-scope cookies must not be installed")

    class FakeEngine:
        context = FakeContext()
        closed = False

        def new_context(self):
            return self.context

        def close(self):
            self.closed = True

    engine = FakeEngine()

    class FakeChromium:
        def launch(self, **_kwargs):
            return engine

    class FakePlaywright:
        chromium = FakeChromium()

    class FakePlaywrightManager:
        def __enter__(self):
            return FakePlaywright()

        def __exit__(self, *_exc):
            return False

    fake_package = types.ModuleType("playwright")
    fake_sync_api = types.ModuleType("playwright.sync_api")
    fake_sync_api.sync_playwright = FakePlaywrightManager
    with (
        patch.dict(sys.modules, {"playwright": fake_package, "playwright.sync_api": fake_sync_api}),
        patch("pylon.shared.playwright_env.ensure", return_value=""),
    ):
        with pytest.raises(ValueError, match="invalid NGA cookie scope"):
            with sources.browser("nga"):
                pytest.fail("cookie validation must stop before yielding the browser")

    assert engine.context.closed is True
    assert engine.closed is True


def test_failed_listing_page_keeps_saved_cursor_unchanged(tmp_path):
    _, store = make_store(tmp_path)
    before = {"page": 7, "before": None, "pages": 6, "complete": False, "days": 90,
              "cutoff": "2099-07-10T00:00:00+00:00", "gaps": 0}
    store.checkpoint("history:nga", before)
    with (
        patch.object(history, "browser", side_effect=fake_browser),
        patch.object(history, "list_page", side_effect=RuntimeError("fixture list failure")),
    ):
        with pytest.raises(RuntimeError, match="fixture list failure"):
            history.backfill(store, "nga", delay=0, fetch_bodies=False)

    assert store.checkpoint("history:nga") == before


def test_incomplete_body_refill_stays_unavailable_and_never_claims_full_body(tmp_path):
    box, store = make_store(tmp_path)
    store.ingest("nga", [history_post("55", "2099-10-08T00:00:00+00:00")], history=True)
    incomplete_detail = {
        **history_post("55", "2099-10-08T00:00:00+00:00"),
        "body_full": "",
        "body_complete": False,
    }
    with (
        patch.object(sources, "browser", side_effect=fake_browser),
        patch.object(sources, "detail", return_value=incomplete_detail),
    ):
        sources.refill(store, history=True)

    with box.connect() as db:
        row = db.execute("SELECT body,body_complete,status FROM marketplace_posts WHERE source_id='55'").fetchone()
    assert row["body"] == ""
    assert row["body_complete"] == 0
    assert row["status"] == "unavailable"


def test_refill_rate_limit_defers_current_nga_row_and_continues_other_sources(
    tmp_path, monkeypatch
):
    box, store = make_store(tmp_path)
    posts = [
        ("nga", "70", "https://bbs.nga.cn/read.php?tid=70"),
        ("nga", "71", "https://bbs.nga.cn/read.php?tid=71"),
        ("chiphell", "72", "https://www.chiphell.com/thread-72-1-1.html"),
    ]
    for source, source_id, url in posts:
        store.ingest(
            source,
            [{"id": source_id, "title": "出 iPhone 15 Pro", "url": url, "body_complete": False}],
            history=True,
        )

    now = {"value": 1000.0}
    monkeypatch.setattr(sources.time, "time", lambda: now["value"])
    monkeypatch.setattr(sources.time, "sleep", lambda _seconds: None)

    @contextmanager
    def browser_for_source(_source):
        yield object()

    detail_calls = []

    def detail_for_source(_ctx, source, post):
        detail_calls.append((source, post["source_id"]))
        if source == "nga":
            raise sources.SourceRateLimited("nga_rate_limited")
        return {
            **post,
            "body_full": "iPhone 15 Pro 256G 4800元出售",
            "body_complete": True,
        }

    with (
        patch.object(sources, "browser", side_effect=browser_for_source),
        patch.object(sources, "detail", side_effect=detail_for_source),
    ):
        errors = sources.refill(store, history=True)

    assert errors == ["nga:source_rate_limited"]
    assert detail_calls == [("nga", "70"), ("chiphell", "72")]
    assert store.checkpoint("nga:cooldown") == {"until": 1060.0}
    with box.connect() as db:
        rows = {
            row["source_id"]: row
            for row in db.execute(
                "SELECT source_id,body_complete,status,attempts,next_attempt,error FROM marketplace_posts"
            )
        }
    assert rows["70"]["attempts"] == 1
    assert rows["70"]["next_attempt"] == 1060.0
    assert rows["70"]["error"] == "source_rate_limited"
    assert rows["70"]["status"] == "historical"
    assert rows["70"]["body_complete"] == 0
    assert rows["71"]["attempts"] == 0
    assert rows["71"]["error"] == ""
    assert rows["72"]["body_complete"] == 1
    assert rows["72"]["status"] == "historical"


@pytest.mark.parametrize("path", ["/thread.php", "/read.php"])
def test_nga_ad_target_accepts_the_same_https_forum_request(path):
    requested = f"https://bbs.nga.cn{path}?tid=901&page=3"
    current = _nga_ad_url(requested)

    assert sources.nga_ad_target(current, requested) == requested


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (_nga_ad_url("https://example.invalid/read.php?tid=901&page=3"),
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        ("https://evil.invalid/?https://bbs.nga.cn/read.php?tid=901&page=3",
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        (_nga_ad_url("http://bbs.nga.cn/read.php?tid=901&page=3"),
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        (_nga_ad_url("https://bbs.nga.cn.example.invalid/read.php?tid=901&page=3"),
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        (_nga_ad_url("https://bbs.nga.cn/forum.php?tid=901&page=3"),
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        (_nga_ad_url("https://bbs.nga.cn/read.php?tid=902&page=3"),
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        (_nga_ad_url("https://bbs.nga.cn/read.php?tid=901&page=4"),
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        (_nga_ad_url("https://bbs.nga.cn/read.php?tid=901&page=3&extra=1"),
         "https://bbs.nga.cn/read.php?tid=901&page=3"),
        (_nga_ad_url("https://bbs.nga.cn/read.php?tid=901&page=3"),
         "https://example.invalid/read.php?tid=901&page=3"),
    ],
)
def test_nga_ad_target_rejects_external_or_changed_destinations(current, requested):
    with pytest.raises(ValueError, match="unexpected NGA advertisement destination|changed the requested page"):
        sources.nga_ad_target(current, requested)


def test_repeated_nga_ad_interstitial_is_bounded_to_two_follows():
    requested = "https://bbs.nga.cn/read.php?tid=903&page=2"
    page = FakeNGAPage(requested, initial_redirect=False, persistent_interstitial=True)

    with pytest.raises(RuntimeError, match="nga_ad_interstitial"):
        sources.skip_nga_ad(page, requested)

    assert page.goto_calls == [requested, requested]


def test_nga_list_page_follows_ad_and_filters_notice_titles():
    requested = sources.NGA_URL + "&page=3"
    records = [
        {"title": "[通知] 系统维护", "url": "https://bbs.nga.cn/read.php?tid=901", "posted": "26-10-08 12:00", "dates": "", "rowText": ""},
        {"title": "[公告] 交易规则", "url": "https://bbs.nga.cn/read.php?tid=902", "posted": "26-10-08 12:00", "dates": "", "rowText": ""},
        {"title": "【版规】论坛版规", "url": "https://bbs.nga.cn/read.php?tid=903", "posted": "26-10-08 12:00", "dates": "", "rowText": ""},
        {"title": "【必读】交易须知", "url": "https://bbs.nga.cn/read.php?tid=904", "posted": "26-10-08 12:00", "dates": "", "rowText": ""},
        {"title": "[二手交易] 出 RTX 3070", "url": "https://bbs.nga.cn/read.php?tid=905", "posted": "26-10-08 12:00", "dates": "", "rowText": ""},
    ]
    page = FakeNGAPage(requested, records=records)

    posts = sources.list_page(FakeNGAContext(page), "nga", page_number=3)

    assert page.goto_calls == [requested, requested]
    assert [post["id"] for post in posts] == ["905"]
    assert page.closed is True


def test_nga_detail_follows_ad_before_reading_post_body():
    requested = "https://bbs.nga.cn/read.php?tid=906&page=2"
    post = {
        "id": "906",
        "title": "[二手交易] 出 RTX 3070",
        "url": requested,
        "posted_at": "",
        "body_full": "",
        "body_complete": False,
    }
    page = FakeNGAPage(requested, body_html="<p>RTX 3070 显卡 1800元出售</p>")

    result = sources.detail(FakeNGAContext(page), "nga", post)

    assert page.goto_calls == [requested, requested]
    assert "RTX 3070" in result["body_full"]
    assert result["body_complete"] is True
    assert page.closed is True


def test_nga_ad_follow_waits_fifteen_seconds_on_the_same_context(monkeypatch):
    clock = {"now": 100.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(sources.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(sources.time, "sleep", fake_sleep)
    monkeypatch.setattr(sources.time, "time", lambda: clock["now"])
    requested = sources.NGA_URL + "&page=2"
    page = FakeNGAPage(requested)

    sources.list_page(FakeNGAContext(page), "nga", page_number=2)

    assert page.goto_calls == [requested, requested]
    assert page.goto_times[1] - page.goto_times[0] >= 15
    assert sleeps == [15]


def test_nga_request_interval_survives_context_replacement(monkeypatch):
    clock = {"now": 100.0}
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(sources.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(sources.time, "sleep", fake_sleep)
    monkeypatch.setattr(sources.time, "time", lambda: clock["now"])

    first_url = sources.NGA_URL + "&page=1"
    second_url = sources.NGA_URL + "&page=2"
    first_page = FakeNGAPage(first_url, initial_redirect=False)
    second_page = FakeNGAPage(second_url, initial_redirect=False)

    sources.list_page(FakeNGAContext(first_page), "nga", page_number=1)
    sources.list_page(FakeNGAContext(second_page), "nga", page_number=2)

    assert first_page.goto_times == [100.0]
    assert second_page.goto_times == [115.0]
    assert sleeps == [15]


def test_nga_http_429_is_reported_as_a_source_rate_limit():
    requested = sources.NGA_URL + "&page=1"
    page = FakeNGAPage(requested, response_status=429)

    with pytest.raises(sources.SourceRateLimited, match="nga_rate_limited"):
        sources.nga_goto(page, requested, FakeNGAContext(page))

    assert page.goto_calls == [requested]


def test_nga_error_2048_refresh_message_is_reported_as_a_source_rate_limit():
    page = FakeNGAPage(
        sources.NGA_URL,
        body_text="(ERROR:2048) > 刷新过快 请等候数秒再行访问",
    )

    with pytest.raises(sources.SourceRateLimited, match="nga_rate_limited"):
        sources.check_nga_rate_limit(page)


@pytest.mark.parametrize(
    ("kind", "selector", "url"),
    [
        ("list", 'a.topic[id^="t_tt"]', sources.NGA_URL + "&page=1"),
        ("detail", "#postcontent0", "https://bbs.nga.cn/read.php?tid=907"),
    ],
)
def test_nga_missing_content_selector_rechecks_for_rate_limit(kind, selector, url):
    page = FakeNGAPage(
        url,
        initial_redirect=False,
        missing_selectors={selector},
        body_on_missing="(ERROR:2048) > 刷新过快 请等候数秒再行访问",
    )
    context = FakeNGAContext(page)

    with pytest.raises(sources.SourceRateLimited, match="nga_rate_limited"):
        if kind == "list":
            sources.list_page(context, "nga")
        else:
            sources.detail(
                context,
                "nga",
                {
                    "id": "907",
                    "title": "出 iPhone 15 Pro",
                    "url": url,
                    "posted_at": "",
                    "body_full": "",
                    "body_complete": False,
                },
            )

    assert page.closed is True


def test_nga_two_digit_post_date_wins_over_a_later_reply_time():
    from pylon.tasks.marketplace import sources as marketplace_sources

    record = {
        "title": "出 iPhone 15 Pro",
        "url": "https://bbs.nga.cn/read.php?tid=66",
        "posted": "26-10-07 21:44",
        "dates": "26-10-08 09:12",
        "rowText": "回帖时间 26-10-08 09:12",
    }

    class FakeLocator:
        first = None

        def __init__(self):
            self.first = self

        def wait_for(self, **_kwargs):
            return None

        def count(self):
            return 0

        def evaluate_all(self, _script):
            return [record]

        def inner_text(self, **_kwargs):
            return ""

    class FakePage:
        def __init__(self):
            self.fake_locator = FakeLocator()
            self.url = ""

        def goto(self, url, **_kwargs):
            self.url = url
            return None

        def get_by_text(self, _text, **_kwargs):
            return self.fake_locator

        def locator(self, _selector):
            return self.fake_locator

        def close(self):
            return None

    class FakeContext:
        def new_page(self):
            return FakePage()

    expected = "2026-10-07T21:44:00+08:00"
    assert marketplace_sources.date_text("26-10-07 21:44") == expected
    result = marketplace_sources.list_page(FakeContext(), "nga")
    assert result[0]["posted_at"] == expected


def test_one_source_browser_startup_failure_does_not_block_other_body_refills(tmp_path):
    _, store = make_store(tmp_path)
    store.ingest("nga", [{
        "id": "61",
        "title": "出 iPhone 15 Pro",
        "url": "https://bbs.nga.cn/read.php?tid=61",
        "body_complete": False,
    }])
    store.ingest("chiphell", [{
        "id": "62",
        "title": "出 RTX 4070 SUPER",
        "url": "https://www.chiphell.com/thread-62-1-1.html",
        "body_complete": False,
    }])

    @contextmanager
    def browser_for_source(source):
        if source == "nga":
            raise RuntimeError("fixture browser startup failure")
        yield object(), object()

    def complete_detail(_ctx, source, post, **kwargs):
        assert source == "chiphell"
        return {
            **post,
            "body_full": "RTX 4070 SUPER 独立显卡 4200元出售",
            "body_complete": True,
        }

    with (
        patch("pylon.tasks.marketplace.collection.client", side_effect=browser_for_source),
        patch.object(sources, "detail", side_effect=complete_detail),
    ):
        errors = sources.refill(store, limit=12)

    assert any("nga" in error for error in errors)
    with store.box.connect() as db:
        rows = {
            row["source"]: row
            for row in db.execute(
                "SELECT source,body_complete,body FROM marketplace_posts"
            )
        }
    assert rows["chiphell"]["body_complete"] == 1
    assert "4070" in rows["chiphell"]["body"]
    assert rows["nga"]["body_complete"] == 0


def test_live_body_refill_stops_its_own_source_after_deadline(tmp_path, monkeypatch):
    _, store = make_store(tmp_path)
    for source_id in ("81", "82", "83"):
        store.ingest("v2ex", [{"id":source_id,"title":"出 iPhone 15 Pro",
            "url":f"https://www.v2ex.com/t/{source_id}","body_complete":False}])
    clock={"now":100.0}
    monkeypatch.setattr(sources.time,"monotonic",lambda:clock["now"])
    calls=[]
    @contextmanager
    def client(_):
        yield object(),None
    def detail(_ctx,source,post,**kwargs):
        calls.append(post["id"])
        clock["now"]+=61
        return {**post,"body_full":"iPhone 15 Pro 4800元出售","body_complete":True}
    with patch("pylon.tasks.marketplace.collection.client",side_effect=client), patch.object(sources,"detail",side_effect=detail):
        assert sources.refill(store,selected_sources=("v2ex",))==[]
    assert calls==["81","82"]
    assert [post["source_id"] for post in store.pending()]==["83"]


def test_live_queues_progress_while_nga_is_blocked_and_reuse_clients(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from pylon.tasks.marketplace import collection
    _,store=make_store(tmp_path)
    for source in ("v2ex","chiphell","nga"):
        for number in (1,2):
            store.ingest(source,[{"id":str(number),"title":"出售 iPhone 15 Pro",
                "url":f"https://example.invalid/t/{number}","body_complete":False}])
    release=threading.Event();others_finished=threading.Event()
    starts=[];calls=[]
    @contextmanager
    def client(source):
        starts.append(source)
        yield object(),object()
    def detail(_ctx,source,post,**kwargs):
        if source=="nga":
            assert release.wait(5)
        calls.append(source)
        if calls.count("v2ex")==2 and calls.count("chiphell")==2:
            others_finished.set()
        return {**post,"body_full":"iPhone 15 Pro 4800元出售","body_complete":True}
    monkeypatch.setattr(collection,"client",client)
    monkeypatch.setattr(collection,"INTERVALS",dict.fromkeys(collection.INTERVALS,0))
    monkeypatch.setattr(sources,"detail",detail)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future=executor.submit(sources.refill,store)
        try:
            assert others_finished.wait(5)
            assert not future.done()
        finally:
            release.set()
        assert future.result(timeout=5)==[]
    assert sorted(starts)==["chiphell","nga","v2ex"]
    assert store.pending()==[]
