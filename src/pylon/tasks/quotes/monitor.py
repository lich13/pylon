#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pylon import events
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from pylon.shared import output, paths, playwright_env
from pylon.tasks.quotes.arbitrage import (
    DB_PATH,
    OPEN_ET_ENDPOINT,
    SOURCE_URL,
    SVG_ENDPOINT,
    connect,
    dedupe_quote_entries,
    discover_unknown_quote_candidates,
    get_meta,
    now_iso,
    parse_quotes_from_copied_tsv,
    parse_quotes_from_open_et,
    parse_quotes_from_svg,
    replace_quotes,
    set_meta,
)

playwright_env.ensure()

BJ = dt.timezone(dt.timedelta(hours=8))
TARGET_HOURS = {9, 12, 15, 18}
QUOTE_PARSER_VERSION = "2026-09-30-validated-quotes-v9"
MIN_CLIPBOARD_TSV_CHARS = 1000
FETCH_SOURCE_ATTEMPTS = 3
COPY_ATTEMPTS_PER_PAGE = 4


def bj_now() -> dt.datetime:
    return dt.datetime.now(BJ).replace(microsecond=0)


def slot_id(now: dt.datetime | None = None) -> str:
    now = now or bj_now()
    return now.strftime("%Y-%m-%d %H:55")


def _clipboard_tsv_complete(tsv: str) -> bool:
    return len(tsv or "") >= MIN_CLIPBOARD_TSV_CHARS and "\t" in (tsv or "")


def _attempt_fullsheet_copy(page, copy_patch: str) -> tuple[str, int]:
    """Return full-sheet TSV and the successful per-page copy attempt number.

    KDocs occasionally accepts the copy-unlock patch but the first clipboard
    read is empty/partial because the grid has not focused yet or the login
    invitation/SPA overlay briefly steals keyboard focus. Keep the browser
    session live and retry the exact user-like selection before failing closed.
    """
    click_points = [(300, 220), (520, 360), (900, 520), (180, 180)]
    last_tsv = ""
    for copy_attempt in range(1, COPY_ATTEMPTS_PER_PAGE + 1):
        try:
            page.evaluate(copy_patch)
        except Exception:
            pass
        try:
            page.evaluate("navigator.clipboard.writeText('').catch(e => '')")
        except Exception:
            pass
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass

        x, y = click_points[(copy_attempt - 1) % len(click_points)]
        page.mouse.click(x, y)
        page.wait_for_timeout(350 + copy_attempt * 150)
        try:
            page.evaluate("window.focus(); if (document.body && document.body.focus) document.body.focus();")
        except Exception:
            pass

        page.keyboard.press("Control+A")
        page.wait_for_timeout(300)
        page.keyboard.press("Control+A")
        page.wait_for_timeout(300)
        page.keyboard.press("Control+C")
        page.wait_for_timeout(1800 + copy_attempt * 700)
        try:
            last_tsv = page.evaluate("navigator.clipboard.readText().catch(e => '')") or ""
        except Exception:
            last_tsv = ""
        if _clipboard_tsv_complete(last_tsv):
            return last_tsv, copy_attempt

    raise RuntimeError(
        "KDocs copy patch did not return full TSV clipboard data "
        f"after {COPY_ATTEMPTS_PER_PAGE} copy attempts "
        f"(last_chars={len(last_tsv)}, has_tab={int('\t' in last_tsv)})"
    )


