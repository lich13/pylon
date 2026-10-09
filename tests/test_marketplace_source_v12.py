"""Forum detail identity contracts shared by the marketplace adapters."""

from __future__ import annotations

import pytest

from pylon.tasks.chiphell import monitor as chiphell_monitor
from pylon.tasks.marketplace import sources


class _Locator:
    def __init__(self, page, selector: str):
        self.page = page
        self.selector = selector
        self.first = self

    def count(self):
        if self.selector in {"#thread_subject", "#postsubject0"}:
            return int(bool(self.page.title))
        if self.selector == "#messagetext":
            return 0
        if self.selector.startswith('td[id^="postmessage_') or self.selector == "#postcontent0":
            return 1
        if self.selector == "body":
            return 1
        if self.selector == '[id^="postdate"], [id^="authorposton"]':
            return 0
        if self.selector in {"#postauthor0", '#postlist .authi a.xw1'}:
            return 1
        return 0

    def wait_for(self, **_kwargs):
        return None

    def inner_html(self):
        return self.page.body_html

    def inner_text(self, **_kwargs):
        if self.selector in {"#thread_subject", "#postsubject0"}:
            return self.page.title
        return self.page.body_html

    def all_text_contents(self):
        return []

    def get_attribute(self, name):
        if name == "href" and self.selector == "#postauthor0":
            return "https://bbs.nga.cn/nuke.php?__lib=ucenter&__act=profile&uid=77"
        if name == "href" and self.selector == '#postlist .authi a.xw1':
            return "home.php?mod=space&uid=88"
        return ""

    def evaluate(self, _script):
        return ""


class _Page:
    def __init__(self, url: str, *, title: str, body_html: str, final_url: str | None = None):
        self.url = url
        self.final_url = final_url or url
        self.title = title
        self.body_html = body_html
        self.goto_calls = []
        self.closed = False

    def goto(self, url, **_kwargs):
        self.goto_calls.append(url)
        self.url = self.final_url
        return None

    def locator(self, selector):
        return _Locator(self, selector)

    def get_by_text(self, _text, *, exact=False):
        assert exact is True
        return _Locator(self, "__ad__")

    def close(self):
        self.closed = True


class _Context:
    def __init__(self, page):
        self.page = page

    def new_page(self):
        return self.page


@pytest.mark.parametrize(
    ("source", "url", "heading"),
    [
        ("chiphell", "https://www.chiphell.com/thread-1201-1-1.html", "CHH 交易帖"),
        ("nga", "https://bbs.nga.cn/read.php?tid=1202", "NGA 交易帖"),
    ],
)
def test_detail_requires_final_url_identity_and_real_heading_container(
    monkeypatch, source, url, heading,
):
    page = _Page(url, title=heading, body_html="RTX 3070 1800元出售")
    if source == "chiphell":
        calls = []

        def fields(_page, limit, **kwargs):
            calls.append((limit, kwargs))
            return {"body_full": "RTX 3070 1800元出售", "body_complete": True}

        monkeypatch.setattr(chiphell_monitor, "post_body_fields", fields)
    post = {
        "id": url.split("tid=")[-1] if "tid=" in url else "1201",
        "title": heading,
        "url": url,
        "posted_at": "",
        "body_full": "",
        "body_complete": False,
    }

    result = sources.detail(_Context(page), source, post)

    assert result["title"] == heading
    assert result["body_complete"] is True
    assert result["body_provenance"]["topic_id"] in {"1201", "1202"}
    assert result["body_provenance"]["identity_verified"] is True
    if source == "chiphell":
        assert calls == [(260, {"expected_url": url})]


@pytest.mark.parametrize(
    ("source", "url", "redirect"),
    [
        (
            "chiphell",
            "https://www.chiphell.com/thread-1203-1-1.html",
            "https://www.chiphell.com/thread-1204-1-1.html",
        ),
        (
            "nga",
            "https://bbs.nga.cn/read.php?tid=1205",
            "https://bbs.nga.cn/read.php?tid=1206",
        ),
    ],
)
def test_detail_rejects_a_same_site_redirect_to_another_topic(source, url, redirect):
    page = _Page(redirect, title="错误主题", body_html="别的主题正文", final_url=redirect)
    post = {
        "id": "1203",
        "title": "原主题",
        "url": url,
        "posted_at": "",
        "body_full": "",
        "body_complete": False,
    }

    with pytest.raises(sources.ContentUnavailable, match="unexpected_topic_redirect"):
        sources.detail(_Context(page), source, post)


@pytest.mark.parametrize(
    ("source", "url"),
    [
        ("chiphell", "https://www.chiphell.com/thread-1207-1-1.html"),
        ("nga", "https://bbs.nga.cn/read.php?tid=1208"),
    ],
)
def test_detail_without_topic_heading_is_content_unavailable(source, url):
    page = _Page(url, title="", body_html="正文存在但没有主题标题")
    post = {
        "id": "1207",
        "title": "采集列表标题",
        "url": url,
        "posted_at": "",
        "body_full": "",
        "body_complete": False,
    }

    with pytest.raises(sources.ContentUnavailable, match="topic_identity_missing"):
        sources.detail(_Context(page), source, post)
