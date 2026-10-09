from __future__ import annotations

import html
import re
from dataclasses import asdict, dataclass, field

from .common import CHANNELS, TOPIC_RE, clean, plain_html, safe_url


@dataclass(frozen=True)
class Observation:
    channel: str
    message_id: int
    topic_id: str
    post_number: int
    title: str
    body: str
    tags: list[str]
    published_at: str
    source_url: str
    links: list[str] = field(default_factory=list)
    edited_at: str = ''

    def payload(self) -> dict:
        return asdict(self)


@dataclass
class Page:
    observations: list[Observation] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    unparsed: int = 0


def _field(text: str, label: str, following: str = '') -> str:
    end = r'(?=\s*(?:' + following + r')\s*[：:]|$)' if following else '$'
    match = re.search(r'(?:^|\s)(?:' + label + r')\s*[：:]\s*(.*?)' + end, text, re.S)
    return clean(match[1]) if match else ''


def _canonical(text: str, markup: str, structured: bool):
    # A quoted topic inside a preview must never replace the labelled permalink.
    if structured:
        if re.match(r'\s*主\s*题\s*[：:]', text):
            matches = list(re.finditer(r'直达链接\s*[：:]\s*(' + TOPIC_RE.pattern + ')', text, re.I))
            if matches:
                return TOPIC_RE.search(matches[-1][1])
        else:
            header = re.split(r'(?:摘要|内容预览)\s*[：:]', text, maxsplit=1)[0]
            match = re.search(r'(?:帖子|原帖)\s*[：:]\s*(' + TOPIC_RE.pattern + ')', header, re.I)
            if match:
                return TOPIC_RE.search(match[1])
    anchors = re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', markup, re.I | re.S)
    for href, label in anchors:
        if re.fullmatch(r'\s*(?:原帖|直达链接|帖子)\s*', plain_html(label)[0]):
            match = TOPIC_RE.search(html.unescape(href))
            if match:
                return match
    if not structured and '中发帖' in markup:
        prefix = markup.split('中发帖', 1)[0]
        matches = [TOPIC_RE.search(html.unescape(url)) for url in re.findall(r'href=["\']([^"\']+)', prefix)]
        matches = [m for m in matches if m]
        if matches:
            return matches[-1]
    return None


def parse_page(html_text: str, channel: str) -> Page:
    if channel not in CHANNELS:
        raise ValueError('unknown Linux.do channel')
    page = Page()
    chunks = re.split(r'data-post=["\']' + re.escape(channel) + r'/(\d+)["\']', html_text)
    for index in range(1, len(chunks), 2):
        message_id, chunk = int(chunks[index]), chunks[index + 1]
        page.message_ids.append(message_id)
        match = re.search(r'<div\b[^>]*class=["\'][^"\']*\btgme_widget_message_text\b[^"\']*["\'][^>]*>(.*?)</div>', chunk, re.S | re.I)
        if not match:
            page.unparsed += 1
            continue
        markup = match[1]
        text, links = plain_html(markup)
        structured = bool(re.match(r'\s*(?:标题|主\s*题)\s*[：:]', text))
        topic = _canonical(text, markup, structured)
        if not topic:
            page.unparsed += 1
            continue
        if structured:
            title = _field(text, r'标题|主\s*题', r'作者|发\s*布\s*者')
            tags = _field(text, r'板块|标签分类', r'编号|时\s*间')
            body = _field(text, r'摘要|内容预览', r'直达链接')
        else:
            parts = re.search(r'\s在\s+(.*?)\s+中发帖\s*(.*)$', text, re.S)
            if not parts:
                page.unparsed += 1
                continue
            title, body, tags = clean(parts[1]), clean(parts[2]), ''
        if not title:
            page.unparsed += 1
            continue
        posted = re.search(r'<time\b[^>]*datetime=["\']([^"\']+)', chunk, re.I)
        page.observations.append(Observation(
            channel, message_id, topic[1], int(topic[2] or 1), title[:300], body,
            [tag.lstrip('#') for tag in re.split(r'\s+|[,，]', tags) if tag],
            posted[1] if posted else '', f'https://t.me/{channel}/{message_id}',
            [url for url in links if url and not TOPIC_RE.search(url)],
        ))
    page.message_ids = sorted(set(page.message_ids), reverse=True)
    page.observations.sort(key=lambda post: post.message_id, reverse=True)
    return page
