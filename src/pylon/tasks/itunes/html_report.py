from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from pylon.tasks.itunes import livedoor_reference, tunemymusic_text


REQUIRED_MARKETS = {"jp", "hk"}
MARKET_LABELS = {"jp": "JP", "hk": "HK"}
APPLE_ARTWORK_SIZE_RE = re.compile(r"/\d+x\d+bb\.(?:jpg|jpeg|png|webp)$", re.IGNORECASE)


@dataclass
class HtmlReportResult:
    release_date: str
    html: str
    content_hash: str
    tracks_by_market: dict[str, list[dict[str, Any]]]
    folded_duplicates: list[dict[str, Any]]
    tunemymusic_text: str
    visible_track_count: int
    audio_track_count: int
    livedoor_matches: dict[str, list[livedoor_reference.LivedoorEntry]]
    livedoor_unmatched: list[livedoor_reference.LivedoorEntry]
    livedoor_suppressed: list[livedoor_reference.LivedoorEntry]
    path: Path | None = None


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _esc(value: Any, default: str = "") -> str:
    return html.escape(_text(value, default), quote=True)


def _compact(value: Any) -> str:
    text = _text(value).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def _load_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"report is not an object: {path}")
    aggregation = data.get("aggregation")
    if not isinstance(aggregation, dict):
        raise ValueError(f"report missing aggregation: {path}")
    return data


def _aggregation(data: dict[str, Any]) -> dict[str, Any]:
    aggregation = data.get("aggregation")
    return aggregation if isinstance(aggregation, dict) else {}


def _report_tracks(data: dict[str, Any]) -> list[dict[str, Any]]:
    tracks = _aggregation(data).get("tracks")
    return [track for track in tracks if isinstance(track, dict)] if isinstance(tracks, list) else []


def _livedoor_history_tracks(
    current_reports: Iterable[dict[str, Any]],
    history_report_paths: Iterable[Path | str],
) -> list[dict[str, Any]]:
    tracks = [track for report in current_reports for track in _report_tracks(report)]
    for raw_path in history_report_paths:
        try:
            report = _load_report(Path(raw_path))
        except (OSError, UnicodeError, ValueError):
            continue
        tracks.extend(_report_tracks(report))
    return tracks


