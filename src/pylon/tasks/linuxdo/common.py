from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

JOB_ID = 'b7a84d1d9f21'
CHANNELS = ('LinuxDoNew', 'linux_do_channel', 'linuxdo_tg')
TOPIC_RE = re.compile(r'https?://linux\.do/t/(?:topic|[^/\s<>]+)/(\d+)(?:/(\d+))?(?=[/?#\s<"\')\]]|$)', re.I)
URL_RE = re.compile(r'https?://[^\s<>"\u3000]+')


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def clean(text: str, limit: int = 24000) -> str:
    text = html.unescape(str(text or ''))
    text = re.sub(r'[\u200b-\u200f\u202a-\u202e\u2060\ufeff]', '', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    text = re.sub(r'(?i)\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|\d{6,}:[A-Za-z0-9_-]{24,})', '[REDACTED]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b', '[REDACTED]', text)
    text = re.sub(r'(?i)(api[_ -]?key|access_token|refresh_token|authorization|password|密码|密钥)\s*[:=：]\s*[^\s,;，；]+', r'\1=[REDACTED]', text)
    text = re.sub(r'[A-Za-z0-9_.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[EMAIL]', text)
    return re.sub(r'[ \t]+', ' ', text).strip()[:limit]


def safe_url(url: str) -> str:
    try:
        parts = urlsplit(html.unescape(url).rstrip('.,，。;；'))
        if parts.scheme not in {'http', 'https'} or not parts.hostname or parts.username or parts.password:
            return ''
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not re.search(r'(?i)token|password|api.?key|secret|auth|signature', k)]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))
    except ValueError:
        return ''


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style'}:
            self.hidden += 1
        if tag in {'br', 'p', 'div', 'li', 'blockquote', 'pre', 'h1', 'h2', 'h3'}:
            self.parts.append('\n')
        if tag == 'a' and not self.hidden:
            url = safe_url(attrs.get('href', ''))
            if url:
                self.links.append(url)

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'div', 'li', 'blockquote', 'pre'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_html(value: str) -> tuple[str, list[str]]:
    parser = PlainHTML()
    parser.feed(value)
    return clean(''.join(parser.parts)), list(dict.fromkeys(parser.links))


def timestamp(value: str) -> float | None:
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            return None
        return date.timestamp()
    except (ValueError, TypeError, OverflowError):
        return None


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def availability_position(facts: list[dict], positions: dict[str, int]) -> int:
    """Canonical reply number, shared by channel copies of a replenishment event."""
    numbers = []
    for fact in facts:
        if fact.get('kind') != 'availability':
            continue
        for citation in fact.get('evidence', []):
            ref = citation.get('ref', '')
            number = positions.get(ref)
            original = re.fullmatch(r'topic:(\d+)', ref)
            if number is None and original:
                number = int(original[1])
            if type(number) is int and number > 0:
                numbers.append(number)
    return max(numbers, default=0)
