#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


from pylon import events
from pylon.tasks.chiphell import monitor as base
from pylon.tasks.chiphell import categories
from pylon.tasks.chiphell.runtime import (
    CHIPHELL_ALERT_SOURCE,
    CHIPHELL_FID,
    CHIPHELL_FORUM_URL,
    CHIPHELL_USER_AGENT,
    DeliveryQueue,
    NavigationFailure,
    apply_baseline,
    choose_interval,
    classify_navigation_failure,
    classify_page,
    navigate_with_retries,
    network_backoff_seconds,
    playwright_proxy_options,
    probe_proxy_path,
    summarize_navigation_error,
    transition_fault,
)


DATA_ROOT = base.paths.CHIPHELL_DATA_ROOT
QUEUE_PATH = DATA_ROOT / "delivery.sqlite3"
HEALTH_PATH = DATA_ROOT / "proxy-health.json"


def _log_event(event: str, **fields: Any) -> None:
    parts = [event, f"at={datetime.now().astimezone().isoformat(timespec='seconds')}"]
    parts.extend(f"{key}={value}" for key, value in fields.items() if value is not None)
    print(" ".join(parts), flush=True)


def _empty_diagnostics() -> dict[str, Any]:
    return {
        "last_error_class": None,
        "last_error_code": None,
        "proxy_probe_status": "NOT_NEEDED",
        "proxy_probe_latency_ms": 0,
        "navigation_duration_ms": 0,
    }


def _collect_proxy_once(
    args: argparse.Namespace,
    *,
    ignore_state: bool = False,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

    state = base.load_state(args.state)
    notified = set() if ignore_state else set(str(value) for value in state.get("notified_ids", []))
    args.profile_dir.mkdir(parents=True, exist_ok=True)
    proxy_options = playwright_proxy_options(os.environ)
    diagnostics = _empty_diagnostics()

    with sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(args.profile_dir),
                headless=True,
                viewport={"width": 1400, "height": 1000},
                user_agent=CHIPHELL_USER_AGENT,
                **proxy_options,
            )
        except PlaywrightError as error:
            error_class, error_code = summarize_navigation_error(error)
            diagnostics.update({
                "last_error_class": error_class,
                "last_error_code": error_code,
                "proxy_probe_status": "NOT_RUN",
            })
            return "BROWSER_LAUNCH_FAILED", [], [], diagnostics
        page = context.new_page()
        try:
            def log_navigation_failure(failure: NavigationFailure) -> None:
                _log_event(
                    "POLL_NAV_RETRY" if failure.next_retry_seconds is not None else "POLL_NAV_FAILED",
                    attempt=failure.attempt,
                    error_class=failure.error_class,
                    error_code=failure.error_code,
                    duration_ms=failure.duration_ms,
                    next_retry_seconds=failure.next_retry_seconds,
                )

            navigation = navigate_with_retries(
                lambda timeout_ms: page.goto(
                    args.url,
                    wait_until="domcontentloaded",
                    timeout=timeout_ms,
                ),
                first_timeout_ms=args.timeout_ms,
                retryable_errors=(PlaywrightError, PlaywrightTimeoutError),
                on_failure=log_navigation_failure,
            )
            diagnostics["navigation_duration_ms"] = navigation.duration_ms
            if navigation.failures:
                last_failure = navigation.failures[-1]
                diagnostics.update({
                    "last_error_class": last_failure.error_class,
                    "last_error_code": last_failure.error_code,
                })
            if not navigation.ok:
                probe = probe_proxy_path()
                diagnostics.update({
                    "proxy_probe_status": probe.status,
                    "proxy_probe_latency_ms": probe.latency_ms,
                })
                _log_event(
                    "PROXY_PROBE",
                    status=probe.status,
                    error_code=probe.error_code,
                    latency_ms=probe.latency_ms,
                    endpoint=probe.endpoint,
                )
                status = classify_navigation_failure(probe)
                return status, [], [], diagnostics
            if navigation.attempts > 1:
                _log_event(
                    "POLL_NAV_RECOVERED",
                    attempts=navigation.attempts,
                    duration_ms=navigation.duration_ms,
                )
            with contextlib.suppress(PlaywrightTimeoutError):
                page.wait_for_selector(
                    f"{base.THREAD_ROW_SELECTOR}, {base.THREAD_LIST_CONTAINER_SELECTOR}",
                    timeout=args.selector_timeout_ms,
                )
            if args.settle_ms:
                page.wait_for_timeout(args.settle_ms)
            page_text = page.locator("body").inner_text(timeout=5000)
            page_status = classify_page(page_text, page.title())
            if page_status != "OK":
                return page_status, [], [], diagnostics

            candidates = base._collect_thread_candidates_from_dom(page, args, notified)
            if not candidates and page.locator(base.THREAD_ROW_SELECTOR).count() == 0 and page.locator(base.THREAD_TITLE_LINK_SELECTOR).count() == 0:
                return "NO_THREAD_LIST", [], [], diagnostics
            candidates = base.order_posts_for_notification(candidates)
            if args.fetch_body:
                for post in candidates:
                    thread_page = context.new_page()
                    try:
                        thread_page.goto(post["url"], wait_until="domcontentloaded", timeout=args.timeout_ms)
                        thread_page.wait_for_timeout(args.settle_ms)
                        post.update(base.post_body_fields(thread_page, args.body_limit))
                    except Exception as error:
                        post["body_excerpt"] = f"正文抓取失败：{type(error).__name__}"
                    finally:
                        thread_page.close()
            else:
                for post in candidates:
                    post["body_excerpt"] = ""
            base_candidates = [post for post in candidates if base.should_notify_post(post)]
            # Category policy is only for the ordinary feed.  The caller
            # derives quote candidates from the full candidate list so a
            # silent category can never suppress a strict price alert.
            notify_candidates = categories.filter_normal_posts(base_candidates)
            return "OK", candidates, notify_candidates, diagnostics
        finally:
            context.close()


