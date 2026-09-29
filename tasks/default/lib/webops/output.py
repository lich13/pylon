from __future__ import annotations

import json
import re
from typing import Any

WAKE_AGENT_FALSE = '{"wakeAgent": false}'
SILENT_MARKER = '[SILENT]'
INVISIBLE_PROMPT_CHARS_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u202a-\u202e]")


def wake_agent_false() -> str:
    return WAKE_AGENT_FALSE


def silent() -> str:
    return SILENT_MARKER


def strip_invisible_prompt_chars(text: str) -> str:
    return INVISIBLE_PROMPT_CHARS_RE.sub("", str(text))


def sanitize_for_prompt(value: Any) -> Any:
    if isinstance(value, str):
        return strip_invisible_prompt_chars(value)
    if isinstance(value, list):
        return [sanitize_for_prompt(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_for_prompt(item) for item in value)
    if isinstance(value, dict):
        return {sanitize_for_prompt(key): sanitize_for_prompt(val) for key, val in value.items()}
    return value


def json_dumps_compact(value: Any) -> str:
    return json.dumps(sanitize_for_prompt(value), ensure_ascii=False, separators=(',', ':'))


def json_dumps_pretty(value: Any, *, sort_keys: bool = False) -> str:
    return json.dumps(sanitize_for_prompt(value), ensure_ascii=False, indent=2, sort_keys=sort_keys)