def fetch_quote_sources_via_browser(timeout_ms: int = 60000, settle_ms: int = 8000) -> dict[str, Any]:
    """Fetch both the rendered SVG and full KDocs workbook stream.

    `svg/et` only covers the rendered visible worksheet range; non-phone rows
    such as MatePad/iPad currently live outside that range but are present in
    the page's initial `/open/et` response. Capture both in one browser session
    so we do not silently drop far-right/far-down quote sections.
    """
    from playwright.sync_api import sync_playwright

    copy_patch = r"""
    (() => {
      function patchApp(app) {
        try {
          if (app && typeof app === 'object') {
            app.canCopy = function(){ return true; };
            app.canDownload = function(){ return true; };
            app.canExport = function(){ return true; };
          }
        } catch(e) {}
      }
      let _APP = window.APP;
      try {
        Object.defineProperty(window, 'APP', {
          configurable: true,
          get(){ return _APP; },
          set(v){ _APP = v; patchApp(v); }
        });
      } catch(e) {}
      patchApp(_APP);
      setInterval(() => patchApp(window.APP), 200);
    })();
    """
    last_error: Exception | None = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for fetch_attempt in range(1, FETCH_SOURCE_ATTEMPTS + 1):
                captured_svg = []
                captured_open_et = []
                context = browser.new_context(
                    viewport={"width": 2400, "height": 1500},
                    user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
                    permissions=["clipboard-read", "clipboard-write"],
                )
                page = context.new_page()
                page.add_init_script(copy_patch)

                def handle_response(resp):
                    if resp.url.startswith(SVG_ENDPOINT) and resp.status == 200:
                        try:
                            captured_svg.append(resp.body())
                        except Exception:
                            pass
                    elif resp.url.startswith(OPEN_ET_ENDPOINT) and resp.status == 200:
                        try:
                            body = resp.body()
                            # The workbook stream is binary; direct post-load requests can
                            # return 404, so capture this page response while it is live.
                            if body and not body.startswith(b"404 page not found"):
                                captured_open_et.append(body)
                        except Exception:
                            pass

                page.on("response", handle_response)
                try:
                    page.goto(SOURCE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                    try:
                        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 15000))
                    except Exception:
                        pass
                    page.wait_for_timeout(settle_ms + (fetch_attempt - 1) * 2500)
                    # The public KDocs page can show a login invitation; dismiss it when visible.
                    try:
                        btn = page.locator(".wps-login-panel__header__right button").first
                        if btn.count():
                            btn.click(timeout=3000)
                            page.wait_for_timeout(1000)
                    except Exception:
                        pass
                    # Prefer the direct SVG endpoint after the page has established guest/session cookies.
                    direct_svg: bytes | None = None
                    resp = page.context.request.get(SVG_ENDPOINT, timeout=timeout_ms)
                    if resp.ok:
                        body = resp.body()
                        if body.startswith(b"<?xml") or b"<svg" in body[:2000]:
                            direct_svg = body
                    page.wait_for_timeout(4000 + (fetch_attempt - 1) * 1000)
                    svg = direct_svg or (captured_svg[-1] if captured_svg else None)
                    if not svg:
                        raise RuntimeError(f"svg endpoint failed: status={resp.status} body={resp.text()[:200] if resp else ''}")
                    if not captured_open_et:
                        raise RuntimeError("open/et workbook response was not captured; refusing to refresh quotes from partial SVG only")
                    clipboard_tsv, copy_attempt = _attempt_fullsheet_copy(page, copy_patch)
                    return {
                        "svg": svg,
                        "open_et": captured_open_et[-1],
                        "clipboard_tsv": clipboard_tsv.encode("utf-8"),
                        "fetch_attempt": fetch_attempt,
                        "copy_attempt": copy_attempt,
                    }
                except Exception as exc:
                    last_error = exc
                    if fetch_attempt >= FETCH_SOURCE_ATTEMPTS:
                        raise
                finally:
                    context.close()
        finally:
            browser.close()
    raise RuntimeError(f"KDocs source fetch failed: {last_error}")


# Backward-compatible helper for ad-hoc callers/tests that only need the SVG.
def fetch_svg_via_browser(timeout_ms: int = 60000, settle_ms: int = 8000) -> bytes:
    return fetch_quote_sources_via_browser(timeout_ms=timeout_ms, settle_ms=settle_ms)["svg"]


