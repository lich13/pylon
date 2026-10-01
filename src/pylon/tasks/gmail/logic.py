"""Safe Gmail message extraction and the small rule DSL used by reviews.

Grok only produces data for this module.  This file deliberately contains no
shell, Python evaluation, network, or Gmail mutation primitives.
"""
from __future__ import annotations

import base64
import datetime as dt
import email.utils
import hashlib
import re
from html.parser import HTMLParser
from typing import Any


ALLOWED_FIELDS = {
    "from", "to", "cc", "subject", "body", "labels", "attachment_names",
    "attachment_types", "date",
}
ALLOWED_OPS = {"contains", "equals", "starts_with", "ends_with", "not_contains", "exists"}
MAX_DEPTH = 5
MAX_NODES = 40


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.skip += 1
        elif tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.skip:
            self.skip -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = _TextParser()
    try:
        parser.feed(value or "")
        parser.close()
    except Exception:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value or "")).strip()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", "".join(parser.parts))).strip()


def decode_body(value: str | None) -> str:
    if not value:
        return ""
    raw = value.encode("ascii", "ignore")
    raw += b"=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
    except Exception:
        return ""


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {str(h.get("name") or ""): str(h.get("value") or "")
            for h in (message.get("payload") or {}).get("headers", [])
            if isinstance(h, dict)}