def _format_notification(posts: list[dict[str, Any]], fid: str, quote_posts: list[dict[str, Any]] | None = None) -> str:
    quote_posts = quote_posts if quote_posts is not None else posts
    arbitrage_messages = base.collect_arbitrage_messages("chiphell", quote_posts)
    if not posts and not arbitrage_messages:
        return ""
    arbitrage = base.format_alerts(arbitrage_messages)
    normal = base.format_telegram_message(posts, fid)
    return f"{arbitrage}\n\n{normal}".strip() if arbitrage and posts else (arbitrage or normal)


def _fault_message(status: str) -> str:
    descriptions = {
        "PROXY_UNAVAILABLE": "家宽代理不可用，任务已禁止腾讯云直连并进入退避。",
        "UPSTREAM_UNAVAILABLE": "家宽代理探测正常，但 Chiphell 页面连续两轮无法完成导航；任务未使用腾讯云直连回退。",
        "BLOCKED_567": "家宽出口仍触发 Chiphell 567/EdgeOne 限制，6 小时后再探测。",
        "LOGIN_REQUIRED": "Chiphell 登录态失效，需要手工同步 Brave cookies。",
        "NO_THREAD_LIST": f"页面未找到 fid={CHIPHELL_FID} 帖子列表，任务已暂停有效数据写入。",
        "BROWSER_LAUNCH_FAILED": "Chromium 启动失败，任务未访问 Chiphell。",
    }
    return f"[{CHIPHELL_ALERT_SOURCE}] {status}: {descriptions.get(status, '未知故障。')}"


def _save_health(
    status: str,
    *,
    now: int,
    next_delay: int,
    candidate_count: int = 0,
    diagnostics: dict[str, Any] | None = None,
    consecutive_failures: int = 0,
    alert_sent: bool = False,
) -> None:
    previous = base.state_json.load(HEALTH_PATH, default={})
    health = dict(previous) if isinstance(previous, dict) else {}
    current_diagnostics = _empty_diagnostics()
    if diagnostics:
        current_diagnostics.update(diagnostics)
    health.update({
        "status": status,
        "last_poll_at": now,
        "next_delay_seconds": next_delay,
        "candidate_count": candidate_count,
        "pending_delivery_count": DeliveryQueue(QUEUE_PATH).pending_count(),
        "last_error_class": current_diagnostics["last_error_class"],
        "last_error_code": current_diagnostics["last_error_code"],
        "proxy_probe_status": current_diagnostics["proxy_probe_status"],
        "proxy_probe_latency_ms": current_diagnostics["proxy_probe_latency_ms"],
        "navigation_duration_ms": current_diagnostics["navigation_duration_ms"],
        "consecutive_failures": int(consecutive_failures),
        "alert_sent": bool(alert_sent),
    })
    if status == "OK":
        health["last_success_at"] = now
    base.state_json.save(HEALTH_PATH, health)


def _run_baseline(args: argparse.Namespace) -> int:
    args.max_posts = max(args.max_posts, 100)
    args.fetch_body = False
    status, candidates, _, diagnostics = _collect_proxy_once(args, ignore_state=True)
    if status != "OK" or not candidates:
        _save_health(
            status,
            now=int(time.time()),
            next_delay=0,
            diagnostics=diagnostics,
            consecutive_failures=1,
        )
        print(f"BASELINE_FAILED status={status}", file=sys.stderr)
        return 1
    state = apply_baseline(base.load_state(args.state), [post["id"] for post in candidates], max_ids=args.max_stored_ids)
    state["baseline_at"] = int(time.time())
    base.save_state(args.state, state, args.max_stored_ids)
    _save_health(
        "OK",
        now=int(time.time()),
        next_delay=0,
        candidate_count=len(candidates),
        diagnostics=diagnostics,
    )
    _log_event("BASELINE_OK", count=len(candidates))
    return 0


