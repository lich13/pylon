from __future__ import annotations

import json
import time
import urllib.request
from urllib.parse import urlsplit

from .common import clean, plain_html
from .judge import provider_for_url


class FetchError(RuntimeError):
    pass


def permitted(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.port in {None, 443}
                and not parsed.username and not parsed.password
                and (parsed.hostname in {'t.me', 'linux.do'} or bool(provider_for_url(url))))
    except ValueError:
        return False


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not permitted(newurl):
            raise FetchError('redirect outside evidence sources')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, *, timeout: int = 8) -> str:
    if not permitted(url):
        raise FetchError('URL outside evidence sources')
    try:
        opener = urllib.request.build_opener(PublicRedirect())
        request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'text/html,application/json'})
        with opener.open(request, timeout=timeout) as response:
            body = response.read(2_000_001)
            if len(body) > 2_000_000:
                raise FetchError('evidence response too large')
            return body.decode('utf-8', errors='replace')
    except FetchError:
        raise
    except Exception as exc:
        # URLs and remote response bodies may contain credentials. Persist only the class/code.
        code = getattr(exc, 'code', '')
        raise FetchError(f'{type(exc).__name__}:{code}') from None


def topic_evidence(topic_id: str, post_numbers: list[int], *, getter=fetch) -> list[dict]:
    if not topic_id.isdigit():
        raise ValueError('invalid topic id')
    url = f'https://linux.do/t/topic/{topic_id}.json'
    data = json.loads(getter(url))
    if not isinstance(data, dict) or str(data.get('id')) != topic_id:
        raise FetchError('topic identity mismatch')
    rows = data.get('post_stream', {}).get('posts', [])
    # A forwarded reply may be outside the initial topic stream page.
    present = {p['post_number'] for p in rows if isinstance(p, dict) and type(p.get('post_number')) is int}
    for number in sorted(set(post_numbers) - present - {1})[-2:]:
        reply = json.loads(getter(f'https://linux.do/t/topic/{topic_id}/{number}.json'))
        if str(reply.get('id')) != topic_id:
            raise FetchError('reply topic identity mismatch')
        rows.extend(reply.get('post_stream', {}).get('posts', []))
    result, seen = [], set()
    wanted = {1, *post_numbers}
    for post in rows:
        if not isinstance(post, dict) or not isinstance(post.get('post_number'), int):
            continue
        number = post['post_number']
        # Include recent replies as potential answers, with bounded work per topic.
        if number in seen or (number not in wanted and number < max(present or {1}) - 8):
            continue
        seen.add(number)
        body, links = plain_html(str(post.get('cooked') or ''))
        if not body:
            continue
        result.append({'ref': f'topic:{number}', 'origin': 'topic', 'post_number': number,
                       'url': f'https://linux.do/t/topic/{topic_id}/{number}',
                       'title': clean(str(data.get('title') or ''), 300),
                       'body': body, 'links': links, 'complete': True})
    if not result:
        raise FetchError('topic body unavailable')
    return result


def official_evidence(snapshot: dict, *, getter=fetch) -> list[dict]:
    from .common import digest
    urls = sorted({url for row in snapshot['evidence'] for url in row['links'] if provider_for_url(url)})
    result = []
    for url in urls[:2]:
        try:
            body, links = plain_html(getter(url))
        except Exception:
            continue
        if len(body) < 80:
            continue
        result.append({'ref': 'official:' + digest(url)[:16], 'origin': 'official', 'post_number': 1,
                       'url': url, 'title': '', 'body': body, 'links': links, 'complete': True})
    return result