def _json_meta(conn, key: str, default: Any) -> Any:
    try:
        value = get_meta(conn, key, "")
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def update_quotes(timeout_ms: int = 60000, min_entries: int = 20) -> dict[str, Any]:
    sources = fetch_quote_sources_via_browser(timeout_ms=timeout_ms)
    svg = sources["svg"]
    open_et = sources["open_et"]
    clipboard_tsv = sources.get("clipboard_tsv", b"").decode("utf-8", "replace")
    fetch_meta = {
        "fetch_attempt": int(sources.get("fetch_attempt") or 0),
        "copy_attempt": int(sources.get("copy_attempt") or 0),
        "clipboard_tsv_bytes": len(sources.get("clipboard_tsv", b"")),
    }
    snapshot_hash = hashlib.sha256(b"svg\0" + svg + b"\nopen_et\0" + open_et + b"\nclipboard_tsv\0" + clipboard_tsv.encode("utf-8")).hexdigest()
    with connect(DB_PATH) as conn:
        last_hash = get_meta(conn, "quotes_snapshot_hash", "")
        last_count = int(get_meta(conn, "quotes_count", "0") or "0")
        parser_version = get_meta(conn, "quotes_parser_version", "")
        pending_missing = _json_meta(conn, "quotes_category_missing_streaks", {})
        if last_hash == snapshot_hash and last_count > 0 and parser_version == QUOTE_PARSER_VERSION and not pending_missing:
            set_meta(conn, "quotes_validated_at", now_iso())
            return {"status": "unchanged", "quote_count": last_count, "db_path": str(DB_PATH), "snapshot_hash": snapshot_hash, **fetch_meta}
    svg_entries = parse_quotes_from_svg(svg)
    # `clipboard_tsv` is the only source that fully covers KDocs' lazy-rendered
    # far-down/far-right ranges (Apple Watch/AirPods/Pencil, Huawei earbuds,
    # DJI/Insta360/Nintendo, OPPO/VIVO/Honor side sections). `open/et` and SVG
    # are retained as independent fallbacks/diagnostics, but TSV is primary.
    tsv_entries = parse_quotes_from_copied_tsv(clipboard_tsv, nonphone_only=False)
    open_entries = parse_quotes_from_open_et(open_et, nonphone_only=False)
    unknown_candidates = discover_unknown_quote_candidates(open_et, limit=20)
    entries = dedupe_quote_entries([*svg_entries, *open_entries, *tsv_entries])
    if len(entries) < max(100, min_entries):
        raise RuntimeError(f"parsed too few valid quote entries: {len(entries)}")
    if last_count >= 100 and len(entries) < last_count * .8:
        raise RuntimeError(f"报价快照不完整：原有 {last_count} 条，本次仅 {len(entries)} 条；保留旧库")
    if any(not str(e['model_spec']).strip() or not 50 <= int(e['quote_price']) <= 50000 for e in entries):
        raise RuntimeError('报价快照含无效型号或价格；保留旧库')
    unknown_hash = hashlib.sha1(json.dumps(unknown_candidates, ensure_ascii=False, sort_keys=True).encode()).hexdigest() if unknown_candidates else ''
    with connect(DB_PATH) as conn:
        def persist_metadata(stats):
            metadata = {
                'quotes_parser_version': QUOTE_PARSER_VERSION,
                'quotes_svg_count': str(len(svg_entries)), 'quotes_open_et_count': str(len(open_entries)),
                'quotes_clipboard_tsv_count': str(len(tsv_entries)),
                'quotes_last_fetch_attempt': str(fetch_meta['fetch_attempt']),
                'quotes_last_copy_attempt': str(fetch_meta['copy_attempt']),
                'quotes_last_clipboard_tsv_bytes': str(fetch_meta['clipboard_tsv_bytes']),
                'quotes_unknown_quote_candidates': json.dumps(unknown_candidates, ensure_ascii=False),
                'quotes_unknown_quote_candidate_hash': unknown_hash,
            }
            conn.executemany('INSERT OR REPLACE INTO meta VALUES(?,?)', metadata.items())
        sync_stats = replace_quotes(conn, entries, SOURCE_URL, snapshot_hash=snapshot_hash,
                                    protect_removed_categories=True, category_absence_confirmations=2,
                                    before_commit=persist_metadata)
    result = {
        "status": "ok",
        "quote_count": sum(int(v) for v in (sync_stats.get("category_counts", {}) or {}).values()),
        "svg_count": len(svg_entries),
        "open_et_count": len(open_entries),
        "clipboard_tsv_count": len(tsv_entries),
        "unknown_quote_candidate_count": len(unknown_candidates),
        "unknown_quote_candidates": unknown_candidates,
        "category_change": sync_stats,
        "category_counts": sync_stats.get("category_counts", {}),
        "db_path": str(DB_PATH),
        "snapshot_hash": snapshot_hash,
        **fetch_meta,
    }
    return result


