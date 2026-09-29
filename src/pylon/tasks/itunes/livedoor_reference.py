from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


SOURCE_URL = "http://blog.livedoor.jp/m_79sc/"
DATE_URL_TEMPLATE = "http://blog.livedoor.jp/m_79sc/archives/{release_date}.html"
JINA_DATE_URL_TEMPLATE = "https://r.jina.ai/http://blog.livedoor.jp/m_79sc/archives/{release_date}.html"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"


@dataclass(frozen=True)
class LivedoorEntry:
    album_id: str
    title: str
    url: str
    body_text: str
    track_id: str = ""


@dataclass(frozen=True)
class AppleSong:
    track_id: str
    album_id: str
    url: str


@dataclass(frozen=True)
class _TextToken:
    value: str


@dataclass(frozen=True)
class _AppleLinkToken:
    url: str


class _ArticleBodyParser(HTMLParser):
    _IGNORED_CONTAINERS = {"iframe", "noscript", "script", "style", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.groups: list[list[list[_TextToken | _AppleLinkToken]]] = []
        self._group: list[list[_TextToken | _AppleLinkToken]] = []
        self._line: list[_TextToken | _AppleLinkToken] = []
        self._pending_breaks = 0
        self._body_depth = 0
        self._done = False
        self._ignored_tags: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []

    @property
    def _in_body(self) -> bool:
        return self._body_depth > 0

    def _is_article_body(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        if tag != "div":
            return False
        classes = set()
        for name, value in attrs:
            if name == "class" and value:
                classes.update(value.split())
        return {"article-body", "entry-content"} <= classes

    def _finish_line(self) -> None:
        if self._line:
            self._group.append(self._line)
            self._line = []

    def _finish_group(self) -> None:
        self._finish_line()
        if self._group:
            self.groups.append(self._group)
            self._group = []

    def _apply_pending_breaks(self) -> None:
        if self._pending_breaks >= 2:
            self._finish_group()
        elif self._pending_breaks == 1:
            self._finish_line()
        self._pending_breaks = 0

    def _append_text(self, value: str) -> None:
        normalized = re.sub(r"\s+", " ", value)
        if not normalized.strip():
            return
        self._apply_pending_breaks()
        self._line.append(_TextToken(normalized))

    def _append_link(self, url: str) -> None:
        self._apply_pending_breaks()
        self._line.append(_AppleLinkToken(url))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if not self._in_body:
            if not self._done and self._is_article_body(tag, attrs):
                self._body_depth = 1
            return

        if tag == "div":
            self._body_depth += 1
        if self._ignored_tags:
            if tag not in {"br", "img", "input", "source", "wbr"}:
                self._ignored_tags.append(tag)
            return
        if tag in self._IGNORED_CONTAINERS:
            self._ignored_tags.append(tag)
            return
        if tag == "br":
            self._pending_breaks += 1
            return
        if tag == "a":
            self._anchor_href = next((value or "" for name, value in attrs if name == "href"), "")
            self._anchor_text = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if not self._in_body:
            return
        if self._ignored_tags:
            if tag == self._ignored_tags[-1]:
                self._ignored_tags.pop()
            if tag == "div":
                self._body_depth -= 1
            return
        if tag == "a" and self._anchor_href is not None:
            href = self._anchor_href
            anchor_text = "".join(self._anchor_text)
            self._anchor_href = None
            self._anchor_text = []
            if _is_apple_url(href):
                self._append_link(href)
            else:
                self._append_text(anchor_text)
        if tag == "div":
            self._body_depth -= 1
            if self._body_depth == 0:
                self._finish_group()
                self._done = True

    def handle_data(self, data: str) -> None:
        if not self._in_body or self._ignored_tags:
            return
        if self._anchor_href is not None:
            self._anchor_text.append(data)
            return
        self._append_text(data)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _is_apple_url(url: str) -> bool:
    try:
        parts = urlsplit(_text(url))
    except Exception:
        return False
    return parts.scheme in {"http", "https"} and parts.netloc.lower().endswith("apple.com")


def _apple_resource_id(url: str, resource: str) -> str:
    try:
        segments = [segment for segment in urlsplit(_text(url)).path.split("/") if segment]
    except Exception:
        return ""
    for index, segment in enumerate(segments):
        if segment == resource:
            for candidate in segments[index + 1 :]:
                if candidate.isdigit():
                    return candidate
                match = re.fullmatch(r"id(\d+)", candidate)
                if match:
                    return match.group(1)
    return ""


def extract_album_id_from_url(url: str) -> str:
    try:
        parts = urlsplit(_text(url))
    except Exception:
        return ""
    segments = [segment for segment in parts.path.split("/") if segment]
    for idx, segment in enumerate(segments):
        if segment == "album":
            for candidate in segments[idx + 1 :]:
                if candidate.isdigit():
                    return candidate
                match = re.fullmatch(r"id(\d+)", candidate)
                if match:
                    return match.group(1)
    return ""


def extract_track_id_from_url(url: str) -> str:
    track_id = _apple_resource_id(url, "song")
    if track_id:
        return track_id
    try:
        query = parse_qs(urlsplit(_text(url)).query)
    except Exception:
        return ""
    candidate = (query.get("i") or [""])[0]
    return candidate if candidate.isdigit() else ""


def extract_album_id_from_track(track: dict[str, Any]) -> str:
    return extract_album_id_from_url(_text(track.get("source_track_url")))


def extract_track_id_from_track(track: dict[str, Any]) -> str:
    direct = _text(track.get("apple_track_id") or track.get("source_apple_track_id"))
    return direct or extract_track_id_from_url(_text(track.get("source_track_url")))


def _line_text(line: list[_TextToken | _AppleLinkToken]) -> str:
    return "".join(token.value for token in line if isinstance(token, _TextToken)).strip()


def _group_entries(group: list[list[_TextToken | _AppleLinkToken]]) -> list[LivedoorEntry]:
    entries: list[LivedoorEntry] = []
    context: list[str] = []
    shared_prefix: list[str] = []
    seen_first_link = False
    link_count = sum(isinstance(token, _AppleLinkToken) for line in group for token in line)

    for line in group:
        text = _line_text(line)
        links = [token for token in line if isinstance(token, _AppleLinkToken)]
        if not links:
            if text:
                context.append(text)
            continue

        own_lines = [*context, *([text] if text else [])]
        context = []
        if not seen_first_link:
            seen_first_link = True
            if link_count > 1 and len(own_lines) >= 2:
                shared_prefix = own_lines[:-1]
                own_lines = own_lines[-1:]
        song_text = own_lines[-1] if own_lines else ""
        for link in links:
            album_id = extract_album_id_from_url(link.url)
            track_id = extract_track_id_from_url(link.url)
            if not album_id and not track_id:
                continue
            body_lines = [*shared_prefix, *own_lines]
            body_text = "\n".join(value for value in body_lines if value).strip()
            entries.append(
                LivedoorEntry(
                    album_id=album_id,
                    track_id=track_id,
                    title=song_text or album_id or track_id,
                    url=link.url,
                    body_text=body_text,
                )
            )
    return entries


def parse_html_entries(source: str) -> list[LivedoorEntry]:
    parser = _ArticleBodyParser()
    parser.feed(source)
    parser.close()
    return [entry for group in parser.groups for entry in _group_entries(group)]


def _normalize_song_url(url: str, track_id: str) -> str:
    try:
        parts = urlsplit(url)
    except Exception:
        return url
    query = parse_qs(parts.query)
    query["i"] = [track_id]
    query.pop("uo", None)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True), ""))


