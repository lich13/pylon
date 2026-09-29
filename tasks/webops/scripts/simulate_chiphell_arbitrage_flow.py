#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.webops import locks, paths, playwright_env
from lib.webops.bark import redact_device_key
from lib.webops.electronics_arbitrage import (
    DB_PATH,
    SOURCE_URL,
    best_arbitrage_match,
    connect,
    due_repeat_alerts,
    normalize_text,
    record_arbitrage_if_match,
    sku_key,
)
from scripts.chiphell_fid26_monitor import compact_excerpt, extract_first_post_excerpt, looks_login_required

playwright_env.ensure()

DEFAULT_URL = "https://www.chiphell.com/thread-2804212-1-1.html"
DEFAULT_FIXTURE_PRODUCT = "华为 Pura 80 12+256 绿"
DEFAULT_FIXTURE_BRAND = "华为"
DEFAULT_FIXTURE_MODEL_SPEC = "Pura 80 12+256"
DEFAULT_FIXTURE_COLOR = "绿"
DEFAULT_FIXTURE_QUOTE_PRICE = 3499


class BarkMockServer:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - stdlib callback name
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception as exc:
                    payload = {"_parse_error": f"{type(exc).__name__}: {exc}", "raw": body.decode("utf-8", "replace")[:1000]}
                outer.requests.append({"method": "POST", "path": self.path, "payload": redact_device_key(payload)})
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(b'{"code":200,"message":"success"}')

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.thread.join(timeout=3)


@contextlib.contextmanager
def patched_env(updates: dict[str, str]):
    old = {k: os.environ.get(k) for k in updates}
    try:
        for k, v in updates.items():
            os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fetch_chiphell_thread(url: str, *, timeout_ms: int, settle_ms: int) -> dict[str, str]:
    from playwright.sync_api import sync_playwright

    profile_dir = paths.CHIPHELL_DATA_ROOT / "browser-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    with locks.nonblocking_file_lock(paths.CHIPHELL_DATA_ROOT / "simulate.lock") as lock_result:
        if not lock_result.acquired:
            raise RuntimeError("Chiphell simulation lock is busy")
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=True,
                viewport={"width": 1400, "height": 1000},
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            )
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(settle_ms)
                title = page.title()
                body_text = page.locator("body").inner_text(timeout=10000)
                if looks_login_required(body_text, title):
                    raise RuntimeError("Chiphell login required or no permission")
                excerpt = extract_first_post_excerpt(page, 1200)
                return {
                    "title": title.replace(" - 玩家出售发布区 - Chiphell - 分享与交流用户体验", "").strip(),
                    "body_excerpt": excerpt,
                    "canonical_url": page.url,
                    "body_prefix": compact_excerpt(body_text, 300),
                }
            finally:
                context.close()


def clone_db_to_temp() -> tuple[tempfile.TemporaryDirectory[str], Path]:
    td = tempfile.TemporaryDirectory(prefix="chiphell_arbitrage_sim_")
    dst = Path(td.name) / "electronics_quotes.sqlite3"
    shutil.copy2(DB_PATH, dst)
    return td, dst


def insert_fixture_quote(
    db_path: Path,
    *,
    brand: str,
    model_spec: str,
    color: str,
    product_name: str,
    quote_price: int,
) -> None:
    key = sku_key(brand, model_spec, color)
    normalized = normalize_text(f"{brand}{model_spec}{color}")
    with connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quotes(sku_key,brand,model_spec,color,product_name,quote_price,normalized,source_url,updated_at)
            VALUES(?,?,?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(sku_key) DO UPDATE SET
                brand=excluded.brand,
                model_spec=excluded.model_spec,
                color=excluded.color,
                product_name=excluded.product_name,
                quote_price=excluded.quote_price,
                normalized=excluded.normalized,
                source_url=excluded.source_url,
                updated_at=excluded.updated_at
            """,
            (key, brand, model_spec, color, product_name, int(quote_price), normalized, f"simulation-fixture:{SOURCE_URL}"),
        )
        row = conn.execute("SELECT value FROM meta WHERE key='quotes_count'").fetchone()
        try:
            count = int(row[0]) if row else 0
        except Exception:
            count = 0
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('quotes_count',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(max(count, 1)),),
        )
        conn.commit()


def current_match(db_path: Path, title: str, body: str, *, assume_sale: bool = True) -> dict[str, Any] | None:
    with connect(db_path) as conn:
        match = best_arbitrage_match(conn, title, body, assume_sale=assume_sale)
        return dict(match) if match else None


def run_simulation(args: argparse.Namespace) -> dict[str, Any]:
    thread = fetch_chiphell_thread(args.url, timeout_ms=args.timeout_ms, settle_ms=args.settle_ms)
    temp_db_ctx, temp_db = clone_db_to_temp()
    with temp_db_ctx:
        production_snapshot_match = current_match(temp_db, thread["title"], thread["body_excerpt"])
        fixture_inserted = False
        if args.fixture_quote_price and not production_snapshot_match:
            insert_fixture_quote(
                temp_db,
                brand=args.fixture_brand,
                model_spec=args.fixture_model_spec,
                color=args.fixture_color,
                product_name=args.fixture_product,
                quote_price=args.fixture_quote_price,
            )
            fixture_inserted = True
        match_after_fixture = current_match(temp_db, thread["title"], thread["body_excerpt"])
        with BarkMockServer() as bark:
            with patched_env({"BARK_API_BASE": bark.api_base, "BARK_DEVICE_KEY": "mock-device-key"}):
                first_message = record_arbitrage_if_match(
                    source="chiphell-sim",
                    source_id=args.source_id,
                    url=thread["canonical_url"],
                    title=thread["title"],
                    body=thread["body_excerpt"],
                    db_path=temp_db,
                )
                repeat_messages = due_repeat_alerts(db_path=temp_db, max_repeats=3, min_minutes=0)
            bark_requests = bark.requests
        return {
            "url": thread["canonical_url"],
            "title": thread["title"],
            "body_excerpt": thread["body_excerpt"],
            "production_snapshot_match": production_snapshot_match,
            "fixture_inserted": fixture_inserted,
            "match_after_fixture": match_after_fixture,
            "first_telegram_message": first_message,
            "repeat_telegram_messages": repeat_messages,
            "bark_request_count": len(bark_requests),
            "bark_requests": bark_requests,
            "used_temp_db": True,
            "mutated_production_db": False,
        }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Simulate a full Chiphell electronics-arbitrage detection flow with a mocked Bark API.")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--source-id", default="2804212")
    ap.add_argument("--timeout-ms", type=int, default=45000)
    ap.add_argument("--settle-ms", type=int, default=3000)
    ap.add_argument("--fixture-quote-price", type=int, default=DEFAULT_FIXTURE_QUOTE_PRICE, help="Inserted into the temp DB only if the production snapshot does not already match. Use 0 to disable.")
    ap.add_argument("--fixture-product", default=DEFAULT_FIXTURE_PRODUCT)
    ap.add_argument("--fixture-brand", default=DEFAULT_FIXTURE_BRAND)
    ap.add_argument("--fixture-model-spec", default=DEFAULT_FIXTURE_MODEL_SPEC)
    ap.add_argument("--fixture-color", default=DEFAULT_FIXTURE_COLOR)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run_simulation(args)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("first_telegram_message") and result.get("bark_request_count", 0) >= 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