def _run_cycle(args: argparse.Namespace, *, observe: bool) -> int:
    status, candidates, posts, diagnostics = _collect_proxy_once(args)
    now = int(time.time())
    state = base.load_state(args.state)
    proxy_state = state.get("proxy_runtime") if isinstance(state.get("proxy_runtime"), dict) else {}
    queue = DeliveryQueue(QUEUE_PATH)

    if status == "OK":
        quote_posts = [post for post in candidates if base.should_notify_post(post)]
        if not observe:
            for prompt in categories.unknown_prompts(posts, profile="default"):
                events.publish(
                    prompt["text"],
                    profile="default",
                    key="chiphell:unknown:" + prompt["fingerprint"],
                    reply_markup=prompt["reply_markup"],
                )
        # The observe path is used by health checks and must not format or
        # persist quote alerts: collect_arbitrage_messages records matches
        # and may send Bark notifications as part of its normal path.
        notification = "" if observe else _format_notification(posts, args.fid, quote_posts)
        if not observe and notification:
            ids = [str(post["id"]) for post in candidates]
            queue.enqueue(f"posts:{','.join(ids)}", notification, now=now)
        if not observe and candidates:
            merged = apply_baseline(state, [post["id"] for post in candidates], max_ids=args.max_stored_ids)
            state.update(merged)
        proxy_state, event = transition_fault(proxy_state, "OK", now=now)
        if not observe and event.recovered:
            queue.enqueue(
                f"recovery:{event.previous}:{now}",
                f"[{CHIPHELL_ALERT_SOURCE}] RECOVERED: {event.previous} 已恢复，任务继续只走家宽代理。",
                now=now,
            )
        proxy_state["network_attempt"] = 0
        state["proxy_runtime"] = proxy_state
        if not observe:
            base.save_state(args.state, state, args.max_stored_ids)
        delay = choose_interval()
        _save_health(
            "OK",
            now=now,
            next_delay=delay,
            candidate_count=len(candidates),
            diagnostics=diagnostics,
        )
        _log_event(
            "POLL_OK",
            candidates=len(candidates),
            observe=int(observe),
            navigation_duration_ms=diagnostics["navigation_duration_ms"],
            next=delay,
        )
        return delay

    alert_threshold = 2 if status == "UPSTREAM_UNAVAILABLE" else 1
    proxy_state, event = transition_fault(
        proxy_state,
        status,
        now=now,
        alert_threshold=alert_threshold,
    )
    attempt = 0 if event.changed else int(proxy_state.get("network_attempt") or 0)
    if status in {"PROXY_UNAVAILABLE", "UPSTREAM_UNAVAILABLE"}:
        delay = network_backoff_seconds(attempt)
        proxy_state["network_attempt"] = attempt + 1
    else:
        delay = 6 * 60 * 60
        proxy_state["network_attempt"] = 0
    state["proxy_runtime"] = proxy_state
    if not observe:
        if event.alert:
            queue.enqueue(f"fault:{status}:{now // (6 * 60 * 60)}", _fault_message(status), now=now)
        base.save_state(args.state, state, args.max_stored_ids)
    _save_health(
        status,
        now=now,
        next_delay=delay,
        diagnostics=diagnostics,
        consecutive_failures=event.consecutive_count,
        alert_sent=event.alert_sent,
    )
    _log_event(
        "POLL_FAULT",
        status=status,
        observe=int(observe),
        error_class=diagnostics["last_error_class"],
        error_code=diagnostics["last_error_code"],
        proxy_probe_status=diagnostics["proxy_probe_status"],
        consecutive_failures=event.consecutive_count,
        alerted=int(event.alert),
        alert_sent=int(event.alert_sent),
        next=delay,
    )
    return delay


def build_parser() -> argparse.ArgumentParser:
    parser = base.build_arg_parser()
    parser.set_defaults(min_interval=60, max_interval=120)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--observe", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    return parser


def apply_forum_target(args: argparse.Namespace) -> argparse.Namespace:
    args.url = CHIPHELL_FORUM_URL
    args.fid = CHIPHELL_FID
    return args


def main(argv: list[str] | None = None) -> int:
    args = apply_forum_target(build_parser().parse_args(argv))
    observe = args.observe or os.environ.get("CHIPHELL_POLLER_MODE") == "observe"
    with base.single_instance(args.lock) as acquired:
        if not acquired:
            print("POLLER_LOCKED")
            return 0
        if args.baseline:
            return _run_baseline(args)
        while True:
            delay = _run_cycle(args, observe=observe)
            if args.once:
                return 0
            time.sleep(delay)


if __name__ == "__main__":
    raise SystemExit(main())
