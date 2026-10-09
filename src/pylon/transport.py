from __future__ import annotations

import html
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from markdown_it import MarkdownIt

from .config import profile_env, redact
from .outbox import Outbox


class DeliveryError(Exception):
    def __init__(self, status: str, message: str, delay: float = 0):
        super().__init__(message)
        self.status, self.delay = status, delay


class SplitHTML(HTMLParser):
    def __init__(self, limit=3500):
        super().__init__(convert_charrefs=True)
        self.limit, self.count = limit, 0
        self.stack, self.parts, self.buf = [], [], []

    def flush(self):
        if self.count:
            self.parts.append("".join(self.buf) + "".join(f"</{t}>" for t, _ in reversed(self.stack)))
        self.buf = [markup for _, markup in self.stack]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.handle_data("\n")
            return
        if tag not in {"b", "strong", "i", "em", "u", "s", "code", "pre", "a", "blockquote"}:
            return
        markup = f"<{tag}>"
        if tag == "a":
            href = dict(attrs).get("href", "")
            if urlsplit(href).scheme not in {"http", "https", "tg", "mailto"}:
                return
            markup = f'<a href="{html.escape(href, quote=True)}">'
        self.stack.append((tag, markup))
        self.buf.append(markup)

    def handle_endtag(self, tag):
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            self.buf.append(f"</{tag}>")

    def handle_data(self, data):
        for char in data:
            width = 2 if ord(char) > 0xffff else 1
            if self.count + width > self.limit:
                self.flush()
            self.buf.append(html.escape(char, quote=False))
            self.count += width


def telegram_chunks(text: str) -> list[str]:
    md = MarkdownIt("commonmark", {"html": False, "breaks": True})
    lists: list[int | None] = []

    def list_open(tokens, idx, options, env):
        token = tokens[idx]
        lists.append(int(token.attrGet("start") or 1) if token.type == "ordered_list_open" else None)
        return ""

    def list_close(tokens, idx, options, env):
        lists.pop()
        return "\n"

    def list_item_open(tokens, idx, options, env):
        number = lists[-1]
        if number is not None:
            lists[-1] = number + 1
        return "  " * (len(lists) - 1) + (f"{number}. " if number is not None else "• ")

    for key, value in {"softbreak": "\n", "hardbreak": "\n", "paragraph_open": "", "paragraph_close": "\n", "heading_open": "<b>", "heading_close": "</b>\n", "list_item_close": "\n", "hr": "\n"}.items():
        md.renderer.rules[key] = lambda tokens, idx, options, env, v=value: v
    for kind in ("bullet", "ordered"):
        md.renderer.rules[f"{kind}_list_open"] = list_open
        md.renderer.rules[f"{kind}_list_close"] = list_close
    md.renderer.rules["list_item_open"] = list_item_open
    md.renderer.rules["fence"] = lambda tokens, idx, options, env: "<pre>" + html.escape(tokens[idx].content) + "</pre>\n"
    md.renderer.rules["code_block"] = md.renderer.rules["fence"]
    splitter = SplitHTML()
    splitter.feed(md.render(text).strip())
    splitter.flush()
    return splitter.parts


def client() -> httpx.Client:
    # Use IPv4 on the existing host, avoiding unusable public IPv6 routes.
    return httpx.Client(transport=httpx.HTTPTransport(local_address="0.0.0.0"),
                        timeout=httpx.Timeout(45, connect=10), trust_env=False)


def telegram_request(http: httpx.Client, token: str, method: str, *, data=None, files=None, read_only=False):
    try:
        response = http.post(f"https://api.telegram.org/bot{token}/{method}", data=data, files=files)
    except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
        raise DeliveryError("pending", type(exc).__name__, 30) from None
    except httpx.HTTPError as exc:
        raise DeliveryError("pending" if read_only else "uncertain", type(exc).__name__, 30) from None
    try:
        value = response.json()
    except ValueError:
        raise DeliveryError("pending" if read_only else "uncertain", f"Telegram HTTP {response.status_code}: invalid response", 30) from None
    if value.get("ok"):
        return value.get("result")
    code = int(value.get("error_code") or response.status_code)
    detail = str(value.get("description") or "request failed").replace(token, "[TOKEN]")
    if code == 429:
        raise DeliveryError("pending", detail, max(1, int(value.get("parameters", {}).get("retry_after", 30))))
    raise DeliveryError("uncertain" if code >= 500 else "failed", redact(detail))


