"""Safe Gmail message extraction and the small rule DSL used by reviews.

Grok only produces data for this module.  This file deliberately contains no
shell, Python evaluation, network, or Gmail mutation primitives.
"""
from __future__ import annotations

import base64
import datetime as dt
import email.utils
from email.header import decode_header, make_header
from email.message import Message
import json
import copy
import hashlib
import re
from html.parser import HTMLParser
from typing import Any


ALLOWED_FIELDS = {
    "from", "to", "cc", "subject", "body", "labels", "attachment_names",
    "attachment_types", "attachment_sizes", "date",
}
ALLOWED_OPS = {"contains", "equals", "starts_with", "ends_with", "not_contains", "exists", "before", "after", "greater_than", "less_than"}
MAX_DEPTH = 5
MAX_NODES = 40

# Share the evaluator's vocabulary with the agent-facing output contract.
NODE_SCHEMA = {'anyOf': [
    {'type': 'object', 'additionalProperties': False,
     'properties': {'field': {'type': 'string', 'enum': sorted(ALLOWED_FIELDS)},
                    'op': {'type': 'string', 'enum': sorted(ALLOWED_OPS)},
                    'value': {'type': ['string', 'boolean', 'number']}},
     'required': ['field', 'op', 'value']},
    *[{'type': 'object', 'additionalProperties': False,
       'properties': {key: {'type': 'array', 'minItems': 1, 'maxItems': MAX_NODES,
                            'items': {'$ref': '#/$defs/gmail_node'}}},
       'required': [key]} for key in ('all', 'any', 'none')],
]}
PROGRAM_SCHEMA = {'type': 'object', 'additionalProperties': False,
                  'properties': {'version': {'type': 'integer', 'const': 1},
                                 **{key: {'type': 'array', 'maxItems': MAX_NODES,
                                          'items': {'$ref': '#/$defs/gmail_node'}}
                                    for key in ('all', 'any', 'none')}},
                  'required': ['version', 'all', 'any', 'none']}


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


def decode_body(value: str | None, charset: str = "utf-8") -> str:
    if not value:
        return ""
    try:
        raw = value.encode("ascii")
        decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
        return decoded.decode(charset, "strict")
    except (ValueError, LookupError, UnicodeError) as exc:
        raise ValueError("邮件正文解码失败，未使用不完整内容") from exc


def _headers(message: dict[str, Any]) -> dict[str, str]:
    result = {}
    for h in (message.get("payload") or {}).get("headers", []):
        if isinstance(h, dict):
            original_name = str(h.get("name") or "")
            name = original_name.lower()
            value = str(make_header(decode_header(str(h.get("value") or ""))))
            result[name] = (result[name] + "\n" + value) if name in result else value
            result[original_name] = result[name]
    return result


