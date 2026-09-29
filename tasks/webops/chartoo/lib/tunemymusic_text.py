from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CAPTION_LIMIT = 1024
CAPTION_PREFIX = "TuneMyMusic:"
TRUNCATION_NOTICE = "完整列表见 HTML 内 TuneMyMusic 区块"


@dataclass
class TuneMyMusicTextResult:
    release_date: str
    text: str
    content_hash: str
    line_count: int


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def lines_from_tracks(tracks_by_market: dict[str, list[dict[str, Any]]]) -> list[str]:
    lines: list[str] = []
    for market in ("jp", "hk"):
        for track in tracks_by_market.get(market) or []:
            artist = _text(track.get("artist") or track.get("source_artist"))
            title = _text(track.get("title") or track.get("source_title"))
            if artist and title:
                lines.append(f"{artist} – {title}")
            elif title:
                lines.append(title)
    return lines


def render_from_tracks(release_date: str, tracks_by_market: dict[str, list[dict[str, Any]]]) -> TuneMyMusicTextResult:
    lines = lines_from_tracks(tracks_by_market)
    text = "\n".join(lines)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return TuneMyMusicTextResult(
        release_date=release_date,
        text=text,
        content_hash=digest,
        line_count=len(lines),
    )


def render_text(report_paths: Iterable[Path | str]) -> TuneMyMusicTextResult:
    from chartoo.lib import html_report

    daily = html_report.render_daily_report(report_paths)
    return render_from_tracks(daily.release_date, daily.tracks_by_market)


def caption_for_text(text: str, *, limit: int = DEFAULT_CAPTION_LIMIT) -> str:
    body = _text(text)
    if not body:
        return CAPTION_PREFIX
    full = f"{CAPTION_PREFIX}\n{body}"
    if len(full) <= limit:
        return full

    suffix = f"\n{TRUNCATION_NOTICE}"
    available = limit - len(CAPTION_PREFIX) - len(suffix) - 1
    kept: list[str] = []
    used = 0
    for line in body.splitlines():
        line_len = len(line)
        next_used = line_len if not kept else used + 1 + line_len
        if next_used > available:
            break
        kept.append(line)
        used = next_used

    if kept:
        kept_text = "\n".join(kept)
        return f"{CAPTION_PREFIX}\n{kept_text}{suffix}"
    fallback = f"{CAPTION_PREFIX}{suffix}"
    return fallback[:limit]
