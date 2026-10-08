import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from contextlib import contextmanager
import time

from pylon.outbox import Outbox
from pylon.tasks.marketplace import poll
from pylon.tasks.marketplace.store import Store


def test_live_notifications_do_not_wait_for_slow_nga_listing(tmp_path):
    store=Store(Outbox(tmp_path/'outbox.sqlite3'))
    store.ingest('v2ex',[{'id':'1','url':'https://example.invalid/t/1','title':'出售 iPhone 15 Pro',
                         'body_full':'iPhone 15 Pro 3000元出售','body_complete':True}])
    release=threading.Event();queued=threading.Event()
    def source(_store,name,deadline):
        if name=='nga':
            assert release.wait(5)
            return ['nga:TimeoutError']
        return []
    from pylon.tasks.marketplace.pipeline import process
    def notify(active,**kwargs):
        result=process(active,current_policy={'version':1,'enabled':True,'categories':['phone.smartphone']},
                       wanted_policy={'version':1,'enabled':False,'targets':[]},**kwargs)
        if result:
            queued.set()
        return result
    with (patch.object(poll.pipeline,'enabled',return_value=True),patch.object(poll,'Store',return_value=store),
          patch.object(poll,'poll_source',side_effect=source),patch.object(poll.pipeline,'process',side_effect=notify),
          patch('pylon.tasks.quotes.health.inspect',return_value={'available':False}),patch.object(poll.events,'result')):
        with ThreadPoolExecutor(max_workers=1) as executor:
            future=executor.submit(poll.main)
            try:
                assert queued.wait(5)
                assert not future.done()
            finally:
                release.set()
            assert future.result(timeout=5)==0
    with store.box.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
    assert store.checkpoint('live:health:v2ex')['failures']==0
    assert store.checkpoint('live:health:nga')['failures']==1


def test_nga_old_sticky_topic_does_not_hide_new_topics_on_the_next_page(tmp_path):
    store=Store(Outbox(tmp_path/'outbox.sqlite3'))
    store.checkpoint('nga:live',{'last_id':100})
    @contextmanager
    def browser(_):
        yield object()
    pages={1:[50,130,120],2:[50,110],3:[50,100,90]}
    requested=[]
    def listing(ctx,source,page):
        requested.append(page)
        return [{'id':str(n),'url':f'https://example.invalid/read.php?tid={n}',
                 'title':'出售 iPhone 14 Pro','body_complete':False} for n in pages[page]]
    with (patch.object(poll.sources,'browser',side_effect=browser),
          patch.object(poll.sources,'list_page',side_effect=listing),
          patch.object(poll.sources,'refill',return_value=[])):
        assert poll.poll_source(store,'nga',time.monotonic()+180)==[]
    assert requested==[1,2,3]
    assert store.checkpoint('nga:live')['last_id']==130
    with store.box.connect() as db:
        assert {r[0] for r in db.execute('SELECT source_id FROM marketplace_posts')}=={'110','120','130'}
