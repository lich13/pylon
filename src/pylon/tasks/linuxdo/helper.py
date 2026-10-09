"""Helper message normalization and deterministic notification routing."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from urllib.parse import parse_qs, unquote, urlsplit

from .common import URL_RE, clean, digest, safe_url
from .sources import Observation

SOURCE = 'linux_do_helper_bot'
PARSER_VERSION = 'helper-20261009-5'
FOLLOWING = {'following_posted', 'following_replied'}
PERSONAL = {'following_posted', 'following_replied', 'private_message', 'replied', 'mentioned',
            'quoted', 'group_mentioned', 'linked', 'posted', 'event_reminder', 'event_invitation'}
BOARD = {'watching_first_post', 'watching_category_or_tag'}
LABELS = {
    '关注者发帖': 'following_posted', '关注者回复': 'following_replied', '私信': 'private_message',
    '回复': 'replied', '提及': 'mentioned', '引用': 'quoted', '群组提及': 'group_mentioned',
    '被引用链接': 'linked', '关注主题回复': 'posted', '活动提醒': 'event_reminder',
    '活动邀请': 'event_invitation', '关注首帖': 'watching_first_post', '板块订阅': 'watching_category_or_tag',
}


def envelope(message, bot_id: int) -> dict:
    text = getattr(message, 'message', '') or ''
    incoming = (not getattr(message, 'out', False) and getattr(message, 'sender_id', None) == bot_id
                and getattr(getattr(message, 'peer_id', None), 'user_id', None) == bot_id)
    links = []
    encoded = text.encode('utf-16-le')
    for entity in getattr(message, 'entities', None) or []:
        start, end = entity.offset * 2, (entity.offset + entity.length) * 2
        label = encoded[start:end].decode('utf-16-le', errors='replace')
        url = getattr(entity, 'url', '')
        if type(entity).__name__ == 'MessageEntityUrl':
            url = label
        if safe_url(url):
            line = encoded[:start].decode('utf-16-le', errors='replace').count('\n') + 1
            links.append({'label': label, 'url': safe_url(url), 'header': line == 1, 'line': line})
    buttons = []
    for row in getattr(getattr(message, 'reply_markup', None), 'rows', None) or []:
        for button in row.buttons:
            action = getattr(button, 'type', button)
            url = safe_url(getattr(action, 'url', ''))
            if url:
                buttons.append({'label': button.text, 'url': url})
    return {'message_id': message.id, 'incoming': incoming, 'text': text,
            'published_at': message.date.isoformat() if getattr(message, 'date', None) else '',
            'edited_at': message.edit_date.isoformat() if getattr(message, 'edit_date', None) else '',
            'links': links, 'buttons': buttons}


@dataclass(frozen=True)
class HelperNotice:
    message_id: int
    kind: str
    route: str
    text: str
    title: str
    actor: str
    topic_id: str
    post_number: int
    url: str
    links: list[str]
    published_at: str
    edited_at: str
    notification_id: str = ''

    def payload(self) -> dict:
        return asdict(self)

    def post_fingerprint(self) -> str:
        return post_fingerprint(self.title, self.text)

    def event_key(self) -> str:
        if self.notification_id:
            identity = ['notification', self.notification_id]
        elif self.topic_id and self.post_number > 0 and self.actor:
            identity = [self.kind, self.topic_id, self.post_number, self.actor.casefold()]
        else:
            identity = ['message', self.message_id]
        return digest([SOURCE, identity])

    def observation(self) -> Observation:
        if self.route not in {'board', 'following'} or not self.topic_id or self.post_number < 1:
            raise ValueError('only identified board notices become model evidence')
        return Observation(SOURCE, self.message_id, self.topic_id, self.post_number, clean(self.title, 300),
                           clean(self.text), [], self.published_at, self.url,
                           [url for url in self.links if not _topic_link(url)], self.edited_at)


def _kind(header: str) -> str:
    names = '|'.join(re.escape(name) for name in [*PERSONAL, *BOARD, *LABELS])
    explicit = re.match(r'^(?:通知类型[：:]\s*|[【\[])(?P<kind>' + names + r')(?:[】\]]|\s*$)', header, re.I)
    if explicit:
        value = explicit['kind'].casefold()
        return LABELS.get(value, value)
    # Anchored notification headers only. Body prose never selects a personal route.
    templates = [
        ('private_message', r'^.{1,100}(?:给你发送了私信|向你发送了私信|sent you a (?:private )?message)'),
        ('following_posted', r'^(?:你关注的|您关注的|关注的用户).{1,100}(?:发布了|发表了|发帖)'),
        ('following_replied', r'^(?:你关注的|您关注的|关注的用户).{1,100}(?:回复了|回帖)'),
        ('replied', r'^.{1,100}(?:回复了你(?:的帖子)?|回复了您(?:的帖子)?|replied to (?:you|your))'),
        ('mentioned', r'^.{1,100}(?:提及了你|提到了你|提及了您|mentioned you)'),
        ('quoted', r'^.{1,100}(?:引用了你|引用了您|quoted you)'),
        ('linked', r'^.{1,100}(?:链接到了你的帖子|linked to your)'),
    ]
    for kind, pattern in templates:
        if re.search(pattern, header, re.I):
            return kind
    return 'unknown'


def _topic_link(url: str):
    parts = urlsplit(url)
    if parts.hostname != 'linux.do':
        return None
    # Helper buttons omit the slug; parse these before the slugged title link.
    # Otherwise /t/123/4 would be mistaken for topic 4 with a numeric slug.
    return (re.fullmatch(r'/t/([0-9]+)(?:/([0-9]+))?/?', parts.path)
            or re.fullmatch(r'/t/[^/\s<>]+/([0-9]+)(?:/([0-9]+))?/?', parts.path))


def _template_kind(header: str, actor_links: list[dict]) -> str:
    if len(actor_links) != 1:
        return 'unknown'
    header = header.replace(actor_links[0]['label'], '<actor>').replace('\ufe0f', '')
    header = re.sub(r'\s+', ' ', header).strip()
    templates = {
        '✉ <actor> 发给你 一条私信': 'private_message',
        '↩ <actor> 回复 给你': 'replied',
        '💬 <actor> 提及 你': 'mentioned',
        '💬 <actor> 引用 你': 'quoted',
        '🔗 <actor> 链接 你的帖子': 'linked',
        '💬 <actor> 发布': 'posted',
        '💬 <actor> 发布了': 'published',
    }
    return templates.get(header, 'unknown')


def parse_message(raw: dict, *, followed: frozenset[str] | None = None) -> HelperNotice:
    text = str(raw.get('text') or '').replace('\r\n', '\n').strip()
    header = text.split('\n', 1)[0].strip()
    kind = _kind(header)
    links = raw.get('links', [])
    buttons = raw.get('buttons', [])
    actor_links = [row for row in links if row.get('header') and row.get('line', 1) == 1
                   and urlsplit(row['url']).hostname == 'linux.do'
                   and re.match(r'^/u/[^/]+/?$', urlsplit(row['url']).path)]
    template_kind = _template_kind(header, actor_links)
    if template_kind not in {'unknown', 'published'}:
        kind = template_kind
    route = 'personal' if kind in PERSONAL else 'board' if kind in BOARD else 'unknown'
    if not raw.get('incoming') or not text:
        kind, route = 'ignored', 'silent'
    elif (re.match(r'^(?:欢迎使用\s*(?:LINUX DO|Linux Do)|Welcome to LINUX DO|操作成功[。！!]?\s*$|回复成功[。！!]?\s*$|点赞成功[。！!]?\s*$)', header, re.I)
          or re.fullmatch(r'Hi [^\n,]+, 当你在 LINUX DO 上有新的通知时，我会给你留言。', header)
          or re.match(r'^(?:[✅❌]\s*)?(?:回复成功\s*[-—]|回复失败[：:]|在处理你的回复时发生了一个错误|你已经赞过这个帖子|取消点赞(?:成功|失败))', header)):
        kind, route = 'service', 'silent'

    # Prefer the bot's explicit action URL, then the notification header's topic link.
    candidates = [row for row in buttons if re.search(r'在线查看|查看原帖|原帖|查看话题|view (?:topic|post)', row['label'], re.I)]
    candidates += [row for row in links if row.get('header')]
    second_line = text.split('\n')[1].strip() if '\n' in text else ''
    topic_headers = [row for row in links if not row.get('header') and row.get('line', 2) == 2
                     and second_line in {row['label'], '在 ' + row['label'], '从 ' + row['label']}]
    if template_kind != 'unknown':
        candidates += topic_headers
    permalink = next((row for row in candidates if _topic_link(row['url'])), None)
    topic = _topic_link(permalink['url']) if permalink else None
    url = safe_url(permalink['url']) if permalink else ''
    title_links = [row for row in links if (row.get('header') or row in topic_headers) and (match := _topic_link(row['url']))
                   and topic and match[1] == topic[1]]
    title = title_links[0]['label'] if title_links else header
    actor = unquote(urlsplit(actor_links[0]['url']).path.strip('/').split('/')[1]) if actor_links else ''
    if actor and not re.fullmatch(r'[\w.-]{1,100}', actor):
        actor = ''
    linked_actor = actor if len(actor_links) == 1 else ''
    if not actor:
        match = re.search(r'^(?:[【\[][^】\]]+[】\]]\s*)?@([A-Za-z0-9_.-]+)(?=\s|$)', header)
        actor = match[1] if match else ''
    # The live Helper uses exactly the same "published" template for board and
    # followed-author posts. Only an explicitly imported complete list resolves it.
    number = int(topic[2] or 1) if topic else 0
    if raw.get('incoming') and followed is not None and linked_actor and (template_kind == 'published' or route == 'board'):
        if linked_actor.casefold() in followed:
            kind = 'following_replied' if number > 1 else 'following_posted'
            route = 'personal'
        elif template_kind == 'published' and number == 1:
            kind, route = 'watching_first_post', 'board'
    if route == 'board' and not topic:
        route = 'unknown'
    if route == 'personal' and kind in FOLLOWING:
        route = 'following' if topic and actor else 'unknown'
    all_links = list(dict.fromkeys(filter(None, [safe_url(row['url']) for row in [*links, *buttons]]
                                               + [safe_url(item) for item in URL_RE.findall(text)])))
    notification_id = ''
    for link in candidates:
        parts = urlsplit(link['url'])
        if parts.hostname == 'linux.do':
            value = parse_qs(parts.query).get('notification_id', [''])[0]
            if value.isdigit():
                notification_id = value
                break
    return HelperNotice(int(raw['message_id']), kind, route, text, title, actor,
                        topic[1] if topic else '', number,
                        url, all_links, raw.get('published_at', ''), raw.get('edited_at', ''), notification_id)


def format_personal(notice: HelperNotice) -> str:
    # Escape Markdown rather than interpreting user-authored body markup as bot controls.
    escaped = re.sub(r'([\\`*_{}\[\]<>])', r'\\\1', notice.text)
    label = next((label for label, kind in LABELS.items() if kind == notice.kind), '个人提醒')
    lines = [f'## Linux.do · {label}', escaped]
    if notice.url and notice.url not in notice.text:
        lines.append('原帖：' + notice.url)
    for url in notice.links:
        if url != notice.url and url not in notice.text:
            lines.append(url)
    lines.append('Helper：https://t.me/linux_do_helper_bot')
    return '\n'.join(lines)


def post_fingerprint(title: str, text: str) -> str:
    # Notification headers differ across subscriptions; their post previews do not.
    body = text.partition('\n')[2]
    first, _, remaining = body.partition('\n')
    if first.strip() in {title, '在 ' + title, '从 ' + title}:
        body = remaining
    return digest([clean(title), clean(body)]) if body.strip() else ''
