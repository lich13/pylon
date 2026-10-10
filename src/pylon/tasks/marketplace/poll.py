"""NGA polling and shared durable work; unavailable source never blocks others."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pylon import events
from . import pipeline, sources, enrich
from .store import Store


def poll_source(store: Store, source: str, deadline: float) -> list[str]:
    errors = []
    try:
        if source!='nga':
            return sources.refill(store,limit=12,selected_sources=(source,),deadline=deadline)
        if (store.checkpoint('nga:cooldown') or {}).get('until',0)>time.time():
            raise sources.SourceRateLimited('nga_rate_limited')
        with sources.browser('nga') as ctx:
            checkpoint = store.checkpoint('nga:live') or {}
            latest = int(checkpoint.get('last_id',0))
            newest = max(latest,int(checkpoint.get('newest_id',latest)))
            start_page = max(1,int(checkpoint.get('next_page',1))) if latest else 1
            for page in range(start_page,start_page+8):
                if time.monotonic()>=deadline:
                    break
                posts = sources.list_page(ctx,'nga',page)
                fresh = [p for p in posts if int(p['id'])>latest]
                # Initial enablement establishes a baseline, not a flood of old posts.
                store.ingest('nga',fresh,history=not bool(latest))
                newest = max([newest]+[int(p['id']) for p in posts])
                # Sticky old topics can appear before newer pages. Reach the
                # boundary only when the whole page contains no unseen IDs.
                if not latest or not fresh:
                    store.checkpoint('nga:live',{'last_id':newest,'at':time.time()})
                    break
                # Persist only after ingestion. A bounded run or a subsequent
                # request failure resumes this scan instead of repeating page 1.
                store.checkpoint('nga:live',{'last_id':latest,'newest_id':newest,
                                            'next_page':page+1,'at':time.time()})
            errors.extend(sources.refill(store,limit=12,selected_sources=('nga',),ctx=ctx,deadline=deadline) or [])
    except Exception as error:
        errors.append(source+':'+type(error).__name__)
        if isinstance(error,sources.SourceRateLimited):
            previous_until = (store.checkpoint('nga:cooldown') or {}).get('until',0)
            if previous_until<=time.time():
                store.checkpoint('nga:cooldown',{'until':time.time()+60})
    return errors


def collection_progress(store: Store, source: str) -> tuple:
    with store.box.connect() as db:
        return tuple(db.execute('''SELECT count(*),coalesce(sum(body_complete),0),coalesce(max(first_seen),0)
            FROM marketplace_posts WHERE source=? AND history=0''',(source,)).fetchone())


def record_health(store: Store, source: str, errors: list[str], *, before: tuple | None = None) -> None:
    from pylon.tasks.intelligence.health import record
    now = time.time()
    name='live:health:'+source
    previous = store.checkpoint(name) or {}
    current=collection_progress(store,source)
    baseline=before if before is not None else previous.get('collected',current)
    progressed=any(new>old for new,old in zip(current,baseline))
    with store.box.connect() as db:
        pending=db.execute('''SELECT count(*) FROM marketplace_posts WHERE source=? AND history=0
            AND status IN ('pending_body','refresh_required')''',(source,)).fetchone()[0]
    # A failed poll itself is unfinished collection work. Successfully collected
    # posts still count as progress when another page or body failed this round.
    record(store.box,'marketplace:'+source+':collection',backlog=max(pending,int(bool(errors))),
           progress=progressed or not errors,error=';'.join(errors),now=now)
    store.checkpoint(name,{'failures':int(previous.get('failures',0))+1 if errors else 0,
                          'errors':errors,'at':now,'collected':current})


def main() -> int:
    if not pipeline.enabled():
        print('{"wakeAgent":false}')
        return 0
    store=Store()
    errors=[]
    count=pipeline.process(store)
    deadline=time.monotonic()+180
    before={source:collection_progress(store,source) for source in ('v2ex','chiphell','nga')}
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='marketplace-source') as pool:
        futures={pool.submit(poll_source,store,source,deadline):source for source in ('v2ex','chiphell','nga')}
        for future in as_completed(futures):
            source=futures[future]
            source_errors=future.result() or []
            errors.extend(source_errors)
            record_health(store,source,source_errors,before=before[source])
            # Queue this source immediately, without waiting for NGA's browser.
            count+=pipeline.process(store,source=source)
    events.result('failed' if errors else 'ok' if count else 'no_change',
                  (';'.join(errors) or f'交易通知入队 {count}')+'；'+enrich.summary(store),changed=bool(count))
    print('{"wakeAgent":false}')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
