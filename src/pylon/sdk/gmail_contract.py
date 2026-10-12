"""Versioned, pure data grammar accepted by the privileged agent adapter."""
from typing import Any
import copy

ALLOWED_FIELDS = {
    "from", "to", "cc", "subject", "body", "labels", "attachment_names",
    "attachment_types", "attachment_sizes", "date",
}

ALLOWED_OPS = {"contains", "equals", "starts_with", "ends_with", "not_contains", "exists", "before", "after", "greater_than", "less_than"}

MAX_DEPTH = 5

MAX_NODES = 40

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


import datetime as dt
import email.utils

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