def fingerprint(message: dict[str, Any]) -> str:
    fields = {k: message.get(k) for k in ("id", "headers", "labels", "body", "attachments", "received_at_ms")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def get_message_full(service, message_id: str, *, max_text_bytes: int = 2 * 1024 * 1024) -> dict[str, Any]:
    """Read every MIME text body (including external text parts), never binary attachments.

    Oversized/undecodable bodies fail closed instead of silently truncating the
    input used to decide permanent deletion.
    """
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = _headers(message)
    plain, html_parts, attachments = [], [], []

    def walk(part):
        body = part.get("body") or {}
        mime = str(part.get("mimeType") or "").lower()
        filename = str(part.get("filename") or "")
        part_headers = {str(x.get("name", "")).lower(): str(x.get("value", "")) for x in part.get("headers", [])}
        attachment = filename or part_headers.get("content-disposition", "").lower().startswith("attachment")
        if filename or body.get("attachmentId"):
            attachments.append({"filename": filename, "mime_type": mime, "size": int(body.get("size") or 0)})
        if mime in {"text/plain", "text/html"}:
            if int(body.get("size") or 0) > max_text_bytes:
                raise ValueError("邮件完整文本超过样本上限，未截取或提交")
            encoded = body.get("data")
            if encoded is None and body.get("attachmentId"):
                encoded = service.users().messages().attachments().get(
                    userId="me", messageId=message_id, id=body["attachmentId"]).execute().get("data")
            content_type = Message()
            content_type["content-type"] = part_headers.get("content-type", mime)
            decoded = decode_body(encoded, content_type.get_content_charset() or "utf-8")
            (plain if mime == "text/plain" else html_parts).append(decoded)
        for child in part.get("parts") or []:
            walk(child)

    walk(message.get("payload") or {})
    body_text = "\n\n".join(plain).strip()
    html_text = "\n\n".join(html_to_text(x) for x in html_parts).strip()
    combined = body_text or html_text
    if body_text and html_text and html_text != body_text:
        combined += "\n\n" + html_text
    if len(combined.encode("utf-8")) > max_text_bytes:
        raise ValueError("邮件完整文本超过样本上限，未截取或提交")
    result = {"id": message_id, "headers": headers,
              **{key: headers.get(key, "") for key in ("from", "to", "cc", "subject", "date")},
              "labels": list(message.get("labelIds") or []), "body_text": body_text,
              "body_html_text": html_text, "body": combined, "attachments": attachments,
              "attachment_names": [x["filename"] for x in attachments],
              "attachment_types": [x["mime_type"] for x in attachments],
              "attachment_sizes": [x["size"] for x in attachments],
              "received_at_ms": int(message.get("internalDate") or 0)}
    result["sample_sha256"] = fingerprint(result)
    return result


def narrow_query(query: str) -> bool:
    # New DSL queries use a deliberately small, conjunction-only Gmail subset.
    # An exact sender is mandatory and OR/braces cannot bypass that anchor.
    if not isinstance(query, str) or len(query.encode()) > 1800:
        return False
    token_matches = list(re.finditer(r'(?:[^\s"{}()]+|"[^"\r\n]*")+', query))
    if not token_matches or any(query[a.end():b.start()].strip() for a, b in zip(token_matches, token_matches[1:])):
        return False
    tokens = [m.group(0) for m in token_matches]
    if "-is:starred" not in tokens:
        return False
    allowed = r'(?:-is:starred|from:[A-Za-z0-9._+%-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}|(?:-?subject:|after:|before:|newer_than:|older_than:|label:|category:)(?:"[^"\r\n]+"|[\w@.\-]+))'
    return (any(re.fullmatch(r'from:[^\s@]+@[^\s@]+', t) for t in tokens)
            and all(re.fullmatch(allowed, t) for t in tokens))


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
        if type(value) is not bool:
            raise ValueError("exists 条件必须使用布尔值")
    elif node["op"] in {"before", "after"}:
        if node["field"] != "date" or not parse_date_ms(str(value)):
            raise ValueError("日期条件必须使用有效日期")
    elif node["op"] in {"greater_than", "less_than"}:
        if node["field"] != "attachment_sizes" or type(value) not in (int, float) or value < 0:
            raise ValueError("附件大小条件必须使用非负字节数")
    elif not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 500:
        raise ValueError("Gmail 逻辑条件值不能为空或过长")
    return 1


def validate_program(program: Any, *, require_exclusion: bool = False) -> dict[str, Any]:
    if not isinstance(program, dict) or set(program) != {"version", "all", "any", "none"}:
        raise ValueError("Gmail 逻辑规则必须包含 version、all、any、none")
    if type(program["version"]) is not int or program["version"] != 1:
        raise ValueError("不支持的 Gmail 逻辑规则版本")
    for key in ("all", "any", "none"):
        if not isinstance(program[key], list):
            raise ValueError("Gmail 逻辑组合格式异常")
    if not program["all"] and not program["any"]:
        raise ValueError("Gmail 逻辑规则不能没有正向条件")
    if not program["all"] and len(program["any"]) > 3:
        raise ValueError("拒绝仅靠平铺关键词 OR 的宽泛 Gmail 规则")
    if require_exclusion and not program["none"]:
        raise ValueError("Gmail 逻辑规则必须包含明确的排除条件")
    def check_or(node):
        if not isinstance(node, dict):
            return
        if require_exclusion and "any" in node and len(node["any"]) > 1 and all(
            isinstance(x, dict) and x.get("field") in {"subject", "body"}
            and x.get("op") == "contains" for x in node["any"]):
            raise ValueError("拒绝平铺关键词 OR；请使用能区分邮件用途的组合逻辑")
        for key in ("all", "any", "none"):
            for child in node.get(key, []):
                check_or(child)
    check_or(program)
    total = sum(_node_count(node) for key in ("all", "any", "none") for node in program[key])
    if total > MAX_NODES:
        raise ValueError("Gmail 逻辑规则条件过多")
    return copy.deepcopy(program)


def _values(message: dict[str, Any], field: str) -> list[str]:
    if field in {"from", "to", "cc"}:
        from email.utils import getaddresses
        raw = str(message.get(field) or "")
        return [raw, *[address for _, address in getaddresses([raw]) if address]]
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
    if item["op"] in {"before", "after"}:
        observed = int(message.get("received_at_ms") or parse_date_ms(message.get("date", "")))
        return bool(observed) and (observed < parse_date_ms(item["value"]) if item["op"] == "before" else observed > parse_date_ms(item["value"]))
    if item["op"] in {"greater_than", "less_than"}:
        return any((x > item["value"] if item["op"] == "greater_than" else x < item["value"])
                   for x in message.get("attachment_sizes", [a.get("size", 0) for a in message.get("attachments", [])]))
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
