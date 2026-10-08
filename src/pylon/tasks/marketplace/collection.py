"""Independent archival body queues with one reusable client per source."""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import sources
from .store import Store

INTERVALS = {'v2ex': 1.0, 'chiphell': 2.0, 'nga': 15.0}


@contextlib.contextmanager
def client(source):
    if source == 'v2ex':
        import httpx
        from pylon.tasks.forums.v2ex import USER_AGENT
        with httpx.Client(headers={'User-Agent':USER_AGENT}, timeout=8, follow_redirects=True,
                          limits=httpx.Limits(max_connections=1,max_keepalive_connections=1)) as ctx:
            yield ctx,None
    else:
        with sources.browser(source) as ctx:
            page=ctx.new_page()
            try:
                yield ctx,page
            finally:
                page.close()


def emit(event, source, **fields):
    print(json.dumps({'time_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                      'event':event,'source':source,**fields},ensure_ascii=False),flush=True)


def drain_source(store: Store, source: str, *, deadline: float, stop: threading.Event,
                 batch_size: int = 50) -> dict:
    name='body-collection:'+source
    metrics=store.checkpoint(name) or {}
    for key in ('requests','succeeded','unavailable','errors','rate_limits','client_starts'):
        metrics.setdefault(key,0)
    metrics.update(state='running',interval_seconds=INTERVALS[source])
    started=time.monotonic()
    last_request=0.0

    def save():
        metrics.update(pending=store.body_queue(source)['pending'],updated_at=time.time())
        store.checkpoint(name,metrics)

    def wait(seconds):
        return stop.wait(min(max(0,seconds),max(0,deadline-time.monotonic())))

    if not store.body_queue(source)['pending']:
        metrics['state']='complete';save()
        return metrics
    with client(source) as (ctx,page):
        metrics['client_starts']+=1
        while not stop.is_set() and time.monotonic()<deadline:
            queue=store.body_queue(source)
            if not queue['pending']:
                metrics['state']='complete';break
            cooldown=store.checkpoint(source+':cooldown') or {}
            wait_until=max(queue['next_attempt'],float(cooldown.get('until',0)))
            if wait_until>time.time():
                wait(min(60,wait_until-time.time()));continue
            posts=store.pending(history=True,source=source,limit=batch_size)
            for row in posts:
                if stop.is_set() or time.monotonic()>=deadline:break
                if wait(last_request+INTERVALS[source]-time.monotonic()):break
                if time.monotonic()>=deadline:break
                last_request=time.monotonic()
                metrics['requests']+=1
                try:
                    post=sources.detail(ctx,source,{**row,'id':row['source_id']},page=page)
                    if not post['body_complete']:
                        store.body_error(row,'body_missing_or_incomplete',permanent=True)
                        metrics['unavailable']+=1
                    else:
                        store.ingest(source,[post],history=True)
                        metrics['succeeded']+=1
                except sources.SourceRateLimited:
                    metrics['rate_limits']+=1
                    store.body_error(row,'source_rate_limited')
                    # Preserve the site's cooldown independently of the other queues.
                    store.checkpoint(source+':cooldown',{'until':time.time()+60})
                    save()
                    if metrics['rate_limits']>=2:
                        metrics.update(state='rate_limited',last_error='source_rate_limited')
                        save();emit('source_fault',source,reason='repeated_rate_limit')
                        return metrics
                    break
                except sources.ContentUnavailable as error:
                    store.body_error(row,str(error),permanent=True)
                    metrics['unavailable']+=1
                except Exception as error:
                    reason=type(error).__name__
                    store.body_error(row,reason,permanent=row['attempts']>=2)
                    metrics['errors']+=1
                    metrics['last_error']=reason
                save()
    if metrics['state']=='running':
        metrics['state']='complete' if not store.body_queue(source)['pending'] else 'stopped' if stop.is_set() else 'budget'
    metrics['elapsed_seconds']=round(metrics.get('elapsed_seconds',0)+time.monotonic()-started,2)
    save()
    if metrics['state']=='complete':emit('source_complete',source,pending=0,requests=metrics['requests'],succeeded=metrics['succeeded'],rate_limits=metrics['rate_limits'])
    return metrics


def collect_bodies(store: Store, *, max_seconds: int = 3600, batch_size: int = 50,
                   selected_sources: tuple[str,...] = ('v2ex','chiphell','nga'),
                   stop: threading.Event | None = None) -> dict:
    if not selected_sources or set(selected_sources)-INTERVALS.keys():
        raise ValueError('invalid collection sources')
    if len(set(selected_sources))!=len(selected_sources):
        raise ValueError('duplicate collection source')
    stop=stop or threading.Event()
    deadline=time.monotonic()+max_seconds
    # One coordinator owns the database's archival queues; no duplicate drainers.
    lock=Path(str(store.box.path)+'.collection.lock')
    with lock.open('a') as handle:
        try:
            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('history collection is already running') from None
        def run(source):
            try:
                return drain_source(store,source,deadline=deadline,stop=stop,batch_size=batch_size)
            except Exception as error:
                metrics={'state':'error','last_error':type(error).__name__,'pending':store.body_queue(source)['pending']}
                store.checkpoint('body-collection:'+source,metrics)
                emit('source_fault',source,reason=type(error).__name__)
                return metrics
        with ThreadPoolExecutor(max_workers=len(selected_sources),thread_name_prefix='marketplace-body') as pool:
            futures={source:pool.submit(run,source) for source in selected_sources}
            return {source:future.result() for source,future in futures.items()}
