"""Resumable archival collection. Never calls the notification pipeline."""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
import sqlite3
import time
import hashlib
from pathlib import Path
from .store import Store
from .sources import browser, list_page, detail, refill, HistoryUnavailable


def import_existing(store: Store, root: Path) -> dict:
    from pylon.tasks.chiphell.categories import history_posts
    chh = history_posts(root/'profiles/webops/data/chiphell_fid26/delivery.sqlite3')
    for post in chh:
        match = re.search(r'(?:thread-|tid=)(\d+)',post['url'])
        post['id'] = match[1] if match else ''
    count = store.ingest('chiphell',chh,history=True)
    state_path = root/'profiles/webops/cron_state/v2ex_notice.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    count_v2 = store.ingest('v2ex',[{**p,'id':str(p.get('message_id','')),'url':p.get('v2ex_url','')}
                                  for p in state.get('skipped_items',[]) if p.get('v2ex_url') and '二手交易' in p.get('title','')],history=True)
    outbox_path = root/'data/outbox.sqlite3'
    if outbox_path.is_file():
        with sqlite3.connect(outbox_path.resolve().as_uri()+'?mode=ro',uri=True) as db:
            for payload,stamp in db.execute("SELECT payload,created_at FROM events WHERE job_id='93e1c037c069' AND kind='text'"):
                text = str(json.loads(payload).get('text') or '')
                posts = []
                for title,url in re.findall(r'\[(.*?)\]\((https://(?:www\.)?v2ex.com/t/\d+[^)]*)\)',text):
                    if '二手' not in title and '二手出售信息' not in text:
                        continue
                    sid = re.search(r'/t/(\d+)',url)[1]
                    posts.append({'id':sid,'title':title,'url':url,
                                  'posted_at':dt.datetime.fromtimestamp(stamp,dt.timezone.utc).isoformat()})
                count_v2 += store.ingest('v2ex',posts,history=True)
    return {'chiphell_imported':count,'v2ex_skipped_imported':count_v2}


def backfill(store: Store, source: str, *, days: int = 90, max_pages: int = 5000,
             delay: float = 2, max_seconds: int = 3600, fetch_bodies: bool = True) -> dict:
    cutoff = dt.datetime.now(dt.timezone.utc)-dt.timedelta(days=days)
    checkpoint_name = 'history:'+source
    cp = store.checkpoint(checkpoint_name) or {'page':1,'before':None,'pages':0,'complete':False,'gaps':0,'days':days,
                                             'cutoff':cutoff.isoformat()}
    if cp.get('days') != days:
        raise ValueError('history checkpoint has a different date window')
    cutoff = dt.datetime.fromisoformat(cp.get('cutoff',cutoff.isoformat()))
    if cp['complete']:
        return cp
    deadline = time.monotonic()+max_seconds
    cm = contextlib.nullcontext(None) if source=='v2ex' else browser(source)
    with cm as ctx:
        for _ in range(max_pages):
            if time.monotonic()>=deadline:
                break
            if source=='v2ex':
                from pylon.tasks.forums.v2ex import BASE_URL, fetch, parse_posts
                from .pipeline import is_v2ex_trade
                raw = fetch(BASE_URL+(f"?before={cp['before']}" if cp['before'] else ''),timeout=12,attempts=1,phase='history')
                parsed = parse_posts(raw)
                dates = [p.posted_at for p in parsed if p.posted_at]
                batch = [{'id':str(p.message_id),'title':p.title,'url':p.v2ex_url,'posted_at':p.posted_at,
                          'body_excerpt':p.body_excerpt,'body_complete':False} for p in parsed if is_v2ex_trade(p)]
                next_before = min((p.message_id for p in parsed),default=cp['before'])
                ended = not parsed or next_before==cp['before']
            else:
                try:
                    batch = list_page(ctx,source,cp['page'])
                except HistoryUnavailable as error:
                    cp.update(complete=True,limited=True,stop_reason=str(error))
                    store.checkpoint(checkpoint_name,cp)
                    break
                dates = [p['posted_at'] for p in batch if p['posted_at']]
                ended = not batch
                next_before = None
            signature = hashlib.sha256(','.join(sorted(str(p['id']) for p in batch)).encode()).hexdigest()
            signatures = cp.get('page_signatures',[])
            if batch and signature in signatures:
                cp.update(complete=True,stop_reason='pagination_repeated',limited=True)
                store.checkpoint(checkpoint_name,cp)
                break
            cp['page_signatures'] = (signatures+[signature])[-10000:]
            reached = False
            fresh = []
            for post in batch:
                if post['posted_at']:
                    try:
                        if dt.datetime.fromisoformat(post['posted_at'].replace('Z','+00:00')) < cutoff:
                            continue
                    except ValueError:
                        pass
                if fetch_bodies and time.monotonic()<deadline:
                    try:
                        post = detail(ctx,source,post)
                        time.sleep(delay)
                    except Exception as error:
                        cp['gaps'] += 1
                        cp['last_detail_error'] = type(error).__name__
                fresh.append(post)
            store.ingest(source,fresh,history=True)
            if dates:
                try:
                    reached = len(dates)>=max(1,len(batch)*.8) and all(dt.datetime.fromisoformat(d.replace('Z','+00:00'))<cutoff for d in dates)
                except ValueError:
                    pass
            cp.update(page=cp['page']+1,before=next_before,pages=cp['pages']+1,
                      complete=bool(reached or ended),last_count=len(batch),
                      oldest=min(dates) if dates else cp.get('oldest',''),newest=max(dates) if dates else '',
                      missing_dates=sum(not p.get('posted_at') for p in batch))
            store.checkpoint(checkpoint_name,cp)
            print(json.dumps({'source':source,'pages':cp['pages'],'stored':len(fresh),'gaps':cp['gaps'],
                              'complete':cp['complete']},ensure_ascii=False),flush=True)
            if cp['complete']:
                break
            time.sleep(delay)
    return cp
