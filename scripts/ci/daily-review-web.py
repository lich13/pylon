"""Exercise the daily review workspace with an isolated database and browser."""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ZONE = ZoneInfo("Asia/Shanghai")
TOTAL_ROWS = 126
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
            day = DAY_A if index < total // 2 else DAY_B
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
            result = {
                "decision": "push" if index % 2 == 0 else "hold",
                "categories": [category],
                "facts": [{"category": category, "kind": "fixture", "subject": title, "value": body}],
                "items": items,
                "reason": f"fixture decision {index:03d}",
            }
            status = STATUSES[index % len(STATUSES)]
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
                    "title": title,
                    "body": body,
                }
            )
    return fixture


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


def submit_filters(page, origin, *, day="", source="", status="", category="", query=""):
    page.locator("#day").select_option(day)
    page.locator("#source").select_option(source)
    page.locator("#status").select_option(status)
    page.locator("#category").select_option(category)
    page.locator("#q").fill(query)
    with page.expect_response(response_for(origin + "/pylon/review/api/items", "GET")):
        page.locator("#filters button[type=submit]").click()


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
            page.on("console", lambda message: errors.append(f"console: {message.text}") if message.type == "error" else None)

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

                    # The default is the newest day; select all days to exercise 50-row paging.
                    submit_filters(page, origin, day="")
                    expect(page.locator("#count")).to_have_text(f"{TOTAL_ROWS} 条")
                    expect(page.locator("#page-label")).to_have_text("1 / 3")
                    expect(page.locator("#feed .review-card")).to_have_count(50)
                    page.locator("#next").click()
                    expect(page.locator("#page-label")).to_have_text("2 / 3")
                    expect(page.locator("#feed .review-card")).to_have_count(50)
                    page.locator("#next").click()
                    expect(page.locator("#page-label")).to_have_text("3 / 3")
                    expect(page.locator("#feed .review-card")).to_have_count(TOTAL_ROWS - 100)
                    page.locator("#prev").click()
                    expect(page.locator("#page-label")).to_have_text("2 / 3")

                    # Date, source, status, category and full-text filters all narrow the real API result.
                    expected_day_a = sum(row["day"] == DAY_A for row in fixture)
                    submit_filters(page, origin, day=DAY_A)
                    expect(page.locator("#count")).to_have_text(f"{expected_day_a} 条")
                    expected_source = sum(row["source"] == "linuxdo" for row in fixture)
                    submit_filters(page, origin, source="linuxdo")
                    expect(page.locator("#count")).to_have_text(f"{expected_source} 条")
                    expected_status = sum(row["status"] == "delivered" for row in fixture)
                    submit_filters(page, origin, status="delivered")
                    expect(page.locator("#count")).to_have_text(f"{expected_status} 条")
                    expected_category = sum(row["category"] == "fixture-a" for row in fixture)
                    submit_filters(page, origin, category="fixture-a")
                    expect(page.locator("#count")).to_have_text(f"{expected_category} 条")
                    submit_filters(page, origin, query="needle-077")
                    expect(page.locator("#count")).to_have_text("1 条")
                    expect(page.locator("#feed .review-card")).to_have_count(1)

                    # Historical decisions, source snapshots, and the notification text are all expandable.
                    submit_filters(page, origin, query="history-keyword")
                    history_card = page.locator(f"article[data-record='{history['id']}']")
                    history_card.locator("details.record-details").click()
                    expect(history_card.locator(".detail-content")).to_contain_text("历史第一条判断")
                    expect(history_card.locator(".detail-content")).to_contain_text("历史最终判断")
                    expect(history_card.locator(".detail-content")).to_contain_text("fixture notification delivered")
                    expect(history_card.locator(".detail-content")).to_contain_text("历史通知样本 body")

                    # Row-level combined correction with an explicit brand scope.
                    submit_filters(page, origin, query=single["title"])
                    single_card = page.locator(f"article[data-record='{single['id']}']")
                    open_action(page, origin, f"article[data-record='{single['id']}'] [data-feedback='category']")
                    wait_feedback_save(
                        page,
                        origin,
                        lambda: (
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
                    page.reload()
                    expect(page.locator("#workspace")).to_be_visible()
                    expect(page.locator("#draft-count")).to_have_text("1")
                    expect(page.locator("#drafts")).to_contain_text("组合纠正原因")

                    # A second authenticated context proves the draft survives a session boundary.
                    second = browser.new_context(viewport={"width": 1360, "height": 960})
                    page2 = second.new_page()
                    hook(page2, origin)
                    page2.goto(origin + "/pylon/review/#login=" + login_token(box))
                    expect(page2.locator("#workspace")).to_be_visible()
                    expect(page2.locator("#draft-count")).to_have_text("1")

                    # Product-level feedback uses the displayed decision/item key.
                    submit_filters(page2, origin, query=product["title"])
                    product_card = page2.locator(f"article[data-record='{product['id']}']")
                    expect(product_card.locator("details.products")).to_contain_text("商品")
                    product_card.locator("details.products").click()
                    product_button = product_card.locator("details.products [data-feedback='facts']").first
                    product_item_key = product_button.get_attribute("data-item")
                    assert product_item_key and product_item_key.endswith(":0"), product_item_key
                    open_action(page2, origin, f"article[data-record='{product['id']}'] details.products [data-feedback='facts']")
                    wait_feedback_save(
                        page2,
                        origin,
                        lambda: (
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
                    expect(page2.locator("#feed .review-card")).to_have_count(2)
                    page2.locator("#select-page").click()
                    expect(page2.locator("#bulk-feedback")).to_be_enabled()
                    page2.locator("#bulk-feedback").click()
                    expect(page2.locator("#feedback-title")).to_contain_text("为 2 条")
                    wait_feedback_save(page2, origin, lambda: page2.locator("[data-action='should_push']").check())
                    expect(page2.locator("#feedback-save")).to_have_text("")
                    page2.locator("#feedback-done").click()
                    state = page2.request.get(origin + "/pylon/review/api/state").json()
                    assert len(state["drafts"]) == 4, state["drafts"]

                    # Submit exactly one batch, then read its frozen evidence in the UI.
                    with page2.expect_response(response_for(origin + "/pylon/review/api/submit", "POST")) as response:
                        page2.locator("#submit-batch").click()
                    assert response.value.status == 200, response.value.text()
                    expect(page2.locator("#draft-count")).to_have_text("0")
                    expect(page2.locator("#notice")).to_contain_text("已提交 4 条反馈")
                    expect(page2.locator("#batches")).to_contain_text("4 条")
                    page2.locator("aside details:has(#batches) > summary").click()
                    page2.locator("#batches [data-batch]").first.click()
                    expect(page2.locator("#batch-dialog")).to_be_visible()
                    expect(page2.locator("#batch-detail")).to_contain_text("组合纠正原因")
                    expect(page2.locator("#batch-detail")).to_contain_text("逐商品价格纠正")

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
                    expect(phone.locator("#draft-count")).to_have_text("0")
                    expect(phone.locator("#batches")).to_contain_text("4 条")
                    expect(phone.locator(".workspaces a")).to_have_count(3)
                    assert phone.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")

                    # The fixed mobile action bar can complete a fresh single-record batch
                    # and the submitted evidence remains available after the restart.
                    submit_filters(phone, restarted_origin, query="history-keyword")
                    history_card = phone.locator(f"article[data-record='{history['id']}']")
                    expect(history_card).to_be_visible()
                    history_card.locator(f"[data-select='{history['id']}']").check()
                    expect(phone.locator("#mobile-feedback")).to_be_enabled()
                    phone.locator("#mobile-feedback").click()
                    expect(phone.locator("#feedback-dialog")).to_be_visible()
                    wait_feedback_save(phone, restarted_origin, lambda: phone.locator("[data-action='should_push']").check())
                    phone.locator("#feedback-done").click()
                    expect(phone.locator("#draft-count")).to_have_text("1")
                    expect(phone.locator("#mobile-submit")).to_be_enabled()
                    with phone.expect_response(response_for(restarted_origin + "/pylon/review/api/submit", "POST")) as response:
                        phone.locator("#mobile-submit").click()
                    assert response.value.status == 200, response.value.text()
                    expect(phone.locator("#draft-count")).to_have_text("0")
                    expect(phone.locator("#notice")).to_contain_text("已提交 1 条反馈")
                    expect(phone.locator("#batches")).to_contain_text("1 条")
                    phone.locator("aside details:has(#batches) > summary").click()
                    phone.locator("#batches [data-batch]").first.click()
                    expect(phone.locator("#batch-dialog")).to_be_visible()
                    expect(phone.locator("#batch-detail")).to_contain_text("应该推送")
                    phone.locator("#batch-dialog .close").click()

                    submit_filters(phone, restarted_origin, query="<script>")
                    malicious_card = phone.locator(f"article[data-record='{malicious['id']}']")
                    expect(malicious_card).to_be_visible()
                    malicious_card.locator("details.record-details").click()
                    expect(malicious_card.locator(".detail-content")).to_contain_text("恶意标题 fixture body")
                    assert phone.evaluate("window.__review_pwned || null") is None
                    assert phone.locator("[onerror]").count() == 0
                    assert not errors, errors
                    assert not blocked, blocked
                    print(
                        "Daily review desktop/mobile: navigation, filters, 50-row paging, history/notification detail, "
                        "row/product/bulk feedback, autosave, cross-session and restart recovery passed; auth/CSRF and "
                        "no-external-request checks passed."
                    )
                    phone.close()
                    mobile.close()
            finally:
                browser.close()


if __name__ == "__main__":
    main()