def _storefront_from_url(url: str) -> str:
    try:
        segments = [segment.lower() for segment in urlsplit(url).path.split("/") if segment]
    except Exception:
        return "jp"
    return segments[0] if segments and re.fullmatch(r"[a-z]{2}", segments[0]) else "jp"


def lookup_songs(track_ids: Iterable[str], country: str, timeout: float = 15.0) -> dict[str, AppleSong]:
    ids = list(dict.fromkeys(track_id for track_id in track_ids if _text(track_id).isdigit()))
    if not ids:
        return {}
    query = urlencode({"id": ",".join(ids), "country": _text(country).lower() or "jp", "entity": "song"})
    request = urllib.request.Request(
        f"{ITUNES_LOOKUP_URL}?{query}",
        headers={"User-Agent": "Mozilla/5.0 ChartooLivedoorReference/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, urllib.error.URLError, TimeoutError, ssl.SSLError):
        return {}
    results = payload.get("results") if isinstance(payload, dict) else []
    songs: dict[str, AppleSong] = {}
    for result in results if isinstance(results, list) else []:
        if not isinstance(result, dict) or _text(result.get("kind")).lower() != "song":
            continue
        track_id = _text(result.get("trackId"))
        album_id = _text(result.get("collectionId"))
        url = _text(result.get("trackViewUrl") or result.get("collectionViewUrl"))
        if track_id.isdigit() and album_id.isdigit() and url:
            songs[track_id] = AppleSong(track_id=track_id, album_id=album_id, url=_normalize_song_url(url, track_id))
    return songs


def _resolve_song_entries(entries: Iterable[LivedoorEntry], timeout: float) -> list[LivedoorEntry]:
    entry_list = list(entries)
    ids_by_storefront: dict[str, list[str]] = {}
    for entry in entry_list:
        if entry.track_id and not entry.album_id:
            ids_by_storefront.setdefault(_storefront_from_url(entry.url), []).append(entry.track_id)
    resolved: dict[tuple[str, str], AppleSong] = {}
    for storefront in sorted(ids_by_storefront):
        for track_id, song in lookup_songs(
            ids_by_storefront[storefront], country=storefront, timeout=timeout
        ).items():
            resolved[(storefront, track_id)] = song
    values: list[LivedoorEntry] = []
    for entry in entry_list:
        song = resolved.get((_storefront_from_url(entry.url), entry.track_id))
        values.append(replace(entry, album_id=song.album_id, url=song.url) if song else entry)
    return values


def match_entries_by_track(
    tracks_by_market: dict[str, list[dict[str, Any]]],
    entries: Iterable[LivedoorEntry],
) -> tuple[dict[str, list[LivedoorEntry]], list[LivedoorEntry]]:
    entry_list = list(entries)
    matches: dict[str, list[LivedoorEntry]] = {}
    for market in ("jp", "hk"):
        for track in tracks_by_market.get(market) or []:
            apple_track_id = extract_track_id_from_track(track)
            album_id = extract_album_id_from_track(track)
            track_key = track_key_for(market, track)
            exact_track_matches = [entry for entry in entry_list if entry.track_id and entry.track_id == apple_track_id]
            matches[track_key] = exact_track_matches or [
                entry for entry in entry_list if not entry.track_id and album_id and entry.album_id == album_id
            ]
    matched_entries = {id(entry) for values in matches.values() for entry in values}
    unmatched = [entry for entry in entry_list if id(entry) not in matched_entries]
    return matches, unmatched


def suppress_unmatched_entries(
    entries: Iterable[LivedoorEntry],
    historical_tracks: Iterable[dict[str, Any]],
) -> tuple[list[LivedoorEntry], list[LivedoorEntry]]:
    tracks = list(historical_tracks)
    track_ids = {track_id for track in tracks if (track_id := extract_track_id_from_track(track))}
    album_ids = {album_id for track in tracks if (album_id := extract_album_id_from_track(track))}
    kept: list[LivedoorEntry] = []
    suppressed: list[LivedoorEntry] = []
    for entry in entries:
        is_suppressed = entry.track_id in track_ids if entry.track_id else bool(entry.album_id in album_ids)
        (suppressed if is_suppressed else kept).append(entry)
    return kept, suppressed


def track_key_for(market: str, track: dict[str, Any]) -> str:
    identity = _text(track.get("apple_track_id")) or _text(track.get("canonical_key")) or _text(track.get("final_rank"))
    return f"{market}:{identity}"


def date_url_for(release_date: str) -> str:
    return DATE_URL_TEMPLATE.format(release_date=_text(release_date))


def jina_date_url_for(release_date: str) -> str:
    return JINA_DATE_URL_TEMPLATE.format(release_date=_text(release_date))


def fetch_html(release_date: str, timeout: float = 15.0) -> str:
    request = urllib.request.Request(
        jina_date_url_for(release_date),
        headers={
            "User-Agent": "Mozilla/5.0 ChartooLivedoorReference/1.0",
            "X-Return-Format": "html",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ssl.SSLError):
        return ""


def fetch_entries(release_date: str, timeout: float = 15.0) -> list[LivedoorEntry]:
    source = fetch_html(release_date, timeout=timeout)
    if not source:
        return []
    return _resolve_song_entries(parse_html_entries(source), timeout=timeout)
