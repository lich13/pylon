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


def record_health(store: Store, source: str, errors: list[str]) -> None:
    now = time.time()
    name='live:health:'+source
    previous = store.checkpoint(name) or {}
    if errors:
        since = previous.get('since',now)
        failures = int(previous.get('failures',0))+1
        alerted = bool(previous.get('alerted'))
        if not alerted and failures>=5 and now-since>=1800:
            store.box.enqueue(profile='webops',job_id='marketplace-poll',kind='text',
                payload={'text':'二手交易监控持续异常：'+','.join(errors)+'。其他来源和已入队通知继续处理。'},
                key='marketplace-fault:'+source+':'+str(since))
            alerted = True
        store.checkpoint(name,{'since':since,'failures':failures,'alerted':alerted,'errors':errors,'at':now})
    else:
        store.checkpoint(name,{'failures':0,'at':now})


def main() -> int:
    if not pipeline.enabled():
        print('{"wakeAgent":false}')
        return 0
    store=Store()
    errors=[]
    count=pipeline.process(store)
    deadline=time.monotonic()+180
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='marketplace-source') as pool:
        futures={pool.submit(poll_source,store,source,deadline):source for source in ('v2ex','chiphell','nga')}
        futures[pool.submit(enrich.run_pending,store)]='enrichment'
        for future in as_completed(futures):
            source=futures[future]
            if source=='enrichment':
                future.result()
                count+=pipeline.process(store)
                continue
            source_errors=future.result() or []
            errors.extend(source_errors)
            record_health(store,source,source_errors)
            # Queue this source immediately, without waiting for NGA's browser.
            count+=pipeline.process(store,source=source)
    events.result('failed' if errors else 'ok' if count else 'no_change',
                  (';'.join(errors) or f'交易通知入队 {count}')+'；'+enrich.summary(store),changed=bool(count))
    print('{"wakeAgent":false}')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
