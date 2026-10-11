"""Exercise the daily review workspace with an isolated database and browser."""
from __future__ import annotations

import json
import os
import re
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Shanghai")
BASE_ROWS = 126
DENSE_ROWS = 1100
TOTAL_ROWS = BASE_ROWS + DENSE_ROWS
DAY_A = "2026-10-11"
DAY_B = "2026-10-10"
SOURCES = ("linuxdo", "v2ex", "chiphell", "nga", "xianbao")
STATUSES = ("delivered", "excluded", "needs_info", "duplicate", "pending", "failed")
CATEGORIES = ("fixture-a", "fixture-b")


def _iso(day: str, index: int) -> str:
    hour = 8 + index % 10
    minute = (index * 7) % 60
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=ZONE).isoformat()


def seed_fixture(box, *, total=TOTAL_ROWS):
    """Create public-looking review inputs without network or notification sends."""
    from pylon.tasks.daily_review import store as review_store

    fixture = []
    with box.connect() as db:
        review_store.initialize(db)
        for index in range(total):
            # Keep the named behavior fixtures on two days and add a large excluded
            # archive so windowing and cursor continuation are exercised in CI.
            day = DAY_A if index < BASE_ROWS // 2 else DAY_B
            source = SOURCES[index % len(SOURCES)]
            lane = "market" if index % 3 == 0 else "intel"
            source_id = f"fixture-{index:03d}"
            title = f"审核样本 {index:03d} · {source}"
            body = (
                f"这是一条 example.invalid 的合成原文 {index:03d}，用于验证日期、来源、"
                f"状态、分类和全文搜索。needle-{index:03d} 只存在于这一条记录。"
            )
            if index == 0:
                title = "历史通知样本 · example.invalid"
                body = "历史通知样本 body，包含 history-keyword 与可展开的原文依据。"
            if index == 1:
                title = '<script>window.__review_pwned=1</script> <img src=x onerror="window.__review_pwned=2">'
                body = "恶意标题 fixture body：<script>window.__review_pwned=3</script>"
            if index == 2:
                title = "逐条纠正样本 · example.invalid"
            if index == 3:
                title = "逐商品样本 · example.invalid"
            if index in (4, 5):
                title = f"批量反馈样本 {index - 3} · example.invalid"

            category = CATEGORIES[index % len(CATEGORIES)]
            published_at = _iso(day, index)
            metadata = {"fixture": True, "index": index}
            snapshot_id = review_store.capture(
                db,
                source,
                source_id,
                lane=lane,
                title=title,
                body=body,
                url=f"https://example.invalid/review/{source_id}",
                published_at=published_at,
                first_seen=published_at,
                evidence_scope="forum_body" if lane == "market" else "helper_excerpt",
                metadata=metadata,
                fingerprint=f"fixture-fingerprint-{index}",
            )
            items = []
            if lane == "market":
                items = [
                    {
                        "item": {
                            "name": f"商品 {index:03d}-A",
                            "price": str(30 + index),
                            "category": category,
                            "condition": "北京自取",
                        },
                        "reasons": ["fixture product"],
                    },
                    {
                        "item": {
                            "name": f"商品 {index:03d}-B",
                            "price": str(60 + index),
                            "category": CATEGORIES[(index + 1) % len(CATEGORIES)],
                            "condition": "全国邮寄",
                        },
                        "reasons": ["second fixture product"],
                    },
                ]
            categories = [category]
            for row in items:
                item_category = row["item"]["category"]
                if item_category not in categories:
                    categories.append(item_category)
            result = {
                "decision": "push" if index % 2 == 0 else "hold",
                "categories": categories,
                "facts": [{
                    "category": category,
                    "kind": "fixture",
                    "subject": title,
                    "value": f"合成摘要 {index:03d}，用于验证紧凑列表。",
                }],
                "items": items,
                "reason": f"fixture decision {index:03d}",
            }
            status = "excluded" if index >= BASE_ROWS else STATUSES[index % len(STATUSES)]
            event_id = None
            if index == 0:
                review_store.record(
                    db,
                    source,
                    source_id,
                    lane=lane,
                    status="needs_info",
                    result={**result, "reason": "历史第一条判断"},
                    rule_version="fixture-history-rule-1",
                    snapshot_id=snapshot_id,
                    decided_at=time.time() - 120,
                )
                event_id = "fixture-review-notification"
                db.execute(
                    """INSERT INTO events
                       (id,profile,job_id,kind,payload,status,attempts,next_attempt,created_at,updated_at)
                       VALUES(?,?,?,?,?,'delivered',1,0,?,?)""",
                    (
                        event_id,
                        "webops",
                        "fixture-review",
                        "text",
                        json.dumps({"text": "fixture notification delivered"}, ensure_ascii=False),
                        time.time() - 100,
                        time.time() - 20,
                    ),
                )
                review_store.record(
                    db,
                    source,
                    source_id,
                    lane=lane,
                    status="delivered",
                    result={**result, "reason": "历史最终判断"},
                    rule_version="fixture-final-rule-2",
                    event_id=event_id,
                    snapshot_id=snapshot_id,
                    decided_at=time.time() - 60,
                )
            else:
                review_store.record(
                    db,
                    source,
                    source_id,
                    lane=lane,
                    status=status,
                    result=result,
                    rule_version=f"fixture-rule-{index % 4}",
                    snapshot_id=snapshot_id,
                )
            fixture.append(
                {
                    "index": index,
                    "id": review_store.digest([source, source_id])[:24],
                    "source": source,
                    "source_id": source_id,
                    "lane": lane,
                    "day": day,
                    "status": "delivered" if index == 0 else status,
                    "category": category,
                    "categories": categories,
                    "title": title,
                    "body": body,
                }
            )
    return fixture


