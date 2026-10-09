"""Synthetic regressions for regional eligibility, not seller location."""
import json

import pytest

from pylon.outbox import Outbox
from pylon.tasks.marketplace import classifier, policy, wanted
from pylon.tasks.marketplace.pipeline import format_message, process
from pylon.tasks.marketplace.store import Store


POLICY = {'version': 2, 'enabled': True, 'categories': ['phone.smartphone', 'virtual.service']}


@pytest.mark.parametrize('title,body,allowed', [
    ('出广州京东家政', '广州京东家政2小时，50元出', False),
    ('出北京京东家政', '北京京东家政2小时，50元出', True),
    ('出京东家政', '京东家政2小时，50元出', False),
    ('广州出京东家政', '京东家政2小时，全国通用，50元出', True),
    ('出广州京东家政', '广州京东家政2小时，50元出，卡券可邮寄', False),
    ('出广州京东家政', '广州京东家政2小时，50元出，本地优先', False),
    ('出京东家政', '京东家政2小时，全国通用但北京除外，50元出', False),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州本地面交优先', True),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州本地优先', True),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，支持广州自提', True),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州自提', False),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州仅面交', False),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州本地优先，不出外地', False),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州可邮寄', True),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州自提，也可邮寄', True),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，广州本地面交优先，走闲鱼', True),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，仅限广州面交，走闲鱼', False),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，北京仅自提', True),
    ('广州出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，不限本地，可自提', True),
    ('广州出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，只支持面交或邮寄', True),
    ('广州出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，本地优先，不包邮', True),
    ('仅广州面交出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出售', False),
    ('广州出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，外地勿扰', False),
    ('出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，仅限广州', False),
    ('辽宁朝阳出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，仅限本地自提', False),
    ('北京出 iPhone 14 Pro', 'iPhone 14 Pro 2000元出，北京购入，现在仅广州面交', False),
])
@pytest.mark.parametrize('source', ['v2ex', 'chiphell', 'nga'])
def test_sale_eligibility_uses_actual_delivery_or_service_area(title, body, allowed, source):
    items = classifier.classify_listing(title, body, source, True)
    assert bool(policy.evaluate(items, POLICY)) is allowed


def test_mixed_services_and_products_keep_their_own_locations():
    items = classifier.classify_listing('闲置分别出售',
        '1. 广州京东家政2小时，50元出\n'
        '2. 北京京东家政2小时，60元出\n'
        '3. iPhone 14 Pro 2000元出，广州本地面交优先', 'v2ex', True)
    matches = policy.evaluate(items, POLICY)
    assert len(matches) == 2
    assert all('广州京东家政' not in i['text'] for i in matches)
    post = {'source':'v2ex','title':'闲置分别出售','body':'','body_complete':True,
            'url':'https://example.invalid/post/1'}
    message = format_message(post, matches, [])
    assert '仅北京可用' in message
    assert '广州京东家政' not in message


@pytest.mark.parametrize('terms,allowed', [
    ('广州本地面交优先', True), ('支持广州自提', True),
    ('仅广州面交', False), ('北京仅面交', True), ('广州不出外地', False),
])
def test_wanted_has_the_same_delivery_boundary(terms, allowed):
    items = classifier.classify_listing('求购 iPhone 14 Pro', '收 iPhone 14 Pro，'+terms, 'v2ex', True)
    inventory = {'version':1,'enabled':True,'targets':[
        {'category':'phone.smartphone','model':'iPhone 14 Pro','specs':{},'region':''}]}
    assert bool(wanted.evaluate(items, inventory)) is allowed


def test_old_unqueued_parsing_is_refreshed_without_replaying_history(tmp_path, monkeypatch):
    box = Outbox(tmp_path/'outbox.sqlite3')
    store = Store(box)
    post = {'id':'1','title':'出广州京东家政','url':'https://example.invalid/post/1',
            'body_full':'广州京东家政2小时50元出售','body_complete':True}
    store.ingest('v2ex', [post])
    store.ingest('v2ex', [post | {'id':'2','url':'https://example.invalid/post/2'}], history=True)
    with box.connect() as db:
        for row in db.execute('SELECT source_id,items FROM marketplace_posts').fetchall():
            items = json.loads(row['items'])
            for item in items:
                item['attributes'].update(classifier_version=2,transaction_scope='not_local',pickup_only=False)
            db.execute('UPDATE marketplace_posts SET items=? WHERE source_id=?', (json.dumps(items),row['source_id']))
    from pylon.tasks.quotes import health
    monkeypatch.setattr(health, 'inspect', lambda _: {'available':False})
    assert process(store,current_policy=POLICY,wanted_policy={'enabled':False,'version':1}) == 0
    with box.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0
        assert db.execute("SELECT status FROM marketplace_posts WHERE source_id='1'").fetchone()[0] == 'skipped'
        old = json.loads(db.execute("SELECT items FROM marketplace_posts WHERE source_id='2'").fetchone()[0])
        assert old[0]['attributes']['classifier_version'] == 2
