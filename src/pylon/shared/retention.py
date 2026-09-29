"""Prune completed run records while preserving delivery and deduplication data."""
from __future__ import annotations

import time

from pylon.outbox import Outbox


def prune_runs(profile: str, days: int, *, dry_run: bool, job_id: str | None = None) -> int:
    box = Outbox()
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows = db.execute("""
            SELECT r.id FROM runs r
            WHERE r.finished_at < ? AND r.status IN ('ok','no_change')
            AND (? IS NULL OR r.job_id=?)
            AND EXISTS (SELECT 1 FROM runs newer WHERE newer.job_id=r.job_id AND newer.started_at>r.started_at)
            AND NOT EXISTS (SELECT 1 FROM run_events re JOIN events e ON e.id=re.event_id
                            WHERE re.run_id=r.id AND e.status!='delivered')
        """, (time.time() - days * 86400, job_id, job_id)).fetchall()
        from pylon.config import jobs
        allowed = {key for key, job in jobs().items() if job['profile'] == profile}
        candidates = [row['id'] for row in rows if row['id'].split(':', 1)[0] in allowed]
        if not dry_run:
            db.executemany('DELETE FROM run_events WHERE run_id=?', [(key,) for key in candidates])
            db.executemany('DELETE FROM runs WHERE id=?', [(key,) for key in candidates])
        return len(candidates)
