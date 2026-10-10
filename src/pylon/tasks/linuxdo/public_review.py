"""Review public followed-author posts without exposing personal notifications."""
from __future__ import annotations

import json
import time
from dataclasses import replace

from . import trade
from .common import clean, digest, timestamp
from .helper import HelperNotice, SOURCE, FOLLOWING
from .helper_store import HelperStore
from .sources import Page


def snapshot_for(notice: HelperNotice, version: str) -> dict:
    ref = f'tg:{SOURCE}:{notice.message_id}'
    evidence = {'ref': ref, 'origin': 'telegram', 'post_number': notice.post_number,
                'url': notice.url, 'title': clean(notice.title, 300), 'body': clean(notice.text),
                'links': notice.links, 'complete': False, 'observed_at': 0,
                'source_published_at': notice.published_at, 'source_edited_at': notice.edited_at}
    return {'topic_id': notice.topic_id, 'title': notice.title, 'evidence': [evidence],
            'fingerprint': digest([version, evidence]), 'actors': {ref: digest(notice.actor.casefold())}}


def stage(store, *, limit=100):
    """Public author notices share the topic queue; personal notices never join."""
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows=db.execute("""SELECT r.*,m.payload,m.kind FROM linuxdo_public_reviews r
            JOIN linuxdo_helper_messages m USING(message_id)
            WHERE m.route='following' AND r.status IN ('pending','hold')
            ORDER BY r.message_id LIMIT ?""",(limit,)).fetchall()
        for row in rows:
            if row['kind'] not in FOLLOWING:
                raise ValueError('private notification in public queue')
            data=json.loads(row['payload'])
            notice=HelperNotice(**{k:v for k,v in data.items() if not k.startswith('_')})
            observation=replace(notice,route='board').observation()
            store.save_page(SOURCE,Page([observation],[notice.message_id]),store_checkpoint(db),connection=db)
            db.execute("UPDATE linuxdo_public_reviews SET status='intelligence',lease_until=0,error='' WHERE message_id=?",(notice.message_id,))
        return len(rows)


def review(store, *, limit=100, **unused):
    return {'processed':stage(store,limit=limit),'errors':0,'queued':0}


def store_checkpoint(db) -> dict:
    row = db.execute('SELECT checkpoint FROM linuxdo_sources WHERE channel=?', (SOURCE,)).fetchone()
    return json.loads(row[0]) if row else {}
