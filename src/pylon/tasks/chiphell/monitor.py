#!/usr/bin/env python3
"""Chiphell fid=328 new-thread monitor helper.

Designed to run without an LLM/model: Playwright opens the forum page, extracts
new thread rows, optionally opens each thread to fetch a first-post excerpt, and
prints either [SILENT], LOGIN_REQUIRED, or a Telegram-ready Markdown message.

This script intentionally does not store credentials. Login is handled by a
persistent Chromium profile directory supplied via --profile-dir.
"""
from __future__ import annotations

import argparse
import contextlib
import html
import random
import re
import sys
import time
from pathlib import Path
from typing import Any

from pylon.shared import locks, output, paths, playwright_env, state_json
from pylon.tasks.quotes.arbitrage import collect_arbitrage_messages, format_alerts, format_contact_note, is_excluded_device_deal

playwright_env.ensure()
from urllib.parse import urljoin

DEFAULT_FID = "328"
DEFAULT_URL = f"https://www.chiphell.com/forum.php?mod=forumdisplay&fid={DEFAULT_FID}&filter=author&orderby=dateline"
THREAD_LIST_CONTAINER_SELECTOR = "table#threadlisttableid"
THREAD_ROW_SELECTOR = 'tbody[id^="normalthread_"]'
THREAD_TITLE_LINK_SELECTOR = "a.s.xst"
DEFAULT_STATE = paths.CHIPHELL_DATA_ROOT / "state.json"
DEFAULT_PROFILE = paths.CHIPHELL_DATA_ROOT / "browser-profile"
DEFAULT_LOCK = paths.CHIPHELL_DATA_ROOT / "monitor.lock"
ALERT_SOURCE = "Chiphell fid=328 新帖监控"


def extract_thread_id(row_id: str | None, href: str | None) -> str | None:
    row_id = row_id or ""
    href = href or ""
    if row_id.startswith("normalthread_"):
        tail = row_id[len("normalthread_") :]
        if tail.isdigit():
            return tail
    for pat in (r"thread-(\d+)-", r"[?&]tid=(\d+)"):
        m = re.search(pat, href)
        if m:
            return m.group(1)
    return None