def _duplicate_keys(track: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    canonical = _text(track.get("canonical_key")).strip()
    if canonical:
        keys.append(f"canonical:{canonical.casefold()}")
    apple_id = _text(track.get("apple_track_id") or track.get("source_apple_track_id")).strip()
    if apple_id:
        keys.append(f"apple:{apple_id.casefold()}")
    title_artist = _compact(track.get("title") or track.get("source_title")) + ":" + _compact(
        track.get("artist") or track.get("source_artist")
    )
    if title_artist != ":":
        keys.append(f"title_artist:{title_artist}")
    return keys


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _performance_key(track: dict[str, Any]) -> tuple[int, int, int, int, int]:
    market_order = {"jp": 1, "hk": 0}
    return (
        _int_value(track.get("total_points")),
        -_int_value(track.get("best_rank"), 9999),
        _int_value(track.get("occurrence_count")),
        -_int_value(track.get("final_rank"), 9999),
        market_order.get(_text(track.get("market")).lower(), 0),
    )


def _dedupe_tracks(reports: dict[str, dict[str, Any]]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    for market, data in reports.items():
        for track in _aggregation(data).get("tracks") or []:
            if not isinstance(track, dict):
                continue
            item = dict(track)
            item["market"] = market
            item["_dedupe_keys"] = _duplicate_keys(item)
            item["_dedupe_key"] = item["_dedupe_keys"][0] if item["_dedupe_keys"] else ""
            entries.append(item)

    grouped: list[list[dict[str, Any]]] = []
    grouped_keys: list[set[str]] = []
    for entry in entries:
        keys = set(entry.get("_dedupe_keys") or [])
        if not keys:
            grouped.append([entry])
            grouped_keys.append({f"unique:{id(entry)}"})
            continue
        matches = [i for i, existing in enumerate(grouped_keys) if keys & existing]
        if not matches:
            grouped.append([entry])
            grouped_keys.append(set(keys))
            continue
        base = matches[0]
        grouped[base].append(entry)
        grouped_keys[base].update(keys)
        for idx in reversed(matches[1:]):
            grouped[base].extend(grouped[idx])
            grouped_keys[base].update(grouped_keys[idx])
            del grouped[idx]
            del grouped_keys[idx]

    kept: dict[int, bool] = {}
    folded: list[dict[str, Any]] = []
    for group in grouped:
        markets = {entry["market"] for entry in group}
        if len(group) < 2 or len(markets) < 2:
            for entry in group:
                kept[id(entry)] = True
            continue
        winner = max(group, key=_performance_key)
        kept[id(winner)] = True
        for entry in group:
            if entry is winner:
                continue
            folded.append(
                {
                    "title": entry.get("title") or entry.get("source_title") or winner.get("title"),
                    "artist": entry.get("artist") or entry.get("source_artist") or winner.get("artist"),
                    "dedupe_key": entry.get("_dedupe_key") or "",
                    "keep_market": winner["market"],
                    "folded_market": entry["market"],
                    "keep_rank": winner.get("final_rank"),
                    "folded_rank": entry.get("final_rank"),
                }
            )

    by_market = {"jp": [], "hk": []}
    for entry in entries:
        if kept.get(id(entry)):
            clean = {k: v for k, v in entry.items() if not k.startswith("_")}
            by_market[entry["market"]].append(clean)
    for market in by_market:
        by_market[market].sort(key=lambda t: _int_value(t.get("final_rank"), 9999))
    folded.sort(key=lambda item: (item["folded_market"], _text(item.get("title"))))
    return by_market, folded


def _summary_stat(reports: dict[str, dict[str, Any]], market: str, key: str) -> str:
    return _esc(_aggregation(reports[market]).get(key), "0")


def _safe_remote_image_url(value: Any) -> str:
    url = _text(value).strip()
    if not url:
        return ""
    if any(ch in url for ch in ('"', "'", "<", ">", "\\")) or any(ch.isspace() for ch in url):
        return ""
    try:
        parts = urlsplit(url)
    except Exception:
        return ""
    if parts.scheme != "https" or not parts.netloc:
        return ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _is_apple_artwork_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except Exception:
        return False
    host = parts.netloc.lower()
    return (host == "mzstatic.com" or host.endswith(".mzstatic.com")) and bool(APPLE_ARTWORK_SIZE_RE.search(parts.path))


def _apple_artwork_variant(url: Any, size: int) -> str:
    safe = _safe_remote_image_url(url)
    if not safe or not _is_apple_artwork_url(safe):
        return safe
    parts = urlsplit(safe)
    path = APPLE_ARTWORK_SIZE_RE.sub(f"/{size}x{size}bb.jpg", parts.path)
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def _apple_artwork_srcset(url: Any) -> str:
    safe = _safe_remote_image_url(url)
    if not safe or not _is_apple_artwork_url(safe):
        return ""
    return ", ".join(f"{_apple_artwork_variant(safe, size)} {size}w" for size in (300, 600, 1000))


def _remote_image_tag(url: Any, class_name: str, *, sizes: str = "", loading: str = "lazy") -> str:
    safe = _safe_remote_image_url(url)
    if not safe:
        return ""
    attrs = [
        f'class="{_esc(class_name)}"',
        f'src="{_esc(_apple_artwork_variant(safe, 300))}"',
        'alt=""',
        'referrerpolicy="no-referrer"',
        f'loading="{_esc(loading)}"',
        'decoding="async"',
        'data-chartoo-cover="1"',
        f'data-chartoo-fallback-class="{_esc(class_name + " cover-empty")}"',
        'onerror="window.chartooCoverFallback&&window.chartooCoverFallback(this)"',
    ]
    srcset = _apple_artwork_srcset(safe)
    if srcset and sizes:
        attrs.insert(2, f'srcset="{_esc(srcset)}"')
        attrs.insert(3, f'sizes="{_esc(sizes)}"')
    return f"<img {' '.join(attrs)}>"


def _cover_style(track: dict[str, Any]) -> str:
    image = _remote_image_tag(track.get("artwork_url"), "cover", sizes="(max-width: 760px) 58px, 72px")
    if not image:
        return '<div class="cover cover-empty"><span>♪</span></div>'
    return image


def _livedoor_notes(entries: list[livedoor_reference.LivedoorEntry]) -> str:
    if not entries:
        return ""
    items = []
    for entry in entries:
        items.append(
            f"""
            <div class="livedoor-note">
              <div class="livedoor-label">Livedoor</div>
              <a href="{_esc(entry.url)}" target="_blank" rel="noopener noreferrer">{_esc(entry.title)}</a>
              <pre>{_esc(entry.body_text)}</pre>
            </div>
            """
        )
    return "".join(items)


def _track_card(
    track: dict[str, Any],
    market: str,
    audio_index: int | None = None,
    livedoor_entries: list[livedoor_reference.LivedoorEntry] | None = None,
) -> str:
    source = _text(track.get("source_track_url")).strip()
    preview = _text(track.get("preview_url")).strip()
    audio_attrs = ""
    if audio_index is not None:
        artwork = _apple_artwork_variant(track.get("artwork_url"), 300)
        audio_attrs = (
            f' data-chartoo-audio-index="{audio_index}"'
            f' data-chartoo-title="{_esc(track.get("title"), "Untitled")}"'
            f' data-chartoo-artist="{_esc(track.get("artist"), "Unknown artist")}"'
            f' data-chartoo-album="{_esc(track.get("album"), "")}"'
            f' data-chartoo-artwork="{_esc(artwork)}"'
        )
    source_link = (
        f'<a class="pill-link" href="{_esc(source)}" target="_blank" rel="noopener noreferrer">Apple Music</a>'
        if source
        else '<span class="pill-link disabled">No source</span>'
    )
    audio = (
        f'<audio controls preload="none"{audio_attrs} src="{_esc(preview)}"></audio>'
        if preview
        else '<span class="no-preview">No preview</span>'
    )
    return f"""
        <article class="track-card">
          <div class="rank">#{_esc(track.get("final_rank"), "?")}</div>
          {_cover_style(track)}
          <div class="track-main">
            <div class="track-title">{_esc(track.get("title"), "Untitled")}</div>
            <div class="track-artist">{_esc(track.get("artist"), "Unknown artist")}</div>
            <div class="track-album">{_esc(track.get("album"), "Unknown album")}</div>
            <div class="metrics">
              <span>{_esc(track.get("duration_label"), "--:--")}</span>
              <span>最高 #{_esc(track.get("best_rank"), "?")}</span>
              <span>出现 {_esc(track.get("occurrence_count"), "0")} 次</span>
              <span>{_esc(track.get("total_points"), "0")} pts</span>
            </div>
            {_livedoor_notes(livedoor_entries or [])}
          </div>
          <div class="track-actions">
            <span class="market-chip {market}">{MARKET_LABELS[market]}</span>
            {source_link}
            {audio}
          </div>
        </article>
    """


def _market_section(
    reports: dict[str, dict[str, Any]],
    tracks: list[dict[str, Any]],
    market: str,
    start_audio_index: int,
    livedoor_matches: dict[str, list[livedoor_reference.LivedoorEntry]],
) -> tuple[str, int]:
    cards = []
    audio_index = start_audio_index
    for track in tracks:
        current_index = audio_index if _text(track.get("preview_url")).strip() else None
        key = livedoor_reference.track_key_for(market, track)
        cards.append(_track_card(track, market, current_index, livedoor_matches.get(key) or []))
        if current_index is not None:
            audio_index += 1
    cards_html = "\n".join(cards)
    if not cards_html:
        cards_html = '<div class="empty-market">跨市场重复折叠后，本市场没有单独展示曲目。</div>'
    return f"""
      <section class="market-section" id="{market}">
        <div class="section-heading">
          <div>
            <p class="eyebrow">{MARKET_LABELS[market]} Market</p>
            <h2>{MARKET_LABELS[market]} iTunes New Songs</h2>
          </div>
          <div class="section-stats">
            <span>原始唯一 {_summary_stat(reports, market, "unique_tracks_before_filter")}</span>
            <span>过滤后 {_summary_stat(reports, market, "tracks_after_filter")}</span>
            <span>丢弃 {_summary_stat(reports, market, "dropped_count")}</span>
          </div>
        </div>
        <div class="track-list">{cards_html}</div>
      </section>
    """, audio_index


def _audio_queue_script() -> str:
    return """
  <script>
    (() => {
      const queue = Array.from(document.querySelectorAll('audio[data-chartoo-audio-index]'))
        .sort((a, b) => Number(a.dataset.chartooAudioIndex) - Number(b.dataset.chartooAudioIndex));
      window.chartooAudioQueue = queue;
      let activeIndex = -1;
      const mediaSession = navigator.mediaSession || null;
      const pauseOthers = (current) => {
        queue.forEach((audio) => {
          if (audio !== current && !audio.paused) audio.pause();
        });
      };
      const stopQueue = () => {
        queue.forEach((audio) => {
          if (!audio.paused) audio.pause();
        });
        activeIndex = -1;
      };
      const currentAudio = () => {
        if (activeIndex >= 0 && queue[activeIndex]) return queue[activeIndex];
        return queue.find((audio) => !audio.paused) || queue[0] || null;
      };
      const playAt = (index) => {
        const audio = queue[index];
        if (!audio) return;
        activeIndex = index;
        pauseOthers(audio);
        audio.play().catch(() => {
          activeIndex = -1;
        });
      };
      const updateMediaMetadata = (audio) => {
        if (!mediaSession || typeof MediaMetadata === "undefined" || !audio) return;
        const artwork = audio.dataset.chartooArtwork;
        const metadata = {
          title: audio.dataset.chartooTitle || document.title,
          artist: audio.dataset.chartooArtist || "",
          album: audio.dataset.chartooAlbum || "",
        };
        if (artwork) {
          metadata.artwork = [{ src: artwork, sizes: "300x300", type: "image/jpeg" }];
        }
        try {
          mediaSession.metadata = new MediaMetadata(metadata);
        } catch (_) {}
      };
      const setActionHandler = (name, handler) => {
        if (!mediaSession || !mediaSession.setActionHandler) return;
        try {
          mediaSession.setActionHandler(name, handler);
        } catch (_) {}
      };
      setActionHandler("play", () => {
        const audio = currentAudio();
        if (!audio) return;
        playAt(queue.indexOf(audio));
      });
      setActionHandler("pause", () => {
        const audio = currentAudio();
        if (audio) audio.pause();
      });
      setActionHandler("nexttrack", () => {
        if (activeIndex >= queue.length - 1) {
          stopQueue();
          return;
        }
        const nextIndex = activeIndex < 0 ? 0 : activeIndex + 1;
        playAt(nextIndex);
      });
      setActionHandler("previoustrack", () => {
        const previousIndex = Math.max((activeIndex < 0 ? 0 : activeIndex - 1), 0);
        playAt(previousIndex);
      });
      queue.forEach((audio, index) => {
        audio.addEventListener('play', () => {
          activeIndex = index;
          pauseOthers(audio);
          updateMediaMetadata(audio);
        });
        audio.addEventListener('ended', () => {
          if (activeIndex !== index) return;
          const next = queue[index + 1];
          if (!next) {
            stopQueue();
            return;
          }
          activeIndex = index + 1;
          pauseOthers(next);
          next.play().catch(() => {
            activeIndex = -1;
          });
        });
      });
    })();
  </script>
    """


def _cover_fallback_script() -> str:
    return """
  <script>
    window.chartooCoverFallback = (img) => {
      if (!img || img.dataset.chartooCoverFailed) return;
      img.dataset.chartooCoverFailed = '1';
      const fallback = document.createElement('div');
      fallback.className = img.dataset.chartooFallbackClass || 'cover cover-empty';
      fallback.innerHTML = '<span>♪</span>';
      img.replaceWith(fallback);
    };
  </script>
    """


def _livedoor_unmatched_section(entries: list[livedoor_reference.LivedoorEntry]) -> str:
    if not entries:
        return ""
    rows = []
    for entry in entries:
        rows.append(
            f"""
          <article class="livedoor-entry">
            <a href="{_esc(entry.url)}" target="_blank" rel="noopener noreferrer">{_esc(entry.title)}</a>
            <pre>{_esc(entry.body_text)}</pre>
          </article>
            """
        )
    return f"""
      <section class="livedoor-unmatched-section">
        <div class="section-heading">
          <div>
            <p class="eyebrow">Livedoor</p>
            <h2>未匹配原文</h2>
          </div>
          <span class="clean-badge">{len(entries)} entries</span>
        </div>
        <div class="livedoor-entry-list">{''.join(rows)}</div>
      </section>
    """


def _html_document(
    reports: dict[str, dict[str, Any]],
    tracks_by_market: dict[str, list[dict[str, Any]]],
    folded: list[dict[str, Any]],
    release_date: str,
    livedoor_matches: dict[str, list[livedoor_reference.LivedoorEntry]],
    livedoor_unmatched: list[livedoor_reference.LivedoorEntry],
) -> str:
    total_visible = sum(len(items) for items in tracks_by_market.values())
    jp_art = (tracks_by_market.get("jp") or [{}])[0].get("artwork_url", "")
    hk_art = (tracks_by_market.get("hk") or [{}])[0].get("artwork_url", "")
    hero_art = _apple_artwork_variant(jp_art or hk_art, 300)
    generated_label = _esc(release_date)
    jp_section, next_audio_index = _market_section(reports, tracks_by_market["jp"], "jp", 0, livedoor_matches)
    hk_section, _ = _market_section(reports, tracks_by_market["hk"], "hk", next_audio_index, livedoor_matches)
    livedoor_unmatched_section = _livedoor_unmatched_section(livedoor_unmatched)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chartoo iTunes Daily — {generated_label}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #121212;
      --panel: #181818;
      --panel-2: #1f1f1f;
      --card: #252525;
      --text: #ffffff;
      --muted: #b3b3b3;
      --line: rgba(255,255,255,.08);
      --green: #1ed760;
      --blue: #539df5;
      --shadow: rgba(0,0,0,.42) 0 12px 32px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background:
        radial-gradient(circle at 20% 0%, rgba(30,215,96,.14), transparent 28rem),
        radial-gradient(circle at 80% 10%, rgba(83,157,245,.10), transparent 30rem),
        var(--bg);
      color: var(--text);
      font-family: "DM Sans", system-ui, -apple-system, "Segoe UI", "Hiragino Sans", "Meiryo", sans-serif;
      letter-spacing: 0;
    }}
    a {{ color: inherit; }}
    .shell {{ width: min(1180px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 40px; }}
    .hero {{
      min-height: 260px;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: end;
      padding: 28px;
      border-radius: 8px;
      background: linear-gradient(145deg, rgba(37,37,37,.92), rgba(18,18,18,.96));
      box-shadow: var(--shadow);
      overflow: hidden;
      position: relative;
    }}
    .hero:before {{
      content: "";
      position: absolute;
      inset: 0;
      background-image: url("{_esc(hero_art)}");
      background-size: cover;
      background-position: center;
      opacity: .12;
      filter: blur(18px) saturate(1.2);
      transform: scale(1.08);
    }}
    .hero > * {{ position: relative; }}
    .eyebrow {{
      margin: 0 0 8px;
      color: var(--green);
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 1.6px;
    }}
    h1 {{ margin: 0; font-size: clamp(34px, 6vw, 72px); line-height: .95; letter-spacing: 0; }}
    h2 {{ margin: 0; font-size: 24px; line-height: 1.15; letter-spacing: 0; }}
    .hero-meta {{ display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; color: var(--muted); }}
    .hero-meta span, .clean-badge {{
      display: inline-flex;
      align-items: center;
      min-height: 30px;
      padding: 6px 12px;
      border-radius: 9999px;
      background: var(--panel-2);
      color: var(--muted);
      font-size: 13px;
      font-weight: 700;
    }}
    .cover-pair {{ display: flex; gap: 12px; align-items: end; }}
    .cover-pair-cover {{ width: 132px; aspect-ratio: 1; object-fit: cover; border-radius: 8px; box-shadow: var(--shadow); background: var(--panel-2); }}
    .market-section, .livedoor-unmatched-section {{
      margin-top: 22px;
      padding: 22px;
      border-radius: 8px;
      background: rgba(24,24,24,.88);
      box-shadow: rgba(0,0,0,.30) 0 8px 18px;
    }}
    .section-heading {{ display: flex; justify-content: space-between; align-items: start; gap: 16px; margin-bottom: 16px; }}
    .section-stats {{ display: flex; flex-wrap: wrap; gap: 8px; justify-content: flex-end; color: var(--muted); }}
    .section-stats span {{ padding: 6px 10px; border-radius: 9999px; background: var(--panel-2); font-size: 12px; font-weight: 700; }}
    .track-list {{ display: grid; gap: 10px; }}
    .track-card {{
      display: grid;
      grid-template-columns: 52px 72px minmax(0, 1fr) minmax(210px, 280px);
      gap: 14px;
      align-items: center;
      padding: 12px;
      border-radius: 8px;
      background: var(--card);
      border: 1px solid var(--line);
    }}
    .rank {{ color: var(--muted); font-size: 20px; font-weight: 900; text-align: center; }}
    .cover {{ width: 72px; aspect-ratio: 1; border-radius: 6px; object-fit: cover; background: var(--panel-2); }}
    .cover-empty {{ display: grid; place-items: center; color: var(--green); font-size: 28px; }}
    .track-main {{ min-width: 0; }}
    .track-title {{ font-size: 17px; font-weight: 900; line-height: 1.22; overflow-wrap: anywhere; }}
    .track-artist, .track-album {{ color: var(--muted); font-size: 13px; line-height: 1.35; overflow-wrap: anywhere; }}
    .metrics {{ display: flex; flex-wrap: wrap; gap: 7px; margin-top: 8px; color: var(--muted); }}
    .metrics span {{ padding: 4px 8px; border-radius: 9999px; background: var(--panel-2); font-size: 12px; font-weight: 700; }}
    .track-actions {{ display: grid; justify-items: end; gap: 8px; }}
    .market-chip, .pill-link {{
      min-height: 28px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      padding: 5px 11px;
      border-radius: 9999px;
      background: var(--panel-2);
      color: var(--text);
      font-size: 12px;
      font-weight: 900;
      text-decoration: none;
    }}
    .market-chip.jp {{ color: var(--green); }}
    .market-chip.hk {{ color: var(--blue); }}
    .pill-link {{ box-shadow: rgb(18,18,18) 0 1px 0, rgb(124,124,124) 0 0 0 1px inset; }}
    .pill-link.disabled, .no-preview {{ color: var(--muted); }}
    audio {{ width: 100%; max-width: 260px; height: 34px; }}
    .livedoor-note {{
      margin-top: 10px;
      padding: 10px 12px;
      border: 1px solid rgba(30,215,96,.20);
      border-radius: 8px;
      background: rgba(30,215,96,.08);
    }}
    .livedoor-label {{ color: var(--green); font-size: 11px; font-weight: 900; text-transform: uppercase; }}
    .livedoor-note a, .livedoor-entry a {{ color: var(--text); font-size: 13px; font-weight: 900; text-decoration: none; }}
    .livedoor-note pre, .livedoor-entry pre {{
      margin: 6px 0 0;
      color: var(--muted);
      font: 600 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .livedoor-entry-list {{ display: grid; gap: 10px; }}
    .livedoor-entry {{ padding: 12px; border-radius: 8px; background: var(--panel-2); }}
    .empty-market {{ color: var(--muted); padding: 18px; border-radius: 8px; background: var(--panel-2); }}
    @media (max-width: 760px) {{
      .shell {{ width: min(100vw - 20px, 640px); padding-top: 10px; }}
      .hero {{ grid-template-columns: 1fr; padding: 20px; min-height: 220px; }}
      .cover-pair-cover {{ width: 88px; }}
      .section-heading {{ display: grid; }}
      .section-stats {{ justify-content: start; }}
      .track-card {{ grid-template-columns: 42px 58px minmax(0,1fr); }}
      .cover {{ width: 58px; }}
      .track-actions {{ grid-column: 1 / -1; justify-items: stretch; }}
    }}
  </style>
  {_cover_fallback_script()}
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div>
        <p class="eyebrow">Chartoo iTunes Daily</p>
        <h1>{generated_label}</h1>
        <div class="hero-meta">
          <span>JP + HK 双市场总文件</span>
          <span>{total_visible} 首展示</span>
          <span>{len(folded)} 首跨市场折叠</span>
        </div>
      </div>
      <div class="cover-pair">
        {_remote_image_tag(jp_art, "cover-pair-cover", sizes="88px") if jp_art else ''}
        {_remote_image_tag(hk_art, "cover-pair-cover", sizes="88px") if hk_art else ''}
      </div>
    </section>
    {jp_section}
    {hk_section}
    {livedoor_unmatched_section}
  </main>
  {_audio_queue_script()}
</body>
</html>
"""


def render_daily_report(
    report_paths: Iterable[Path | str],
    livedoor_entries: Iterable[livedoor_reference.LivedoorEntry] | None = None,
    livedoor_history_report_paths: Iterable[Path | str] | None = None,
) -> HtmlReportResult:
    reports: dict[str, dict[str, Any]] = {}
    dates: set[str] = set()
    for raw_path in report_paths:
        path = Path(raw_path)
        data = _load_report(path)
        aggregation = _aggregation(data)
        market = _text(aggregation.get("market")).lower()
        release_date = _text(aggregation.get("release_date"))
        if market:
            reports[market] = data
        if release_date:
            dates.add(release_date)

    if set(reports) != REQUIRED_MARKETS:
        raise ValueError("daily HTML report requires both JP and HK reports")
    if len(dates) != 1:
        raise ValueError(f"daily HTML report requires one release date, got {sorted(dates)}")
    release_date = next(iter(dates))
    tracks_by_market, folded = _dedupe_tracks(reports)
    tune_result = tunemymusic_text.render_from_tracks(release_date, tracks_by_market)
    livedoor_matches, livedoor_unmatched = livedoor_reference.match_entries_by_track(
        tracks_by_market,
        list(livedoor_entries or []),
    )
    livedoor_unmatched, livedoor_suppressed = livedoor_reference.suppress_unmatched_entries(
        livedoor_unmatched,
        _livedoor_history_tracks(reports.values(), livedoor_history_report_paths or []),
    )
    document = _html_document(reports, tracks_by_market, folded, release_date, livedoor_matches, livedoor_unmatched)
    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]
    visible_track_count = sum(len(tracks) for tracks in tracks_by_market.values())
    audio_track_count = sum(
        1
        for tracks in tracks_by_market.values()
        for track in tracks
        if _text(track.get("preview_url")).strip()
    )
    return HtmlReportResult(
        release_date=release_date,
        html=document,
        content_hash=digest,
        tracks_by_market=tracks_by_market,
        folded_duplicates=folded,
        tunemymusic_text=tune_result.text,
        visible_track_count=visible_track_count,
        audio_track_count=audio_track_count,
        livedoor_matches=livedoor_matches,
        livedoor_unmatched=livedoor_unmatched,
        livedoor_suppressed=livedoor_suppressed,
    )


def write_daily_report(
    report_paths: Iterable[Path | str],
    output_dir: Path | str,
    livedoor_entries: Iterable[livedoor_reference.LivedoorEntry] | None = None,
    livedoor_history_report_paths: Iterable[Path | str] | None = None,
) -> HtmlReportResult:
    result = render_daily_report(
        report_paths,
        livedoor_entries=livedoor_entries,
        livedoor_history_report_paths=livedoor_history_report_paths,
    )
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.release_date}-chartoo.html"
    path.write_text(result.html, encoding="utf-8")
    result.path = path
    return result