def bump_fixture_version(box, row, *, version="fixture-v2"):
    """Capture a newer content version so submitted feedback becomes stale."""
    from pylon.tasks.daily_review import store as review_store

    with box.connect() as db:
        snapshot_id = review_store.capture(
            db,
            row["source"],
            row["source_id"],
            lane=row["lane"],
            title=row["title"],
            body=f"{row['body']} 字段版本 {version}。",
            url=f"https://example.invalid/review/{row['source_id']}",
            published_at=_iso(row["day"], row["index"]),
            first_seen=_iso(row["day"], row["index"]),
            evidence_scope="forum_body" if row["lane"] == "market" else "helper_excerpt",
            metadata={"fixture": True, "index": row["index"], "field_version": version},
            fingerprint=f"fixture-fingerprint-{row['index']}-{version}",
        )
    assert snapshot_id
    return snapshot_id


@contextmanager
def running_app(store):
    """Run the same app instance on a disposable loopback port."""
    from pylon.tasks.marketplace import web
    import uvicorn

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(web.create_app(store, secure=False), log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("daily review web fixture did not start")
        time.sleep(0.05)
    try:
        yield origin
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        if thread.is_alive():
            raise RuntimeError("daily review web fixture did not stop")


def login_token(box):
    from pylon.tasks.marketplace import web_auth

    with box.connect() as db:
        return web_auth.issue_login(db)


def response_for(path, method=None):
    def predicate(response):
        return response.url.startswith(path) and (method is None or response.request.method == method)

    return predicate


def submit_filters(page, origin, *, day="", source="", status="", category="", query="", review=None):
    # The workspace is shown before its initial browse completes. Do not let
    # that response satisfy the next filter request's response expectation.
    page.wait_for_function(
        "() => !!JSON.parse(sessionStorage.getItem('pylon-review-position-v3') || 'null')?.view"
    )
    values = {"day": day, "source": source, "status": status, "category": category, "q": query}
    if review is not None:
        values["review"] = review
    page.evaluate(
        """values => {
            for (const [id, value] of Object.entries(values)) {
                const node = document.querySelector('#' + id);
                if (node) node.value = value;
            }
        }""",
        values,
    )
    def matches_filters(response):
        if not response_for(origin + "/pylon/review/api/browse", "POST")(response):
            return False
        submitted = response.request.post_data_json
        return all(submitted.get(key, "") == value for key, value in values.items())

    with page.expect_response(matches_filters) as response:
        page.locator("#filters button[type=submit]").click()
    assert response.value.status == 200, response.value.text()
    payload = response.value.json()
    page.wait_for_function(
        "view => JSON.parse(sessionStorage.getItem('pylon-review-position-v3') || 'null')?.view === view",
        arg=payload["view_id"],
    )
    return payload


def feed_response(origin, section="main", *, view_id=None, cursor=None):
    def predicate(response):
        query = parse_qs(urlsplit(response.url).query)
        return (
            response.url.startswith(origin + "/pylon/review/api/feed?")
            and response.request.method == "GET"
            and query.get("section") == [section]
            and (view_id is None or query.get("view") == [view_id])
            and (cursor is None or query.get("cursor") == [cursor])
        )

    return predicate


def load_next_stream(page, origin, *, section="main", view_id=None, cursor=None):
    """Trigger one cursor continuation through the real stream sentinel."""
    with page.expect_response(feed_response(origin, section, view_id=view_id, cursor=cursor)) as response:
        page.locator(f"[data-sentinel='{section}']").scroll_into_view_if_needed()
        page.wait_for_timeout(250)
    assert response.value.status == 200, response.value.text()
    page.wait_for_timeout(250)
    payload = response.value.json()
    assert 0 < len(payload.get("items", [])) <= 50, payload
    return payload


def count_from_label(page, selector):
    text = page.locator(selector).inner_text()
    match = re.search(r"(\d+)", text)
    assert match, (selector, text)
    return int(match.group(1))


def scroll_to_bottom(page):
    """Move the main stream sentinel into view for cursor continuation."""
    page.locator("[data-sentinel='main']").scroll_into_view_if_needed()
    page.wait_for_timeout(150)


def assert_review_viewport(page):
    """Ensure the windowed main stream remains readable and bounded."""
    layout = page.evaluate(
        """() => {
            const toolbar = document.querySelector('.review-toolbar')?.getBoundingClientRect();
            const table = document.querySelector('.table-wrap')?.getBoundingClientRect();
            const visible = [...document.querySelectorAll('#feed tr.record-row')]
                .map(row => ({box: row.getBoundingClientRect(), title: row.querySelector('.title-toggle')?.innerText || ''}))
                .filter(({box}) => box.bottom > 0 && box.top < innerHeight);
            const row = visible[0]?.box;
            const title = visible[0]?.title || '';
            return {
                scrollY: window.scrollY,
                viewportHeight: window.innerHeight,
                toolbarTop: toolbar?.top,
                toolbarBottom: toolbar?.bottom,
                tableTop: table?.top,
                rowTop: row?.top,
                rowBottom: row?.bottom,
                title,
                rows: document.querySelectorAll('#feed tr.record-row').length,
                visibleRows: visible.length,
            };
        }"""
    )
    assert layout["rows"] <= 120, layout
    assert layout["visibleRows"] > 0 and layout["rowTop"] < layout["viewportHeight"] and layout["rowBottom"] > 0, layout
    assert layout["title"].strip(), layout


def main_row_ids(page):
    return page.locator("#feed tr.record-row").evaluate_all("nodes => nodes.map(node => node.dataset.record)")


def wait_main_rows(page, minimum):
    page.wait_for_function(
        """minimum => {
            try {
                const saved = JSON.parse(sessionStorage.getItem('pylon-review-position-v3') || 'null');
                return Number(saved?.streams?.main?.count || 0) >= minimum;
            } catch {
                return false;
            }
        }""",
        arg=minimum,
        timeout=15000,
    )


def capture_main_anchor(page):
    """Move to a middle row and capture its reading position before refresh."""
    page.locator("#main-stream").evaluate(
        "node => window.scrollTo(0, scrollY + node.getBoundingClientRect().top + node.offsetHeight / 2 - innerHeight / 2)"
    )
    page.wait_for_function(
        """() => {
            const toolbar = document.querySelector('.review-toolbar').getBoundingClientRect();
            return scrollY > 1000 && [...document.querySelectorAll('#feed tr.record-row')].some(row => {
                const box = row.getBoundingClientRect();
                return box.top >= toolbar.bottom && box.bottom < innerHeight;
            });
        }"""
    )
    anchor_id = page.locator("#feed tr.record-row").evaluate_all(
        """rows => rows.find(row => {
            const box = row.getBoundingClientRect();
            const toolbar = document.querySelector('.review-toolbar').getBoundingClientRect();
            return box.top >= toolbar.bottom && box.bottom < innerHeight;
        }).dataset.record"""
    )
    assert anchor_id
    page.wait_for_function(
        """id => {
            const row = [...document.querySelectorAll('#feed tr.record-row')]
                .find(node => node.dataset.record === id);
            const toolbar = document.querySelector('.review-toolbar')?.getBoundingClientRect();
            if (!row || !toolbar) return false;
            const box = row.getBoundingClientRect();
            return box.bottom > 0 && box.top < innerHeight && toolbar.bottom > 0 && toolbar.top < innerHeight;
        }""",
        arg=anchor_id,
        timeout=15000,
    )
    position = page.evaluate(
        """id => {
            const row = [...document.querySelectorAll('#feed tr.record-row')]
                .find(node => node.dataset.record === id);
            const box = row.getBoundingClientRect();
            const toolbar = document.querySelector('.review-toolbar').getBoundingClientRect();
            return {
                anchorTop: box.top,
                anchorBottom: box.bottom,
                scrollY,
                toolbarTop: toolbar.top,
                toolbarBottom: toolbar.bottom,
                viewportHeight: innerHeight,
            };
        }""",
        anchor_id,
    )
    assert position["toolbarTop"] >= -1 and position["toolbarBottom"] <= position["viewportHeight"] + 1, position
    assert position["anchorBottom"] > 0 and position["anchorTop"] < position["viewportHeight"], position
    return {"id": anchor_id, **position}


def wait_refresh_anchor(page, anchor, *, view_id, minimum_rows=100):
    """Wait for browse bootstrap, cursor restoration, and the saved anchor to settle."""
    page.wait_for_function(
        """({id, top, y, minimum, view}) => {
            const workspace = document.querySelector('#workspace');
            const rows = [...document.querySelectorAll('#feed tr.record-row')];
            const row = rows.find(node => node.dataset.record === id);
            const toolbar = document.querySelector('.review-toolbar')?.getBoundingClientRect();
            let loaded = 0;
            try {
                const saved = JSON.parse(sessionStorage.getItem('pylon-review-position-v3') || 'null');
                if (saved?.view !== view) return false;
                loaded = Number(saved?.streams?.main?.count || 0);
            } catch {}
            if (!workspace || workspace.hidden || loaded < minimum || !row || !toolbar) return false;
            const ready = typeof restoring === 'undefined' || restoring === false;
            const box = row.getBoundingClientRect();
            return ready && box.bottom > 0 && box.top < innerHeight
                && Math.abs(box.top - top) <= 6 && Math.abs(scrollY - y) <= 6
                && toolbar.top >= -1 && toolbar.bottom <= innerHeight + 1;
        }""",
        arg={"id": anchor["id"], "top": anchor["anchorTop"], "y": anchor["scrollY"], "minimum": minimum_rows, "view": view_id},
        timeout=15000,
    )
    position = page.evaluate(
        """id => {
            const row = [...document.querySelectorAll('#feed tr.record-row')]
                .find(node => node.dataset.record === id);
            const box = row.getBoundingClientRect();
            const toolbar = document.querySelector('.review-toolbar').getBoundingClientRect();
            return {
                anchorTop: box.top,
                anchorBottom: box.bottom,
                scrollY,
                toolbarTop: toolbar.top,
                toolbarBottom: toolbar.bottom,
                viewportHeight: innerHeight,
            };
        }""",
        anchor["id"],
    )
    assert abs(position["anchorTop"] - anchor["anchorTop"]) <= 6, (anchor, position)
    assert abs(position["scrollY"] - anchor["scrollY"]) <= 6, (anchor, position)
    assert position["toolbarTop"] >= -1 and position["toolbarBottom"] <= position["viewportHeight"] + 1, position
    assert position["anchorBottom"] > 0 and position["anchorTop"] < position["viewportHeight"], position


def wait_feedback_save(page, origin, action):
    with page.expect_response(response_for(origin + "/pylon/review/api/feedback", "POST")) as response:
        action()
        page.wait_for_timeout(700)
    assert response.value.status == 200, response.value.text()


def open_action(page, origin, selector):
    page.locator(selector).first.click()
    page.locator("#feedback-dialog").wait_for(state="visible")
    page.wait_for_timeout(650)
    # The action button itself is an auto-save; keep this assertion separate
    # so the subsequent combined correction has its own request boundary.
    state = page.request.get(origin + "/pylon/review/api/state")
    assert state.status == 200, state.text()


def assert_draft(page, origin, *, entry_id, item_key, actions, scope, scope_value):
    payload = page.request.get(origin + "/pylon/review/api/state")
    assert payload.status == 200, payload.text()
    drafts = payload.json()["drafts"]
    draft = next(d for d in drafts if d["entry_id"] == entry_id and d["item_key"] == item_key)
    value = draft["value"]
    assert set(actions).issubset(value["actions"]), value
    assert value["scope"] == scope, value
    assert value["scope_value"] == scope_value, value
    return draft


def check_navigation(page, origin):
    from playwright.sync_api import expect

    page.get_by_role("link", name="情报筛选", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    assert page.url.startswith(origin + "/pylon/intel/")
    page.get_by_role("link", name="交易筛选", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    assert page.url.startswith(origin + "/pylon/market/")
    page.get_by_role("link", name="每日审核", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    assert page.url.startswith(origin + "/pylon/review/")


def first_unrelated_row(page, excluded):
    """Choose a visible main-stream row while keeping named fixtures independent."""
    candidates = page.locator("#feed tr.record-row").evaluate_all(
        """nodes => nodes.map(node => {
            const box = node.getBoundingClientRect();
            return {id: node.dataset.record, visible: node.offsetParent !== null && box.bottom > 0 && box.top < innerHeight};
        }).filter(candidate => candidate.visible && candidate.id)"""
    )
    for candidate in candidates:
        record_id = candidate["id"]
        if record_id not in excluded:
            row = page.locator(f"#feed tr.record-row[data-record='{record_id}']")
            row.wait_for(state="visible")
            return row
    raise AssertionError("no unrelated review row available for selection")


def open_group(page, origin, key, *, return_payload=False):
    group = page.locator(f"details[data-group='{key}']")
    payload = None
    if group.get_attribute("open") is None:
        with page.expect_response(feed_response(origin, key)) as response:
            group.locator("summary").click()
        assert response.value.status == 200, response.value.text()
        payload = response.value.json()
    group.locator(f"tbody#feed-{key}").wait_for(state="visible")
    return (group, payload) if return_payload else group


def recall_group_row(page, origin, row, *, key):
    """Recall a dense group row so existing feedback assertions can use #feed."""
    entry_id = row.get_attribute("data-record")
    assert entry_id
    with page.expect_response(response_for(origin + "/pylon/review/api/recall", "POST")) as recall:
        with page.expect_response(response_for(origin + "/pylon/review/api/browse", "POST")) as browse:
            row.locator(f"[data-recall='{entry_id}']").click()
    assert recall.value.status == 200, recall.value.text()
    assert browse.value.status == 200, browse.value.text()
    page.locator(f"#feed tr.record-row[data-record='{entry_id}']").wait_for(state="visible")
    return page.locator(f"#feed tr.record-row[data-record='{entry_id}']")


def ensure_main_fixture(page, origin, row, *, group):
    """Locate a fixture in the main stream, recalling it from its archive group when needed."""
    main_row = page.locator(f"#feed tr.record-row[data-record='{row['id']}']")
    if main_row.count():
        return main_row
    group_node = open_group(page, origin, group)
    dense = group_node.locator(f"tbody#feed-{group} tr.record-row[data-record='{row['id']}']")
    dense.wait_for(state="visible")
    return recall_group_row(page, origin, dense, key=group)


def assert_group_contract(page):
    from playwright.sync_api import expect

    groups = page.locator("#archive-groups details[data-group]")
    assert groups.count() == 4
    for key in ("other", "excluded", "unresolved", "duplicate"):
        group = page.locator(f"#archive-groups details[data-group='{key}']")
        expect(group.locator("summary")).to_be_visible()
        assert group.locator("summary .sample-titles > span").count() <= 3
        assert group.locator(f"form[data-group-filter='{key}']").count() == 1
        assert group.locator(f".group-scroll[data-scroll='{key}']").count() == 1
        assert group.locator(f"tbody#feed-{key} tr.record-row").count() == 0


def main():
    with tempfile.TemporaryDirectory(prefix="pylon-daily-review-web-") as directory:
        root = Path(directory)
        os.environ.update(
            PYLON_HOME=str(root),
            PYLON_OUTBOX=str(root / "outbox.sqlite3"),
            PYLON_PROFILE_ROOT=str(root / "profiles/webops"),
        )
        from pylon.outbox import Outbox
        from pylon.tasks.intelligence.store import Store as IntelStore
        from pylon.tasks.marketplace.store import Store
        from playwright.sync_api import expect, sync_playwright

        box = Outbox()
        store = Store(box)
        IntelStore(box)
        fixture = seed_fixture(box)
        by_title = {row["title"]: row for row in fixture}
        single = by_title["逐条纠正样本 · example.invalid"]
        product = by_title["逐商品样本 · example.invalid"]
        bulk = [by_title[f"批量反馈样本 {index} · example.invalid"] for index in (1, 2)]
        history = by_title["历史通知样本 · example.invalid"]
        malicious = next(row for row in fixture if row["index"] == 1)
        initial_events = box.counts().get("delivered", 0)
        blocked = []
        errors = []
        expected_feed_abort = {"value": False}

        def hook(page, origin):
            def route(route):
                url = route.request.url
                if url.startswith(origin + "/"):
                    route.continue_()
                else:
                    blocked.append(url)
                    route.abort()

            page.route("**/*", route)
            page.on("pageerror", lambda error: errors.append(f"pageerror: {error}"))

            def collect_console_error(message):
                if message.type != "error":
                    return
                if expected_feed_abort["value"] and ("ERR_FAILED" in message.text or "Failed to load resource" in message.text):
                    expected_feed_abort["value"] = False
                    return
                errors.append(f"console: {message.text}")

            page.on("console", collect_console_error)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                with running_app(store) as origin:
                    desktop = browser.new_context(viewport={"width": 1360, "height": 960})
                    page = desktop.new_page()
                    hook(page, origin)
                    page.goto(origin + "/pylon/review/#login=" + login_token(box))
                    expect(page.locator("#workspace")).to_be_visible()
                    expect(page.locator("#login")).to_be_hidden()
                    assert "#" not in page.url
                    expect(page.locator(".workspaces a")).to_have_count(3)
                    check_navigation(page, origin)

                    # The layered browse view removes page controls and opens with 50 main candidates.
                    expect(page.locator("#review")).to_have_value("pending")
                    for obsolete in ("#page_size", "#page-label", "#prev", "#next"):
                        expect(page.locator(obsolete)).to_have_count(0)
                    initial_browse = submit_filters(page, origin, day="")
                    assert len(initial_browse["items"]) == 50, initial_browse
                    assert 0 < page.locator("#feed tr.record-row").count() <= 120
                    expect(page.locator("#main-count")).to_contain_text("主列表")
                    expect(page.locator("#stored-count")).to_contain_text("已收纳")
                    expect(page.locator("#count")).to_contain_text("全部")
                    assert count_from_label(page, "#main-count") >= 100
                    assert count_from_label(page, "#count") == TOTAL_ROWS
                    assert_group_contract(page)

                    # The excluded archive carries a large synthetic fixture; its DOM stays windowed
                    # while its own scroll container follows an independent cursor.
                    excluded_group, excluded_initial = open_group(page, origin, "excluded", return_payload=True)
                    assert count_from_label(page, "#archive-groups details[data-group='excluded'] summary") >= 1000
                    excluded_scroll = excluded_group.locator(".group-scroll[data-scroll='excluded']")
                    excluded_group.locator("summary").scroll_into_view_if_needed()
                    dense_ids = {item["id"] for item in excluded_initial["items"]}
                    dense_cursors = {excluded_initial["next_cursor"]}
                    assert len(dense_ids) == len(excluded_initial["items"]) == 50
                    while len(dense_ids) < 1000:
                        with page.expect_response(feed_response(origin, "excluded")) as excluded_response:
                            excluded_scroll.evaluate("node => node.scrollTop = node.scrollHeight")
                            page.wait_for_timeout(250)
                        assert excluded_response.value.status == 200, excluded_response.value.text()
                        excluded_page = excluded_response.value.json()
                        page_ids = {item["id"] for item in excluded_page["items"]}
                        assert len(page_ids) == len(excluded_page["items"]) <= 50
                        assert dense_ids.isdisjoint(page_ids), (len(dense_ids), page_ids & dense_ids)
                        dense_ids.update(page_ids)
                        if excluded_page["next_cursor"] is None:
                            break
                        assert excluded_page["next_cursor"] not in dense_cursors
                        dense_cursors.add(excluded_page["next_cursor"])
                        assert excluded_group.locator("tbody#feed-excluded tr.record-row").count() <= 120
                    assert len(dense_ids) >= 1000
                    assert 0 < excluded_group.locator("tbody#feed-excluded tr.record-row").count() <= 120
                    assert excluded_scroll.evaluate("node => node.scrollTop") > 0
                    assert excluded_scroll.evaluate("node => node.scrollHeight > node.clientHeight")
                    excluded_filter = excluded_group.locator("form[data-group-filter='excluded']")
                    excluded_filter.locator("select[name=category]").select_option("fixture-a")
                    with page.expect_response(feed_response(origin, "excluded")) as filtered_excluded:
                        excluded_filter.locator("button").click()
                    assert "category=fixture-a" in filtered_excluded.value.url
                    assert filtered_excluded.value.status == 200, filtered_excluded.value.text()
                    assert excluded_group.locator("tbody#feed-excluded tr.record-row").count() <= 120

                    # Refreshing the browse view keeps the open dense group and its local reading offset.
                    excluded_group.locator("summary").scroll_into_view_if_needed()
                    saved_group_top = excluded_scroll.evaluate("node => node.scrollTop")
                    with page.expect_response(response_for(origin + "/pylon/review/api/browse", "POST")) as refreshed:
                        page.locator("#refresh-list").click()
                    assert refreshed.value.status == 200, refreshed.value.text()
                    refreshed_group = page.locator("details[data-group='excluded']")
                    page.wait_for_function(
                        """({selector, target}) => {
                            const group = document.querySelector(selector);
                            const scroll = group?.querySelector('.group-scroll');
                            return group?.open === true && scroll && Math.abs(scroll.scrollTop - target) <= 2;
                        }""",
                        arg={"selector": "details[data-group='excluded']", "target": saved_group_top},
                        timeout=15000,
                    )

                    assert all(
                        "这是一条 example.invalid 的合成原文" not in text
                        for text in page.locator("#feed tr.record-row").all_inner_texts()
                    )
                    statuses = page.locator("#feed tr.record-row .status").evaluate_all(
                        "nodes => nodes.map(node => node.className.includes('delivered'))"
                    )
                    first_pending = next((index for index, delivered in enumerate(statuses) if not delivered), len(statuses))
                    assert all(statuses[index] for index in range(first_pending))
                    assert not any(statuses[index] for index in range(first_pending, len(statuses)))
                    assert_review_viewport(page)

                    # Dense archive scrolling can also advance the main observer. Start the
                    # selection/continuation check from a fresh top-level view and cursor.
                    main_browse = submit_filters(page, origin, day="")
                    assert len(main_browse["items"]) == 50, main_browse
                    assert_review_viewport(page)

                    # Selection survives a cursor continuation; the stream loads another 50 rows automatically.
                    excluded = {single["id"], product["id"], history["id"], *(row["id"] for row in bulk)}
                    first_page_ids = set(page.locator("#feed tr.record-row").evaluate_all("nodes => nodes.map(n => n.dataset.record)"))
                    first_selected = first_unrelated_row(page, excluded)
                    first_selected_id = first_selected.get_attribute("data-record")
                    first_selected.locator("[data-select]").check()
                    continuation = load_next_stream(page, origin, view_id=main_browse["view_id"], cursor=main_browse["next_cursor"])
                    assert continuation["next_cursor"] != main_browse["next_cursor"], continuation
                    assert page.locator("#feed tr.record-row").count() <= 120
                    second_selected = first_unrelated_row(page, excluded | {first_selected_id})
                    second_selected_id = second_selected.get_attribute("data-record")
                    assert first_selected_id and second_selected_id and first_selected_id != second_selected_id
                    assert second_selected_id not in first_page_ids or second_selected_id != first_selected_id
                    second_selected.locator("[data-select]").check()
                    expect(page.locator("#selection-count")).to_have_text("已选 2 条")
                    with page.expect_response(response_for(origin + "/pylon/review/api/feedback", "POST")):
                        page.locator("#bulk-correct").click()
                    expect(page.locator("#draft-count")).to_have_text("2")
                    state = page.request.get(origin + "/pylon/review/api/state").json()
                    assert {draft["entry_id"] for draft in state["drafts"]} == {first_selected_id, second_selected_id}
                    page.locator("#clear-selection").click()

                    # Refresh from the middle of a two-page stream through the visible sticky toolbar.
                    desktop_second_ids = {item["id"] for item in continuation["items"]}
                    assert len(desktop_second_ids) == 50
                    assert first_page_ids.isdisjoint(desktop_second_ids)
                    wait_main_rows(page, 100)
                    desktop_loaded_ids = main_row_ids(page)
                    assert desktop_loaded_ids
                    assert len(desktop_loaded_ids) == len(set(desktop_loaded_ids)), desktop_loaded_ids
                    desktop_anchor = capture_main_anchor(page)
                    refresh = page.locator("#refresh-list")
                    expect(refresh).to_be_visible()
                    refresh_box = refresh.bounding_box()
                    assert refresh_box and 0 <= refresh_box["y"] < 960, refresh_box
                    with page.expect_response(response_for(origin + "/pylon/review/api/browse", "POST")) as refreshed:
                        refresh.click()
                    assert refreshed.value.status == 200, refreshed.value.text()
                    wait_refresh_anchor(page, desktop_anchor, view_id=refreshed.value.json()["view_id"])
                    desktop_refreshed_ids = main_row_ids(page)
                    assert desktop_refreshed_ids
                    assert len(desktop_refreshed_ids) == len(set(desktop_refreshed_ids)), desktop_refreshed_ids
                    assert desktop_anchor["id"] in desktop_refreshed_ids

                    # Repeat the same user path on a fresh 390px context and require a full second page.
                    refresh_mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                    refresh_phone = refresh_mobile.new_page()
                    hook(refresh_phone, origin)
                    refresh_phone.goto(origin + "/pylon/review/#login=" + login_token(box))
                    expect(refresh_phone.locator("#workspace")).to_be_visible()
                    mobile_browse = submit_filters(refresh_phone, origin, day="")
                    assert len(mobile_browse["items"]) == 50, mobile_browse
                    mobile_first_ids = {item["id"] for item in mobile_browse["items"]}
                    assert len(mobile_first_ids) == 50
                    mobile_continuation = load_next_stream(
                        refresh_phone, origin, view_id=mobile_browse["view_id"], cursor=mobile_browse["next_cursor"]
                    )
                    mobile_second_ids = {item["id"] for item in mobile_continuation["items"]}
                    assert len(mobile_second_ids) == 50
                    assert mobile_first_ids.isdisjoint(mobile_second_ids)
                    wait_main_rows(refresh_phone, 100)
                    mobile_loaded_ids = main_row_ids(refresh_phone)
                    assert mobile_loaded_ids
                    assert len(mobile_loaded_ids) == len(set(mobile_loaded_ids)), mobile_loaded_ids
                    assert refresh_phone.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    mobile_anchor = capture_main_anchor(refresh_phone)
                    mobile_refresh = refresh_phone.locator("#refresh-list")
                    expect(mobile_refresh).to_be_visible()
                    mobile_refresh_box = mobile_refresh.bounding_box()
                    assert mobile_refresh_box and 0 <= mobile_refresh_box["y"] < 844, mobile_refresh_box
                    with refresh_phone.expect_response(response_for(origin + "/pylon/review/api/browse", "POST")) as mobile_refreshed:
                        mobile_refresh.click()
                    assert mobile_refreshed.value.status == 200, mobile_refreshed.value.text()
                    wait_refresh_anchor(refresh_phone, mobile_anchor, view_id=mobile_refreshed.value.json()["view_id"])
                    mobile_refreshed_ids = main_row_ids(refresh_phone)
                    assert mobile_refreshed_ids
                    assert len(mobile_refreshed_ids) == len(set(mobile_refreshed_ids)), mobile_refreshed_ids
                    assert mobile_anchor["id"] in mobile_refreshed_ids
                    assert refresh_phone.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    refresh_phone.close()
                    refresh_mobile.close()

                    # A failed continuation exposes a retry control and succeeds without discarding loaded rows.
                    failed = {"value": True}

                    def fail_once(route):
                        if failed["value"] and "section=main" in route.request.url:
                            failed["value"] = False
                            expected_feed_abort["value"] = True
                            route.abort("failed")
                        else:
                            route.continue_()

                    page.route("**/pylon/review/api/feed**", fail_once)
                    scroll_to_bottom(page)
                    expect(page.locator("[data-load-state='main']")).to_have_text("加载未完成")
                    expect(page.locator("[data-retry='main']")).to_be_visible()
                    expected_feed_abort["value"] = False
                    page.unroute("**/pylon/review/api/feed**", fail_once)
                    with page.expect_response(feed_response(origin, "main")) as retry_response:
                        page.locator("[data-retry='main']").click()
                    assert retry_response.value.status == 200, retry_response.value.text()
                    assert_review_viewport(page)

                    # Date, source, status, category and full-text filters all narrow the real API result.
                    expected_day_a = sum(row["day"] == DAY_A for row in fixture)
                    submit_filters(page, origin, day=DAY_A)
                    expect(page.locator("#count")).to_have_text(f"全部 {expected_day_a}")
                    expected_source = sum(row["source"] == "linuxdo" for row in fixture)
                    submit_filters(page, origin, source="linuxdo")
                    expect(page.locator("#count")).to_have_text(f"全部 {expected_source}")
                    expected_status = sum(row["status"] == "delivered" for row in fixture)
                    submit_filters(page, origin, status="delivered")
                    expect(page.locator("#count")).to_have_text(f"全部 {expected_status}")
                    expected_category = sum("fixture-a" in row["categories"] for row in fixture)
                    submit_filters(page, origin, category="fixture-a")
                    expect(page.locator("#count")).to_have_text(f"全部 {expected_category}")
                    submit_filters(page, origin, query="needle-077")
                    expect(page.locator("#count")).to_have_text("全部 1")
                    expect(page.locator("#feed tr.record-row")).to_have_count(0)
                    unresolved = open_group(page, origin, "unresolved")
                    expect(unresolved.locator("tbody#feed-unresolved tr.record-row")).to_have_count(1)
                    expect(unresolved.locator("[data-load-state='unresolved']")).to_contain_text("已显示")

                    # Historical decisions, source snapshots, and notification text are lazy-loaded by title.
                    submit_filters(page, origin, query="history-keyword")
                    history_row = page.locator(f"tr.record-row[data-record='{history['id']}']")
                    expect(history_row).to_be_visible()
                    expect(history_row).not_to_contain_text("历史通知样本 body")
                    expect(page.locator(f"tr[data-detail='{history['id']}']")).to_be_hidden()
                    with page.expect_response(response_for(origin + f"/pylon/review/api/detail?id={history['id']}", "GET")):
                        history_row.locator("[data-expand]").click()
                    history_detail = page.locator(f"tr[data-detail='{history['id']}'] .detail-content")
                    expect(history_detail).to_contain_text("历史第一条判断")
                    expect(history_detail).to_contain_text("历史最终判断")
                    expect(history_detail).to_contain_text("fixture notification delivered")
                    expect(history_detail).to_contain_text("历史通知样本 body")

                    # Row-level combined correction with an explicit brand scope.
                    submit_filters(page, origin, query=single["title"])
                    single_row = ensure_main_fixture(page, origin, single, group="unresolved")
                    expect(single_row).to_be_visible()
                    open_action(page, origin, f"tr.record-row[data-record='{single['id']}'] [data-feedback='open']")
                    wait_feedback_save(
                        page,
                        origin,
                        lambda: (
                            page.locator("[data-action='category']").check(),
                            page.locator("[data-action='facts']").check(),
                            page.locator("#correct-category").fill("fixture-corrected"),
                            page.locator("#correct-price").fill("50 元"),
                            page.locator("#correct-region").fill("仅北京"),
                            page.locator("#reason").fill("组合纠正原因"),
                            page.locator("#scope-details summary").click(),
                            page.locator("#scope").select_option("brand"),
                            page.locator("#scope-value").fill("fixture-brand"),
                        ),
                    )
                    expect(page.locator("#feedback-save")).to_have_text("")
                    page.locator("#feedback-done").click()
                    assert_draft(
                        page,
                        origin,
                        entry_id=single["id"],
                        item_key="",
                        actions=("category", "facts"),
                        scope="brand",
                        scope_value="fixture-brand",
                    )
                    with page.expect_response(response_for(origin + "/pylon/review/api/browse-view", "GET")) as resumed:
                        page.reload()
                    assert resumed.value.status == 200, resumed.value.text()
                    expect(page.locator("#workspace")).to_be_visible()
                    expect(page.locator("#draft-count")).to_have_text("3")
                    expect(page.locator("#drafts")).to_contain_text("组合纠正原因")

                    # A second authenticated context proves the draft survives a session boundary.
                    second = browser.new_context(viewport={"width": 1360, "height": 960})
                    page2 = second.new_page()
                    hook(page2, origin)
                    page2.goto(origin + "/pylon/review/#login=" + login_token(box))
                    expect(page2.locator("#workspace")).to_be_visible()
                    expect(page2.locator("#draft-count")).to_have_text("3")

                    # Product-level feedback uses the displayed decision/item key.
                    submit_filters(page2, origin, query=product["title"])
                    product_row = ensure_main_fixture(page2, origin, product, group="duplicate")
                    expect(product_row).to_be_visible()
                    expect(product_row.locator("details")).to_have_count(0)
                    expect(product_row).not_to_contain_text("商品 003-A")
                    expect(page2.locator(f"tr[data-detail='{product['id']}']")).to_be_hidden()
                    with page2.expect_response(response_for(origin + f"/pylon/review/api/detail?id={product['id']}", "GET")):
                        product_row.locator("[data-expand]").click()
                    product_detail = page2.locator(f"tr[data-detail='{product['id']}']")
                    expect(product_detail.locator("details.products")).to_contain_text("商品")
                    product_detail.locator("details.products").click()
                    product_button = product_detail.locator("details.products [data-feedback='open']").first
                    product_item_key = product_button.get_attribute("data-item")
                    assert product_item_key and product_item_key.endswith(":0"), product_item_key
                    open_action(page2, origin, f"tr[data-detail='{product['id']}'] details.products [data-feedback='open']")
                    wait_feedback_save(
                        page2,
                        origin,
                        lambda: (
                            page2.locator("[data-action='facts']").check(),
                            page2.locator("#reason").fill("逐商品价格纠正"),
                            page2.locator("#scope-details summary").click(),
                            page2.locator("#scope").select_option("series"),
                            page2.locator("#scope-value").fill("fixture-series"),
                        ),
                    )
                    expect(page2.locator("#feedback-save")).to_have_text("")
                    page2.locator("#feedback-done").click()
                    product_draft = assert_draft(
                        page2,
                        origin,
                        entry_id=product["id"],
                        item_key=product_item_key,
                        actions=("facts",),
                        scope="series",
                        scope_value="fixture-series",
                    )
                    assert product_draft["item_key"] == product_item_key

                    # Bulk feedback selects both records on the filtered page and saves one shared opinion.
                    submit_filters(page2, origin, query="批量反馈样本")
                    expect(page2.locator("#count")).to_have_text("全部 2")
                    ensure_main_fixture(page2, origin, bulk[0], group="other")
                    ensure_main_fixture(page2, origin, bulk[1], group="unresolved")
                    expect(page2.locator("#feed tr.record-row")).to_have_count(2)
                    page2.locator("#select-page").click()
                    expect(page2.locator("#bulk-feedback")).to_be_enabled()
                    page2.locator("#bulk-feedback").click()
                    expect(page2.locator("#feedback-title")).to_contain_text("为 2 条")
                    wait_feedback_save(page2, origin, lambda: page2.locator("[data-action='should_push']").check())
                    expect(page2.locator("#feedback-save")).to_have_text("")
                    page2.locator("#feedback-done").click()
                    state = page2.request.get(origin + "/pylon/review/api/state").json()
                    assert len(state["drafts"]) == 6, state["drafts"]

                    # Submit exactly one batch, then read its frozen evidence in the UI.
                    with page2.expect_response(response_for(origin + "/pylon/review/api/submit", "POST")) as response:
                        page2.locator("#submit-batch").click()
                    assert response.value.status == 200, response.value.text()
                    expect(page2.locator("#draft-count")).to_have_text("0")
                    expect(page2.locator("#notice")).to_contain_text("已提交 6 条反馈")
                    expect(page2.locator("#batches")).to_contain_text("6 条")
                    page2.locator(".review-extras details:has(#batches) > summary").click()
                    page2.locator("#batches [data-batch]").first.click()
                    expect(page2.locator("#batch-dialog")).to_be_visible()
                    expect(page2.locator("#batch-detail")).to_contain_text("组合纠正原因")
                    expect(page2.locator("#batch-detail")).to_contain_text("逐商品价格纠正")
                    expect(page2.locator("#batch-detail .feedback-result")).to_have_count(6)
                    expect(page2.locator("#batch-detail .feedback-result .status")).to_have_count(6)
                    assert page2.locator("#batch-detail .feedback-result .status").all_inner_texts() == ["已提交"] * 6
                    page2.locator("#batch-dialog .close").click()

                    # A submitted whole-post feedback disappears from pending review but remains visible in all.
                    submit_filters(page2, origin, query=bulk[0]["title"])
                    expect(page2.locator("#count")).to_have_text("全部 0")
                    submit_filters(page2, origin, query=bulk[0]["title"], review="all")
                    expect(page2.locator("#count")).to_have_text("全部 1")
                    expect(page2.locator("#feed tr.record-row .row-hints")).to_contain_text("已提交")

                    # Capturing a newer field version makes the old feedback stale and reopens review.
                    bump_fixture_version(box, bulk[0])
                    submit_filters(page2, origin, query=bulk[0]["title"], review="pending")
                    expect(page2.locator("#count")).to_have_text("全部 1")
                    expect(page2.locator("#feed tr.record-row .row-hints")).to_contain_text("内容已更新")

                    # Unauthorized and invalid-CSRF writes are rejected before dispatch.
                    anonymous = playwright.request.new_context()
                    try:
                        assert anonymous.get(origin + "/pylon/review/api/state").status == 401
                    finally:
                        anonymous.dispose()
                    csrf = page2.request.post(
                        origin + "/pylon/review/api/logout",
                        headers={"Origin": origin, "X-CSRF-Token": "wrong-fixture-token"},
                        data={},
                    )
                    assert csrf.status == 403, csrf.text()
                    assert box.counts().get("delivered", 0) == initial_events
                    page2.close()
                    second.close()
                    page.close()
                    desktop.close()
                # Restart the same database and app, then read the submitted batch again.
                with running_app(store) as restarted_origin:
                    mobile = browser.new_context(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
                    phone = mobile.new_page()
                    hook(phone, restarted_origin)
                    phone.goto(restarted_origin + "/pylon/review/#login=" + login_token(box))
                    expect(phone.locator("#workspace")).to_be_visible()
                    expect(phone.locator("#review")).to_have_value("pending")
                    expect(phone.locator("#draft-count")).to_have_text("0")
                    expect(phone.locator("#batches")).to_contain_text("6 条")
                    expect(phone.locator(".workspaces a")).to_have_count(3)
                    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

                    # The fixed mobile action bar can complete a fresh single-record batch
                    # and the submitted evidence remains available after the restart.
                    submit_filters(phone, restarted_origin, query="history-keyword")
                    history_row = phone.locator(f"tr.record-row[data-record='{history['id']}']")
                    expect(history_row).to_be_visible()
                    expect(history_row).not_to_contain_text("历史通知样本 body")
                    expect(phone.locator(f"tr[data-detail='{history['id']}']")).to_be_hidden()
                    with phone.expect_response(response_for(restarted_origin + f"/pylon/review/api/detail?id={history['id']}", "GET")):
                        history_row.locator("[data-expand]").click()
                    expect(phone.locator(f"tr[data-detail='{history['id']}'] .detail-content")).to_contain_text("历史通知样本 body")
                    history_row.locator(f"[data-select='{history['id']}']").check()
                    expect(phone.locator("#bulk-feedback")).to_be_enabled()
                    phone.locator("#bulk-feedback").click()
                    expect(phone.locator("#feedback-dialog")).to_be_visible()
                    wait_feedback_save(phone, restarted_origin, lambda: phone.locator("[data-action='should_push']").check())
                    phone.locator("#feedback-done").click()
                    expect(phone.locator("#draft-count")).to_have_text("1")
                    expect(phone.locator("#submit-batch")).to_be_enabled()
                    with phone.expect_response(response_for(restarted_origin + "/pylon/review/api/submit", "POST")) as response:
                        phone.locator("#submit-batch").click()
                    assert response.value.status == 200, response.value.text()
                    expect(phone.locator("#draft-count")).to_have_text("0")
                    expect(phone.locator("#notice")).to_contain_text("已提交 1 条反馈")
                    expect(phone.locator("#batches")).to_contain_text("1 条")
                    phone.locator(".review-extras details:has(#batches) > summary").click()
                    phone.locator("#batches [data-batch]").first.click()
                    expect(phone.locator("#batch-dialog")).to_be_visible()
                    expect(phone.locator("#batch-detail")).to_contain_text("应该推送")
                    expect(phone.locator("#batch-detail .feedback-result .status")).to_have_text("已提交")
                    phone.locator("#batch-dialog .close").click()

                    submit_filters(phone, restarted_origin, query="<script>")
                    malicious_row = ensure_main_fixture(phone, restarted_origin, malicious, group="excluded")
                    expect(malicious_row).to_be_visible()
                    with phone.expect_response(response_for(restarted_origin + f"/pylon/review/api/detail?id={malicious['id']}", "GET")):
                        malicious_row.locator("[data-expand]").click()
                    expect(phone.locator(f"tr[data-detail='{malicious['id']}'] .detail-content")).to_contain_text("恶意标题 fixture body")
                    assert phone.evaluate("window.__review_pwned || null") is None
                    assert phone.locator("[onerror]").count() == 0
                    assert not errors, errors
                    assert not blocked, blocked
                    print(
                        "Daily review desktop/mobile: layered browse, 50-row cursor streams, dense archive windowing, "
                        "history/notification detail, row/product/bulk feedback, autosave, cross-session and restart "
                        "recovery passed; auth/CSRF and no-external-request checks passed."
                    )
                    phone.close()
                    mobile.close()
            finally:
                browser.close()


if __name__ == "__main__":
    main()