def get_message_full(service, message_id: str, *, max_text_bytes: int = 512 * 1024) -> dict[str, Any]:
    """Read text parts and attachment metadata, never attachment bytes."""
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = _headers(message)
    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []

    def walk(part: dict[str, Any]):
        body = part.get("body") or {}
        mime = str(part.get("mimeType") or "").lower()
        filename = str(part.get("filename") or "")
        attachment_id = body.get("attachmentId")
        if filename or attachment_id or mime.startswith("application/") or mime.startswith("image/"):
            if filename or attachment_id:
                attachments.append({"filename": filename, "mime_type": mime,
                                    "size": int(body.get("size") or 0)})
        decoded = decode_body(body.get("data"))
        if decoded:
            if mime == "text/plain":
                plain.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(message.get("payload") or {})
    body_text = "\n\n".join(x for x in plain if x).strip()
    html_text = "\n\n".join(html_to_text(x) for x in html_parts if x).strip()
    combined = body_text or html_text
    if body_text and html_text and html_text not in body_text:
        combined += "\n\n" + html_text
    encoded = combined.encode("utf-8")
    if len(encoded) > max_text_bytes:
        combined = encoded[:max_text_bytes].decode("utf-8", "ignore")
    received = message.get("internalDate")
    try:
        received_at_ms = int(received) if received is not None else 0
    except (TypeError, ValueError):
        received_at_ms = 0
    return {
        "id": message_id,
        "headers": headers,
        "from": headers.get("From", ""),
        "to": headers.get("To", ""),
        "cc": headers.get("Cc", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "labels": list(message.get("labelIds") or []),
        "body_text": body_text,
        "body_html_text": html_text,
        "body": combined,
        "attachments": attachments,
        "attachment_names": [str(x.get("filename") or "") for x in attachments],
        "attachment_types": [str(x.get("mime_type") or "") for x in attachments],
        "received_at_ms": received_at_ms,
        "sample_sha256": hashlib.sha256(combined.encode("utf-8")).hexdigest(),
    }


def _node_count(node: Any, depth: int = 0) -> int:
    if depth > MAX_DEPTH:
        raise ValueError("Gmail 逻辑规则嵌套过深")
    if isinstance(node, dict) and len(node) == 1 and next(iter(node)) in {"all", "any", "none"}:
        key = next(iter(node))
        values = node[key]
        if not isinstance(values, list) or not values or len(values) > MAX_NODES:
            raise ValueError("Gmail 逻辑组合必须包含有限条件")
        return 1 + sum(_node_count(x, depth + 1) for x in values)
    if not isinstance(node, dict) or set(node) != {"field", "op", "value"}:
        raise ValueError("Gmail 逻辑条件字段异常")
    if node["field"] not in ALLOWED_FIELDS or node["op"] not in ALLOWED_OPS:
        raise ValueError("Gmail 逻辑条件字段或操作不受支持")
    value = node["value"]
    if node["op"] == "exists":
        if value not in (True, False):
            raise ValueError("exists 条件必须使用布尔值")
    elif not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 500:
        raise ValueError("Gmail 逻辑条件值不能为空或过长")
    return 1


def validate_program(program: Any) -> dict[str, Any]:
    if not isinstance(program, dict) or set(program) != {"version", "all", "any", "none"}:
        raise ValueError("Gmail 逻辑规则必须包含 version、all、any、none")
    if program["version"] != 1:
        raise ValueError("不支持的 Gmail 逻辑规则版本")
    for key in ("all", "any", "none"):
        if not isinstance(program[key], list):
            raise ValueError("Gmail 逻辑组合格式异常")
    if not program["all"] and not program["any"]:
        raise ValueError("Gmail 逻辑规则不能没有正向条件")
    if not program["all"] and len(program["any"]) > 3:
        raise ValueError("拒绝仅靠平铺关键词 OR 的宽泛 Gmail 规则")
    total = sum(_node_count(node) for key in ("all", "any", "none") for node in program[key])
    if total > MAX_NODES:
        raise ValueError("Gmail 逻辑规则条件过多")
    return {
        "version": 1,
        "all": [dict(x) if isinstance(x, dict) else x for x in program["all"]],
        "any": [dict(x) if isinstance(x, dict) else x for x in program["any"]],
        "none": [dict(x) if isinstance(x, dict) else x for x in program["none"]],
    }


def _values(message: dict[str, Any], field: str) -> list[str]:
    if field == "body":
        return [str(message.get("body") or ""), str(message.get("body_text") or ""), str(message.get("body_html_text") or "")]
    if field == "labels":
        return [str(x) for x in message.get("labels") or []]
    if field == "attachment_names":
        return [str(x) for x in message.get("attachment_names") or [a.get("filename", "") for a in message.get("attachments") or []]]
    if field == "attachment_types":
        return [str(x) for x in message.get("attachment_types") or [a.get("mime_type", "") for a in message.get("attachments") or []]]
    return [str(message.get(field) or "")]


def _predicate(message: dict[str, Any], item: dict[str, Any]) -> bool:
    values = _values(message, item["field"])
    op, wanted = item["op"], str(item["value"]).casefold()
    present = any(v != "" for v in values)
    if op == "exists":
        return present is bool(item["value"])
    folded = [v.casefold() for v in values]
    if op == "contains":
        return any(wanted in value for value in folded)
    if op == "not_contains":
        return all(wanted not in value for value in folded)
    if op == "equals":
        return any(value == wanted for value in folded)
    if op == "starts_with":
        return any(value.startswith(wanted) for value in folded)
    if op == "ends_with":
        return any(value.endswith(wanted) for value in folded)
    return False


def matches(program: dict[str, Any], message: dict[str, Any]) -> bool:
    program = validate_program(program)

    def evaluate(node: dict[str, Any], depth: int = 0) -> bool:
        if depth > MAX_DEPTH:
            return False
        if "field" in node:
            return _predicate(message, node)
        if "all" in node:
            return all(evaluate(x, depth + 1) for x in node["all"])
        if "any" in node:
            return any(evaluate(x, depth + 1) for x in node["any"])
        if "none" in node:
            return not any(evaluate(x, depth + 1) for x in node["none"])
        return False

    return (all(evaluate(x) for x in program["all"])
            and (not program["any"] or any(evaluate(x) for x in program["any"]))
            and not any(evaluate(x) for x in program["none"]))


def parse_date_ms(value: str) -> int:
    try:
        text = str(value or "").strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1000)
    except Exception:
        try:
            return int(email.utils.parsedate_to_datetime(value).timestamp() * 1000)
        except Exception:
            return 0