def record_refresh_event(slot: str, mode: str, status: str, result: dict[str, Any] | None = None, err: Exception | None = None) -> None:
    result = result or {}
    with connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS quote_refresh_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                occurred_at TEXT NOT NULL,
                slot TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                quote_count INTEGER,
                svg_count INTEGER,
                open_et_count INTEGER,
                clipboard_tsv_count INTEGER,
                fetch_attempt INTEGER,
                copy_attempt INTEGER,
                clipboard_tsv_bytes INTEGER
            )
            """
        )
        conn.execute(
            """
            INSERT INTO quote_refresh_events(
                occurred_at, slot, mode, status, error, quote_count, svg_count,
                open_et_count, clipboard_tsv_count, fetch_attempt, copy_attempt,
                clipboard_tsv_bytes
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                bj_now().isoformat(),
                slot,
                mode,
                status,
                f"{type(err).__name__}: {err}" if err else None,
                int(result.get("quote_count") or 0) if result else None,
                int(result.get("svg_count") or 0) if result.get("svg_count") is not None else None,
                int(result.get("open_et_count") or 0) if result.get("open_et_count") is not None else None,
                int(result.get("clipboard_tsv_count") or 0) if result.get("clipboard_tsv_count") is not None else None,
                int(result.get("fetch_attempt") or 0) if result.get("fetch_attempt") is not None else None,
                int(result.get("copy_attempt") or 0) if result.get("copy_attempt") is not None else None,
                int(result.get("clipboard_tsv_bytes") or 0) if result.get("clipboard_tsv_bytes") is not None else None,
            ),
        )
        conn.commit()


def mark_success(slot: str, mode: str, result: dict[str, Any]) -> None:
    record_refresh_event(slot, mode, str(result.get("status") or "ok"), result=result)
    with connect(DB_PATH) as conn:
        set_meta(conn, "last_success_slot", slot)
        set_meta(conn, "pending_slot", "")
        set_meta(conn, "pending_fail_count", "0")
        set_meta(conn, "last_quote_error", "")
        set_meta(conn, "last_quote_result", json.dumps(result, ensure_ascii=False))


def mark_failure(slot: str, mode: str, err: Exception) -> tuple[int, bool]:
    record_refresh_event(slot, mode, "failure", err=err)
    with connect(DB_PATH) as conn:
        pending = get_meta(conn, "pending_slot", "")
        fail_count = int(get_meta(conn, "pending_fail_count", "0") or "0")
        if pending != slot:
            fail_count = 0
        fail_count += 1
        set_meta(conn, "pending_slot", slot)
        set_meta(conn, "pending_fail_count", str(fail_count))
        set_meta(conn, "last_quote_error", f"{type(err).__name__}: {err}")
    return fail_count, False


def should_retry() -> str:
    with connect(DB_PATH) as conn:
        pending = get_meta(conn, "pending_slot", "")
        success = get_meta(conn, "last_success_slot", "")
    if pending and pending != success:
        return pending
    return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch KDocs electronics quotes into SQLite.")
    ap.add_argument("--mode", choices=["main", "retry", "force"], default="main")
    ap.add_argument("--timeout-ms", type=int, default=60000)
    ap.add_argument("--min-entries", type=int, default=20)
    args = ap.parse_args(argv)
    if args.mode == "main" and os.environ.get("PYLON_TRIGGER") == "manual":
        args.mode = "force"

    from . import health
    now = bj_now()
    slot = slot_id(now)
    try:
        if args.mode == 'main' and (now.hour not in TARGET_HOURS or now.minute < 50):
            print(output.wake_agent_false())
            return 0
        if args.mode == 'retry':
            current = health.inspect(DB_PATH)
            try:
                pending = should_retry()
            except Exception:
                pending = ''
            if not pending and current['available']:
                events.result('ok', health.status_text(path=DB_PATH), changed=False)
                print(output.wake_agent_false())
                return 0
            slot = pending or slot
        result = update_quotes(timeout_ms=args.timeout_ms, min_entries=args.min_entries)
        mark_success(slot, args.mode, result)
        health.check(DB_PATH)
        events.result('ok', health.status_text(path=DB_PATH), changed=result.get('status') != 'unchanged')
        print(output.wake_agent_false())
        return 0
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        try:
            mark_failure(slot, args.mode, exc)
        except Exception:
            pass  # The quote DB itself may be unreadable. Outbox is independent.
        health.check(DB_PATH, error=error)
        events.result('failed', error + '\n' + health.status_text(path=DB_PATH))
        print(output.wake_agent_false())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
