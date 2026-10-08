from contextlib import contextmanager
import threading
from concurrent.futures import ThreadPoolExecutor

from pylon.outbox import Outbox
from pylon.tasks.marketplace import collection, sources
from pylon.tasks.marketplace.store import Store


def seed(tmp_path, counts):
    store=Store(Outbox(tmp_path/'analysis.sqlite3'))
    for source,count in counts.items():
        store.ingest(source,[{'id':str(n+1),'title':'出售显卡',
                             'url':f'https://example.invalid/t/{n+1}'} for n in range(count)],history=True)
    return store


def test_slow_nga_does_not_block_v2ex_and_clients_are_reused(tmp_path,monkeypatch):
    store=seed(tmp_path,{'v2ex':3,'nga':2})
    release=threading.Event();v2_done=threading.Event();starts=[];requests=[]
    @contextmanager
    def client(source):
        starts.append(source)
        yield source,object()
    def detail(ctx,source,post,**kwargs):
        requests.append(source)
        if source=='nga':
            assert release.wait(5)
        if source=='v2ex' and requests.count('v2ex')==3:v2_done.set()
        return {**post,'body_full':'RTX 4070 显卡 2000元出售','body_complete':True}
    monkeypatch.setattr(collection,'client',client)
    monkeypatch.setattr(sources,'detail',detail)
    monkeypatch.setattr(collection,'INTERVALS',dict.fromkeys(collection.INTERVALS,0))
    with ThreadPoolExecutor(max_workers=1) as pool:
        result=pool.submit(collection.collect_bodies,store,max_seconds=10,batch_size=1,selected_sources=('v2ex','nga'))
        try:
            assert v2_done.wait(5), 'V2EX was blocked by NGA'
            assert not result.done()
        finally:release.set()
        metrics=result.result(timeout=5)
    assert starts.count('v2ex')==starts.count('nga')==1
    assert all(m['state']=='complete' for m in metrics.values())
    with store.box.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
        assert db.execute('SELECT COUNT(*) FROM marketplace_posts WHERE event_id IS NOT NULL').fetchone()[0]==0


def test_source_retry_and_cooldown_do_not_touch_other_queues(tmp_path,monkeypatch):
    store=seed(tmp_path,{'v2ex':1,'nga':1})
    @contextmanager
    def client(source):yield source,None
    def detail(ctx,source,post,**kwargs):
        if source=='nga':raise sources.SourceRateLimited('nga_rate_limited')
        return {**post,'body_full':'出售显卡 2000元','body_complete':True}
    monkeypatch.setattr(collection,'client',client)
    monkeypatch.setattr(sources,'detail',detail)
    store.checkpoint('body-collection:nga',{'rate_limits':1})
    results=collection.collect_bodies(store,max_seconds=5,selected_sources=('v2ex','nga'))
    assert results['v2ex']['state']=='complete'
    assert results['nga']['state']=='rate_limited'
    assert store.body_queue('nga')['pending']==1
    assert store.checkpoint('nga:cooldown')['until']>0
    assert store.checkpoint('v2ex:cooldown') is None


def test_future_retry_is_not_marked_complete_or_refetched(tmp_path,monkeypatch):
    store=seed(tmp_path,{'nga':1})
    row=store.pending(history=True,source='nga')[0]
    store.body_error(row,'TimeoutError')
    stop=threading.Event();stop.set()
    @contextmanager
    def client(source):yield source,None
    monkeypatch.setattr(collection,'client',client)
    result=collection.collect_bodies(store,max_seconds=5,selected_sources=('nga',),stop=stop)
    assert result['nga']['state']=='stopped'
    assert result['nga']['requests']==0
    assert Store(Outbox(store.box.path)).body_queue('nga')['pending']==1


def test_completed_source_never_opens_a_browser(tmp_path,monkeypatch):
    store=seed(tmp_path,{})
    def unexpected(_):raise AssertionError('completed source opened a browser')
    monkeypatch.setattr(collection,'client',unexpected)
    results=collection.collect_bodies(store,max_seconds=5)
    assert all(m['state']=='complete' and m['client_starts']==0 for m in results.values())


def test_source_selection_rejects_duplicate_drainers(tmp_path):
    import pytest
    store=seed(tmp_path,{})
    with pytest.raises(ValueError,match='duplicate'):
        collection.collect_bodies(store,selected_sources=('nga','nga'))
