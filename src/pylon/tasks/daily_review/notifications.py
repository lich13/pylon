"""A durable digest button issues a fresh owner login only when clicked."""
from __future__ import annotations

import hmac
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from pylon.outbox import Outbox
from pylon.inline import owns_callback
from pylon.software_updates import _signature
from . import store


def button(day):
    return 'rv1|'+day+'|'+_signature('webops', 'review-login', day)


def callback(profile, payload, *, box=None):
    parts = str(payload.get('data', '')).split('|')
    if (profile != 'webops' or len(parts) != 3 or parts[0] != 'rv1' or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', parts[1])
            or not hmac.compare_digest(parts[2], _signature(profile, 'review-login', parts[1]))
            or not owns_callback(profile, payload, box=box)):
        raise ValueError('按钮不属于本人会话或原始消息')
    from pylon.tasks.marketplace.web_auth import login_card
    from pylon.tasks.marketplace.store import Store
    return login_card(profile, store=Store(box), workspace='review', day=parts[1])


def digest(box=None, *, now=None):
    from pylon.tasks.marketplace.web_auth import public_url
    box = box or Outbox(); now = time.time() if now is None else now
    local = datetime.fromtimestamp(now, store.ZONE)
    last_day = (local.date()-timedelta(days=1 if local.hour >= 9 else 2)).isoformat()
    parsed = urlsplit(public_url()); base = parsed.scheme+'://'+parsed.netloc+'/pylon/review/'
    sent = []
    with box.connect() as db:
        store.initialize(db); db.execute('BEGIN IMMEDIATE'); store.sync_deliveries(db)
        days = db.execute('SELECT day,count(*) count FROM review_entries WHERE day>=? AND day<=? GROUP BY day ORDER BY day',
                          (store.START, last_day)).fetchall()
        for row in days:
            day = row['day']
            if db.execute('SELECT 1 FROM review_digests WHERE day=?', (day,)).fetchone(): continue
            counts = dict(db.execute('SELECT status,count(*) FROM review_entries WHERE day=? GROUP BY status', (day,)))
            text = f'{day} 每日审核\n共 {row["count"]} 条；已推送 {counts.get("delivered",0)}，已过滤 {counts.get("excluded",0)}，待核对 {counts.get("needs_info",0)}，处理失败 {counts.get("failed",0)}。'
            payload = {'text': text, 'reply_markup': {'inline_keyboard': [
                [{'text': '打开当日审核', 'url': base+'?day='+day}],
                [{'text': '登录后打开', 'callback_data': button(day)}]]}}
            eid = box.enqueue(profile='webops', job_id='daily-review', kind='text', payload=payload,
                              key='daily-review:'+day, connection=db)
            db.execute('INSERT INTO review_digests VALUES(?,?,?)', (day, eid, now)); sent.append(day)
        store.prune(db, now=now)
    return {'queued_days': sent}
