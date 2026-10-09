"""Resolve only explicit or source-time-anchored offer deadlines."""
from __future__ import annotations

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from .common import timestamp


def expiration(offer: dict, snapshot: dict, *, now: float, field_name: str = 'expires_at') -> tuple[float | None, bool]:
    field = offer[field_name]
    value = field['value']
    if not value:
        return None, False
    direct = timestamp(value)
    if direct is not None:
        return direct, False
    row = next((e for e in snapshot['evidence'] if e['ref'] == field['ref']), {})
    # An edit does not renew "remaining N days" or "today only" automatically.
    anchor = timestamp(row.get('source_published_at', ''))
    zone = ZoneInfo('Asia/Shanghai')
    date = datetime.fromtimestamp(anchor, zone) if anchor is not None else None
    full = re.search(r'(?:(20\d{2})[年/-])?(\d{1,2})[月/.-](\d{1,2})(?:日|号)?', value)
    clock = re.search(r'(\d{1,2})\s*[:：]\s*(\d{2})(?::(\d{2}))?', value)
    def end_of(day):
        if clock:
            return day.replace(hour=int(clock[1]), minute=int(clock[2]), second=int(clock[3] or 0), microsecond=0).timestamp()
        return (day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
    try:
        if full:
            if not full[1] and date is None:
                return None, True
            year = int(full[1]) if full[1] else date.year
            return end_of(datetime(year, int(full[2]), int(full[3]), tzinfo=zone)), False
        if date is None:
            return None, True
        if re.search(r'今日|今天|当天|明天|明日', value):
            return end_of(date + timedelta(days=1 if re.search(r'明天|明日', value) else 0)), False
        relative = re.search(r'(\d+)\s*(天|小时)\s*(?:后到期|内有效|后截止)', value)
        if not relative and field_name == 'usable_until':
            relative = re.search(r'(?:剩余|还剩)\s*(\d+)\s*(天|小时)', value)
        if relative:
            return anchor + int(relative[1]) * (86400 if relative[2] == '天' else 3600), False
    except ValueError:
        pass
    return None, True
