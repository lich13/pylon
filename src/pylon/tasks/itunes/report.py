#!/usr/bin/env python3
from __future__ import annotations

"""Chartoo iTunes daily HTML report sender.

This script keeps the collection/finalization monitor and only sends a
fixed-template HTML document after both JP and HK reports exist for the same
release date.
"""

import argparse
import datetime as dt
import hashlib
import json
import subprocess
from pylon import events
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


from pylon.tasks.itunes import html_report, livedoor_reference, telegram_document
from pylon.shared import output, paths, state_json


PYTHON = paths.PYTHON
PROFILE_ROOT = paths.PROFILE_ROOT
DATA_ROOT = paths.CHARTOO_DATA_ROOT
DEFAULT_NOTIFY_STATE = DATA_ROOT / "html_notifications.json"
ENV_PATH = PROFILE_ROOT / ".env"
RECENT_RELEASE_DAYS = 2
LIVEDOOR_SUPPRESSION_DAYS = 3


@dataclass
class ReportArtifact:
    artifact_key: str
    release_date: str
    content_hash: str
    path: Path | None
    caption: str


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_json(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text or text == output.wake_agent_false() or text == output.silent():
        return {}
    return json.loads(text)


def run_monitor() -> str:
    proc = subprocess.run(
        [str(PYTHON), "-m", "pylon.tasks.itunes.monitor", "run"],
        cwd=str(PROFILE_ROOT),
        text=True,
        capture_output=True,
        timeout=1800,
    )
    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        return output.json_dumps_compact(
            {
                "action": "error",
                "error": f"monitor exited with returncode={proc.returncode}",
                "stderr": stderr[:500],
                "stdout": stdout[:500],
            }
        )
    return stdout or output.wake_agent_false()


def load_notified(path: Path = DEFAULT_NOTIFY_STATE) -> dict[str, Any]:
    data = state_json.load(path, default={})
    return data if isinstance(data, dict) else {}


def save_notified(value: dict[str, Any], path: Path = DEFAULT_NOTIFY_STATE) -> None:
    state_json.save(path, value)


def _report_path_for(market: str, release_date: str) -> Path:
    return DATA_ROOT / "reports" / f"{release_date}-{market}.json"


def _report_date_market(path: Path) -> tuple[str, str] | None:
    stem = path.stem
    parts = stem.rsplit("-", 1)
    if len(parts) != 2:
        return None
    release_date, market = parts
    if market not in ("jp", "hk"):
        return None
    try:
        dt.date.fromisoformat(release_date)
    except ValueError:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        aggregation = data.get("aggregation") if isinstance(data, dict) else {}
        if not isinstance(aggregation, dict):
            return None
        if str(aggregation.get("release_date") or "") != release_date:
            return None
        if str(aggregation.get("market") or "").lower() != market:
            return None
    except Exception:
        return None
    return release_date, market


def recent_complete_report_dates(limit: int = RECENT_RELEASE_DAYS) -> list[str]:
    reports_dir = DATA_ROOT / "reports"
    by_date: dict[str, set[str]] = {}
    if not reports_dir.exists():
        return []
    for path in reports_dir.glob("*.json"):
        parsed = _report_date_market(path)
        if parsed is None:
            continue
        release_date, market = parsed
        by_date.setdefault(release_date, set()).add(market)
    complete = [date for date, markets in by_date.items() if {"jp", "hk"} <= markets]
    return sorted(complete)[-max(0, int(limit)):]


def _report_paths_from_finalizations(finalizations: Iterable[dict[str, Any]], release_date: str) -> list[Path]:
    found: dict[str, Path] = {}
    for fin in finalizations:
        if str(fin.get("release_date") or "") != release_date:
            continue
        market = str(fin.get("market") or "").lower()
        report_json = ((fin.get("reports") or {}).get("report_json") or "")
        if market in ("jp", "hk") and report_json:
            found[market] = Path(report_json)
    for market in ("jp", "hk"):
        found.setdefault(market, _report_path_for(market, release_date))
    return [found["jp"], found["hk"]]


def livedoor_history_report_paths(release_date: str) -> list[Path]:
    try:
        target_date = dt.date.fromisoformat(release_date)
    except ValueError:
        return []
    paths: list[Path] = []
    for days_ago in range(1, LIVEDOOR_SUPPRESSION_DAYS + 1):
        historical_date = (target_date - dt.timedelta(days=days_ago)).isoformat()
        for market in ("jp", "hk"):
            path = _report_path_for(market, historical_date)
            if _report_date_market(path) == (historical_date, market):
                paths.append(path)
    return paths


def fetch_livedoor_entries(release_date: str) -> list[livedoor_reference.LivedoorEntry]:
    return livedoor_reference.fetch_entries(release_date)


def caption_for_report(result: html_report.HtmlReportResult) -> str:
    return f"Chartoo {result.release_date}\n总歌曲数：{result.visible_track_count}"


def candidate_dates_from_payload(payload: dict[str, Any]) -> list[str]:
    dates = sorted(
        {
            str(fin.get("release_date") or "")
            for fin in (payload.get("finalizations") or [])
            if isinstance(fin, dict) and fin.get("release_date")
        }
    )
    return [date for date in dates if date]


def candidate_dates(payload: dict[str, Any] | None = None) -> list[str]:
    values = set(recent_complete_report_dates())
    if payload:
        values.update(candidate_dates_from_payload(payload))
    return sorted(values)


def write_html_for_date(release_date: str, finalizations: Iterable[dict[str, Any]]) -> html_report.HtmlReportResult | None:
    report_paths = _report_paths_from_finalizations(finalizations, release_date)
    if not all(path.exists() for path in report_paths):
        return None
    return html_report.write_daily_report(
        report_paths,
        DATA_ROOT / "html",
        livedoor_entries=fetch_livedoor_entries(release_date),
        livedoor_history_report_paths=livedoor_history_report_paths(release_date),
    )


def write_artifacts_for_date(release_date: str, finalizations: Iterable[dict[str, Any]]) -> list[ReportArtifact] | None:
    report_paths = _report_paths_from_finalizations(finalizations, release_date)
    if not all(path.exists() for path in report_paths):
        return None
    html_result = html_report.write_daily_report(
        report_paths,
        DATA_ROOT / "html",
        livedoor_entries=fetch_livedoor_entries(release_date),
        livedoor_history_report_paths=livedoor_history_report_paths(release_date),
    )
    if html_result.audio_track_count == 0:
        return []
    caption = caption_for_report(html_result)
    content_hash = f"{html_result.content_hash}-{hashlib.sha256(caption.encode('utf-8')).hexdigest()[:16]}"
    return [
        ReportArtifact(
            artifact_key="html",
            release_date=html_result.release_date,
            content_hash=content_hash,
            path=html_result.path,
            caption=caption,
        ),
    ]


def _has_dual_markets(existing: dict[str, Any]) -> bool:
    markets = existing.get("markets")
    if not isinstance(markets, (list, tuple, set)):
        return False
    delivered_markets = {str(market).lower() for market in markets}
    return delivered_markets == {"hk", "jp"}


def _artifact_already_sent(artifact: ReportArtifact, notified: dict[str, Any]) -> bool:
    existing = notified.get(artifact.release_date)
    if not isinstance(existing, dict) or not _has_dual_markets(existing):
        return False
    if existing.get("sent_at_utc"):
        return True
    artifacts = existing.get("artifacts")
    if not isinstance(artifacts, dict):
        return False
    artifact_state = artifacts.get(artifact.artifact_key)
    return isinstance(artifact_state, dict) and bool(artifact_state.get("sent_at_utc"))


def report_already_sent(result: html_report.HtmlReportResult, notified: dict[str, Any]) -> bool:
    existing = notified.get(result.release_date)
    if not isinstance(existing, dict):
        return False
    if not _has_dual_markets(existing):
        return False
    if existing.get("sent_at_utc"):
        return True
    artifacts = existing.get("artifacts")
    if not isinstance(artifacts, dict):
        return False
    artifact_state = artifacts.get("html")
    return isinstance(artifact_state, dict) and bool(artifact_state.get("sent_at_utc"))


def record_sent(result: ReportArtifact, notified: dict[str, Any]) -> None:
    existing = notified.get(result.release_date)
    if not isinstance(existing, dict):
        existing = {}
    artifacts = existing.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    sent_at = now_utc()
    artifacts[result.artifact_key] = {
        "content_hash": result.content_hash,
        "path": str(result.path or ""),
        "sent_at_utc": sent_at,
    }
    existing["markets"] = ["hk", "jp"]
    existing["artifacts"] = artifacts
    html_state = artifacts.get("html") or {}
    existing["content_hash"] = html_state.get("content_hash", "")
    existing["path"] = html_state.get("path", "")
    existing["sent_at_utc"] = sent_at
    notified[result.release_date] = existing


def send_html_report(result: ReportArtifact) -> dict[str, Any]:
    if events.enabled():
        event_id = events.publish_document(result.path, result.caption, key=f"chartoo:{result.release_date}:{result.content_hash}")
        return {"ok": True, "queued": event_id}
    config = telegram_document.load_telegram_config(ENV_PATH)
    if not result.path:
        raise RuntimeError("report artifact path is missing")
    return telegram_document.send_document(config, result.path, caption=result.caption)


def format_send_failure(release_date: str, exc: Exception) -> str:
    events.result("failed", f"Chartoo {release_date}: {type(exc).__name__}")
    return f"Chartoo HTML 日报推送失败 — {release_date}\n\n原因：{exc}"


def run_for_dates(
    dates: Iterable[str],
    *,
    finalizations: Iterable[dict[str, Any]],
    force_send: bool = False,
    dry_run: bool = False,
) -> str:
    notified = load_notified(DEFAULT_NOTIFY_STATE)
    for release_date in dates:
        artifacts = write_artifacts_for_date(release_date, finalizations)
        if not artifacts:
            continue
        if report_already_sent(artifacts[0], notified) and not force_send:
            continue
        if dry_run:
            continue
        for result in artifacts:
            if _artifact_already_sent(result, notified) and not force_send:
                continue
            try:
                send_html_report(result)
            except Exception as exc:
                return format_send_failure(release_date, exc)
            record_sent(result, notified)
            save_notified(notified, DEFAULT_NOTIFY_STATE)
    return output.wake_agent_false()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send fixed-template Chartoo JP/HK HTML daily report")
    parser.add_argument("--date", default="", help="generate/send an existing release date without running the monitor")
    parser.add_argument("--force-send", action="store_true", help="send even when the same content hash was already recorded")
    parser.add_argument("--dry-run", action="store_true", help="generate HTML without calling Telegram or recording sent state")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args([] if argv is None else argv)
    if args.date:
        text = run_for_dates([args.date], finalizations=[], force_send=args.force_send, dry_run=args.dry_run)
        print(text)
        return 0

    stdout = run_monitor()
    if stdout in ("", output.wake_agent_false(), output.silent()):
        print(run_for_dates(candidate_dates(), finalizations=[]))
        return 0
    try:
        payload = parse_json(stdout)
    except Exception:
        events.result("failed", "Chartoo monitor 输出异常")
        print("Chartoo HTML 日报异常\n\n原因：monitor 输出无法解析。")
        return 0
    if payload.get("action") == "error":
        events.result("failed", "Chartoo monitor 输出异常")
        print("Chartoo HTML 日报异常\n\n原因：monitor 执行失败。")
        return 0
    if payload.get("action") != "finalize":
        print(run_for_dates(candidate_dates(payload), finalizations=[]))
        return 0
    dates = candidate_dates(payload)
    text = run_for_dates(dates, finalizations=payload.get("finalizations") or [])
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