def clean_discuz_html(raw_html: str | None) -> str:
    """Convert Discuz post HTML to text while removing anti-scraping jammer spans."""
    s = raw_html or ""
    s = re.sub(r"<font\b(?=[^>]*\bclass=[\"'][^\"']*\bjammer\b)[^>]*>.*?</font>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<span\b(?=[^>]*\bstyle=[\"'][^\"']*display\s*:\s*none)[^>]*>.*?</span>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<ignore_js_op\b[^>]*>.*?</ignore_js_op>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<script\b[^>]*>.*?</script>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<style\b[^>]*>.*?</style>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"</p\s*>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(re.sub(r"\s+", " ", s).strip())


def compact_excerpt(text: str | None, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    # Strip common Discuz boilerplate snippets if present.
    text = re.sub(r"发表于\s*\d{4}-\d{1,2}-\d{1,2}[^ ]*", "", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def parse_typeoption_html(raw_html: str | None) -> str:
    """Extract Chiphell/Discuz classified fields above the first-post body.

    fid=26 sale posts put actionable fields such as price, QQ/contact,
    condition and local-only trade range in ``div.typeoption`` before ``td.t_f``.
    Those fields must be part of both the visible excerpt and arbitrage matching.
    """
    s = raw_html or ""
    parts: list[str] = []
    cap = re.search(r"<caption\b[^>]*>(.*?)</caption>", s, flags=re.I | re.S)
    if cap:
        caption = clean_discuz_html(cap.group(1)).strip(" ：:")
        if caption and caption not in parts:
            parts.append(caption)
    for row in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", s, flags=re.I | re.S):
        row_html = row.group(1)
        m = re.search(r"<th\b[^>]*>(.*?)</th>\s*<td\b[^>]*>(.*?)</td>", row_html, flags=re.I | re.S)
        if not m:
            continue
        label = clean_discuz_html(m.group(1)).strip().strip(" ：:")
        value = clean_discuz_html(m.group(2)).replace("\xa0", " ").strip().strip(" ：:")
        value = re.sub(r"\s+", " ", value)
        if not label or not value or value in {"-", "—", "无", "暂无"}:
            continue
        parts.append(f"{label}：{value}")
    return "；".join(parts)


def load_state(path: Path) -> dict[str, Any]:
    data = state_json.load(path, default={"notified_ids": []})
    if not isinstance(data, dict):
        return {"notified_ids": []}
    ids = data.get("notified_ids")
    if not isinstance(ids, list):
        data["notified_ids"] = []
    return data


def save_state(path: Path, data: dict[str, Any], max_ids: int = 500) -> None:
    ids = [str(x) for x in data.get("notified_ids", []) if str(x)]
    data["notified_ids"] = ids[-max_ids:]
    state_json.save(path, data)


def order_posts_for_notification(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the page order; the monitored URL is already sorted by dateline."""
    return list(posts)


def should_notify_post(post: dict[str, Any]) -> bool:
    """Drop user-excluded device classes from normal Chiphell/arbitrage alerts."""
    text = f"{post.get('title') or ''}\n{post.get('body_excerpt') or ''}"
    return not is_excluded_device_deal(text)


def format_telegram_message(posts: list[dict[str, Any]], fid: str = DEFAULT_FID) -> str:
    lines = ["## Chiphell 新帖"]
    for i, post in enumerate(posts, 1):
        title = str(post.get("title") or "无标题").strip()
        url = str(post.get("url") or "").strip()
        segments = post.get("category_segments")
        if isinstance(segments, list) and segments:
            excerpt = compact_excerpt("；".join(str(item.get("text") or "") for item in segments), 180)
        else:
            excerpt = compact_excerpt(str(post.get("body_excerpt") or ""), 180)
        item = f"{i}. [{title}]({url})"
        if excerpt:
            item += f" 摘要：{excerpt}"
        contact_note = format_contact_note(str(post.get("body_excerpt") or ""))
        if contact_note and contact_note not in item:
            item += f" {contact_note}"
        lines.append(item)
    return "\n".join(lines).strip()


def looks_login_required(page_text: str, title: str = "") -> bool:
    hay = f"{title}\n{page_text}"
    markers = [
        "尚未登录",
        "没有权限访问该版块",
        "登录",
        "找回密码",
    ]
    return "没有权限访问该版块" in hay or "尚未登录" in hay or ("提示信息" in title and any(m in hay for m in markers))


def extract_first_post_body(page) -> str:
    parts: list[str] = []
    typeoption_loc = page.locator("#postlist .plc .pct .typeoption, #postlist .pcb .typeoption, .typeoption")
    try:
        if typeoption_loc.count():
            raw_typeoption = typeoption_loc.first.evaluate("el => el.innerHTML")
            typeoption_text = parse_typeoption_html(raw_typeoption)
            if typeoption_text:
                parts.append(typeoption_text)
    except Exception:
        pass

    selectors = [
        "td.t_f",              # Discuz post body text
        "div.t_fsz td.t_f",
        "div.pcb td.t_f",
        "#postlist td.t_f",
        "#postlist .pcb",
    ]
    for selector in selectors:
        loc = page.locator(selector)
        try:
            count = loc.count()
        except Exception:
            count = 0
        if count:
            try:
                raw_html = loc.first.evaluate("el => el.innerHTML")
                from pylon.shared.html_text import body_text
                content = body_text(raw_html)
                if content:
                    parts.append(content)
                full_text = "\n\n".join(p for p in parts if p).strip()
                if full_text:
                    return full_text
            except Exception:
                continue
    return "\n\n".join(parts).strip()


def post_body_fields(page, limit: int = 180) -> dict[str, Any]:
    from pylon.shared.html_text import MAX_BODY_CHARS
    full = extract_first_post_body(page)
    excerpt = compact_excerpt(full, limit)
    contact = format_contact_note(full)
    if contact and contact not in excerpt:
        excerpt = f"{excerpt} {contact}".strip()
    return {'body_full': full[:MAX_BODY_CHARS], 'body_complete': bool(full) and len(full) <= MAX_BODY_CHARS,
            'body_excerpt': excerpt}


def extract_first_post_excerpt(page, limit: int = 180) -> str:
    return post_body_fields(page, limit)['body_excerpt']



@contextlib.contextmanager
def single_instance(lock_path: Path):
    """Advisory non-blocking lock to avoid overlapping Playwright runs."""
    with locks.nonblocking_file_lock(lock_path) as result:
        yield result.acquired


def _candidate_from_link(args: argparse.Namespace, notified: set[str], link) -> dict[str, Any] | None:
    try:
        row_id = link.evaluate("el => el.closest('tbody')?.id || ''") or ""
        if row_id.startswith("stickthread_") or row_id == "separatorline":
            return None
        href = link.get_attribute("href") or ""
        abs_url = urljoin(args.url, href)
        tid = extract_thread_id(row_id, abs_url)
        title_text = compact_excerpt(link.inner_text(timeout=3000), 120)
    except Exception:
        return None
    if not tid or not title_text or tid in notified:
        return None
    return {"id": tid, "title": title_text, "url": abs_url}


def _collect_thread_candidates_from_dom(page, args: argparse.Namespace, notified: set[str]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    rows = page.locator(THREAD_ROW_SELECTOR)
    rows_count = rows.count()
    for i in range(min(rows_count, args.max_posts)):
        link = rows.nth(i).locator(THREAD_TITLE_LINK_SELECTOR).first
        if link.count() == 0:
            continue
        item = _candidate_from_link(args, notified, link)
        if item:
            candidates.append(item)
    if candidates or rows_count:
        return candidates

    # Fallback for intermittent Discuz/Chromium timing where title links are
    # queryable before the enclosing table selector is reliably visible.
    links = page.locator(THREAD_TITLE_LINK_SELECTOR)
    for i in range(links.count()):
        item = _candidate_from_link(args, notified, links.nth(i))
        if item:
            candidates.append(item)
        if len(candidates) >= args.max_posts:
            break
    return candidates


def collect_once(args: argparse.Namespace) -> tuple[str, list[dict[str, Any]]]:
    from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

    state = load_state(args.state)
    notified = set(str(x) for x in state.get("notified_ids", []))
    profile_dir = args.profile_dir
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=args.headless,
                viewport={"width": 1400, "height": 1000},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            )
        except PlaywrightError as e:
            msg = str(e)
            if "XServer" in msg or "Missing X server" in msg or "$DISPLAY" in msg:
                return "DISPLAY_REQUIRED", []
            return "BROWSER_LAUNCH_FAILED", [{"title": compact_excerpt(msg, 220), "url": "", "body_excerpt": ""}]
        page = context.new_page()
        try:
            resp = page.goto(args.url, wait_until="domcontentloaded", timeout=args.timeout_ms)
            with contextlib.suppress(PlaywrightTimeoutError):
                page.wait_for_selector(f"{THREAD_ROW_SELECTOR}, {THREAD_LIST_CONTAINER_SELECTOR}", timeout=args.selector_timeout_ms)
            if args.settle_ms > 0:
                page.wait_for_timeout(args.settle_ms)
            body_text = page.locator("body").inner_text(timeout=5000)
            title = page.title()
            if looks_login_required(body_text, title):
                return "LOGIN_REQUIRED", []

            candidates = _collect_thread_candidates_from_dom(page, args, notified)
            if not candidates and page.locator(THREAD_ROW_SELECTOR).count() == 0 and page.locator(THREAD_TITLE_LINK_SELECTOR).count() == 0:
                return "NO_THREAD_LIST", []

            # Preserve page order; the monitored URL is already sorted by new thread dateline.
            candidates = order_posts_for_notification(candidates)
            for post in candidates:
                if not args.fetch_body:
                    post["body_excerpt"] = ""
                    continue
                tpage = context.new_page()
                try:
                    tpage.goto(post["url"], wait_until="domcontentloaded", timeout=args.timeout_ms)
                    tpage.wait_for_timeout(args.settle_ms)
                    post.update(post_body_fields(tpage, args.body_limit))
                except Exception as e:
                    post["body_excerpt"] = f"正文抓取失败：{type(e).__name__}"
                finally:
                    tpage.close()

            notify_candidates = [post for post in candidates if should_notify_post(post)]

            if candidates and not args.dry_run:
                ids = [str(x) for x in state.get("notified_ids", [])]
                for post in candidates:
                    ids.append(post["id"])
                state["notified_ids"] = ids
                save_state(args.state, state, args.max_stored_ids)
            return "OK", notify_candidates
        finally:
            context.close()


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Monitor Chiphell fid=328 new threads without model calls.")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--fid", default=DEFAULT_FID)
    ap.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE)
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    ap.add_argument("--max-posts", type=int, default=8)
    ap.add_argument("--max-stored-ids", type=int, default=500)
    ap.add_argument("--fetch-body", action="store_true", default=True)
    ap.add_argument("--no-fetch-body", dest="fetch_body", action="store_false")
    ap.add_argument("--body-limit", type=int, default=180)
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--selector-timeout-ms", type=int, default=5000)
    ap.add_argument("--settle-ms", type=int, default=600)
    ap.add_argument("--headless", action="store_true", default=True)
    ap.add_argument("--headed", dest="headless", action="store_false")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--loop", action="store_true", help="Run continuously; intended for manual/background service, not a scheduled one-shot task.")
    ap.add_argument("--min-interval", type=int, default=5)
    ap.add_argument("--max-interval", type=int, default=30)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    while True:
        with single_instance(args.lock) as acquired:
            if not acquired:
                print(output.wake_agent_false())
                status, posts = "LOCKED", []
            else:
                status, posts = collect_once(args)
        if status == "LOCKED":
            pass
        elif status == "LOGIN_REQUIRED":
            print(f"[{ALERT_SOURCE}] LOGIN_REQUIRED: Chiphell 未登录或无权访问 fid=328。请先用同一个 --profile-dir 完成人工登录。")
        elif status == "DISPLAY_REQUIRED":
            print(f"[{ALERT_SOURCE}] DISPLAY_REQUIRED: 你用了 --headed，但当前 SSH/Telegram 环境没有 X11 显示器。请用 ssh -Y/-X 连接并确认 $DISPLAY 存在，或先用 headless 检查。")
        elif status == "BROWSER_LAUNCH_FAILED":
            reason = posts[0].get("title") if posts else "unknown"
            print(f"[{ALERT_SOURCE}] BROWSER_LAUNCH_FAILED: Chromium 启动失败：{reason}")
        elif status == "NO_THREAD_LIST":
            print(f"[{ALERT_SOURCE}] NO_THREAD_LIST: 页面已打开，但没有找到帖子列表选择器。可能页面结构变更、登录态失效或 fid=328 权限不足。")
        elif posts:
            arbitrage = format_alerts(collect_arbitrage_messages("chiphell", posts))
            normal = format_telegram_message(posts, args.fid)
            print(f"{arbitrage}\n\n{normal}".strip() if arbitrage else normal)
        else:
            print(output.wake_agent_false())
        if not args.loop:
            break
        time.sleep(random.randint(args.min_interval, args.max_interval))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