def deliver_event(box: Outbox, event: dict, http: httpx.Client):
    payload = json.loads(event["payload"])
    receipt = json.loads(event["receipt"] or "null")
    def check_expiration():
        expiry = payload.get('expires_at')
        if type(expiry) in (int, float) and expiry <= time.time():
            raise DeliveryError('suppressed', 'notification expired')
    try:
        check_expiration()
        if event["kind"] == "bark":
            try:
                response = http.post(payload["api_base"].rstrip("/") + "/push", json=payload["body"])
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise DeliveryError("pending", type(exc).__name__, 30) from None
            except httpx.HTTPError as exc:
                raise DeliveryError("uncertain", type(exc).__name__) from None
            if response.status_code == 429:
                raise DeliveryError("pending", "Bark rate limit", 60)
            try:
                value = response.json()
            except ValueError:
                raise DeliveryError("uncertain", f"Bark HTTP {response.status_code}: invalid response") from None
            if not (200 <= response.status_code < 300 and value.get("code") in (None, 0, 200, "200")):
                raise DeliveryError("uncertain" if response.status_code >= 500 else "failed", f"Bark HTTP {response.status_code}")
            receipt = {"http_status": response.status_code, "code": value.get("code")}
        else:
            env = profile_env(event["profile"])
            token, chat = env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_HOME_CHANNEL"]
            if event["kind"] == "document":
                path = Path(payload["path"])
                import hashlib
                if hashlib.sha256(path.read_bytes()).hexdigest() != payload["sha256"]:
                    raise DeliveryError("failed", "attachment checksum mismatch")
                with path.open("rb") as f:
                    result = telegram_request(http, token, "sendDocument", data={"chat_id": chat, "caption": payload.get("caption", "")[:1024]}, files={"document": (path.name, f, "text/html")})
                receipt = {"message_id": result["message_id"], "chat_id": result["chat"]["id"], "document": result.get("document", {})}
            elif event["kind"] == "text":
                receipt = receipt or []
                chunks = telegram_chunks(payload["text"])
                for chunk in chunks[len(receipt):]:
                    check_expiration()
                    data = {"chat_id": chat, "text": chunk, "parse_mode": "HTML", "link_preview_options": '{"is_disabled":true}'}
                    if payload.get("reply_markup") and len(receipt) + 1 == len(chunks):
                        data["reply_markup"] = json.dumps(payload["reply_markup"], ensure_ascii=False, separators=(",", ":"))
                    if payload.get("reply_to_message_id") and not receipt:
                        data["reply_parameters"] = json.dumps({"message_id": payload["reply_to_message_id"]})
                    result = telegram_request(http, token, "sendMessage", data=data)
                    receipt.append({"message_id": result["message_id"], "chat_id": result["chat"]["id"]})
                    # A confirmed chunk is never repeated after a later chunk fails.
                    box.finish(event["id"], "sending", receipt=receipt)
                    if len(chunks) > 1:
                        time.sleep(1.1)
            else:
                raise DeliveryError("failed", "unsupported message kind")
        box.finish(event["id"], "delivered", receipt=receipt)
    except DeliveryError as exc:
        delay = exc.delay or min(1800, 15 * 2 ** min(event["attempts"], 7))
        box.finish(event["id"], exc.status, error=redact(str(exc)), delay=delay)
    except Exception as exc:
        box.finish(event["id"], "failed", error=redact(f"{type(exc).__name__}: {exc}"))


def drain(box: Outbox, http: httpx.Client, limit: int = 30) -> int:
    count = 0
    for _ in range(limit):
        row = box.claim()
        if row is None:
            break
        deliver_event(box, row, http)
        count += 1
    return count
