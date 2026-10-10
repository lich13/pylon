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
                and parsed.hostname == 't.me')
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
    """Compatibility boundary: Helper summaries must never cause site requests."""
    return []


def official_evidence(snapshot: dict, *, getter=fetch) -> list[dict]:
    """Links are retained for the owner to open, never followed for enrichment."""
    return []
