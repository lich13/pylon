"""Retire the old maintenance UI without erasing execution evidence."""
from __future__ import annotations

import json
import time

RETIRED_MESSAGE = '软件维护现仅保留 /update_codex 和 /update_grok。'
RETIRED_JOBS = {'software-maintenance', 'software-update-review'}


def retire(box):
    """Hide old selections and suppress only wholly unsent retired messages."""
    suppressed = 0
    with box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE software_reviews SET available=0,selected=0 WHERE available!=0 OR selected!=0')
        rows = db.execute("SELECT id,job_id,payload,receipt FROM events WHERE status='pending'").fetchall()
        for row in rows:
            if row['receipt'] and json.loads(row['receipt']):
                continue
            payload = json.loads(row['payload'])
            buttons = (payload.get('reply_markup') or {}).get('inline_keyboard', [])
            old_card = any(str(button.get('callback_data', '')).startswith(('su1|', 'su2|'))
                           for line in buttons for button in line)
            old_inventory = str(payload.get('text', '')).startswith(('腾讯云软件检查（', '每周维护汇总\n'))
            if row['job_id'] in RETIRED_JOBS or (row['job_id'] == 'bot' and (old_card or old_inventory)):
                db.execute("UPDATE events SET status='suppressed',error=?,updated_at=? WHERE id=? AND status='pending'",
                           ('旧软件维护入口已下线', time.time(), row['id']))
                suppressed += 1
    return suppressed
