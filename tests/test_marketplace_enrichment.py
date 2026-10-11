import json
import time

import pytest

from pylon.outbox import Outbox
from pylon.shared.html_text import body_text
from pylon.tasks.marketplace import classifier, enrich, pipeline, policy
from pylon.tasks.marketplace.store import Store


def example():
    return dict(source='v2ex',source_id='42',title='多件闲置出售',body_complete=True,
                url='https://example.invalid/t/42',history=0,
                body='类型 | 名称 | 价格 |\n手机 | iPhone 15 Pro | 4,123 |\n'
                     '手表 | AppleWatchUltra2 | 2,345 |\n耳机 | AirPods4 | 567 |\n'
                     '耳机 | AirPods4 | 678 |\n全国可邮寄，北京自提优先\n'
                     '详细信息和闲鱼入口 https://example.invalid/details')


def answer():
    return {'items':[dict(name=name,category=category,start_line=i,end_line=i,price=price,
                         price_line=i,status='available',link_ids=[])
                     for i,(name,category,price) in enumerate([
                         ('iPhone 15 Pro','phone.smartphone',4123),
                         ('AppleWatchUltra2','wearable.watch',2345),
                         ('AirPods4','audio.tws',567),('AirPods4','audio.tws',678)],2)],
            'common_lines':[6],'shared_link_ids':['L0']}


def seed(tmp_path,monkeypatch,*,historical=False):
    monkeypatch.setattr(enrich,'model_name',lambda:'fixture-model')
    store=Store(Outbox(tmp_path/'queue.sqlite3'))
    post=example()
    store.ingest(post['source'],[dict(id=post['source_id'],title=post['title'],url=post['url'],
                                    body_full=post['body'],body_complete=True)],history=historical)
    with store.box.connect() as db:
        post=dict(db.execute('SELECT * FROM marketplace_posts').fetchone())
    return store,post


def test_marketplace_html_keeps_table_rows_and_real_anchor_targets():
    text=body_text('<div class="topic_content"><table><tr><th>名称</th><th>价格</th></tr>'
                   '<tr><td>AppleWatchUltra2</td><td>2,345</td></tr></table>'
                   '<p><a href="https://example.invalid/details"><b>闲鱼详情</b></a></p>'
                   '<a href="javascript:bad()">无效</a><script>秘密</script></div>',classes={'topic_content'})
    assert 'AppleWatchUltra2 | 2,345 |' in text
    assert '闲鱼详情 https://example.invalid/details' in text
    assert 'javascript' not in text and '秘密' not in text
    assert '名称 | 价格 |' in text


def test_marketplace_pretty_html_does_not_break_table_cells_into_rows():
    text=body_text('<table>\n<tr>\n<td>\n耳机\n</td>\n<td><p>AirPods 4</p></td>\n'
                   '<td>\n567\n</td>\n</tr>\n<tr><td>手机</td><td>iPhone 14</td><td>2,345</td></tr></table>')
    rows=[line for line in text.splitlines() if line.strip()]
    assert len(rows)==2
    assert rows[0].count('|')==3 and 'AirPods 4' in rows[0] and '567' in rows[0]
    assert rows[1].count('|')==3 and '2,345' in rows[1]


def test_marketplace_image_download_controls_do_not_become_trade_links():
    text=body_text('<a href="https://example.invalid/forum.php?mod=attachment&amp;aid=fixture">下载附件</a>'
                   '<a href="https://example.invalid/photo.jpg?sig=fixture">photo.jpg</a>'
                   '<a href="https://example.invalid/item/42">商品详情</a>')
    assert 'aid=' not in text and 'sig=' not in text
    assert enrich.links(text)==['https://example.invalid/item/42']


def test_marketplace_touchpad_does_not_expand_mouse_subscription():
    items=classifier.classify_listing('闲置出售','Magic Trackpad 678元出\nMagic Keyboard 456元出')
    assert {i['category'] for i in items}=={'input.touchpad','input.keyboard'}
    assert policy.evaluate(items,{'enabled':True,'types':['input.mouse'],'categories':[]})==[]


def test_marketplace_summary_title_not_copied_to_each_product():
    post=example();post['title']='出售 iPhone 和 AirPods'
    value=answer();value['common_lines'].append(0)
    items=enrich.validate(value,post)
    assert 'AirPods' not in items[0]['text']


def test_marketplace_unknown_unselected_item_does_not_block_known_products():
    post=example();post['body']=post['body'].replace('耳机 | AirPods4 | 678 |','小物件 | 物件甲 | 678 |')
    value=answer();value['items'][-1].update(name='物件甲',category='unknown')
    items=enrich.validate(value,post)
    assert items[-1]['category']=='unknown'
    assert len(policy.evaluate(items,{'enabled':True,'groups':['phone'],'categories':[]}))==1


def test_marketplace_gifted_accessory_does_not_cancel_device_sale():
    post=example();post['body']=post['body'].replace('4,123 |','4,123 | 送手机壳')
    value=answer()
    # Gift accessories do not supply the device's price or condition.
    items=enrich.validate(value,post)
    assert items[0]['category']=='phone.smartphone' and items[0]['price']==4123
    assert items[0]['status']=='available'


def test_marketplace_device_name_on_case_is_not_a_device():
    post=example();post['body']=post['body'].replace('手机 | iPhone 15 Pro |','手机壳 | iPhone 15 Pro |')
    with pytest.raises(enrich.AnalysisError,match='unsupported_category'):
        enrich.validate(answer(),post)


def test_marketplace_thousands_not_split_into_items_or_model_numbers():
    post=example()
    items=classifier.classify_listing(post['title'],post['body'])
    prices={i['price'] for i in items}
    assert {4123,2345,567,678}<=prices
    assert not prices & {2,4,123,345}
    assert enrich.needs_analysis(post,items)
    assert not enrich.needs_analysis({**post,'body_complete':False},items)
    assert classifier.asking_price('AppleWatchUltra2') is None
    assert classifier.asking_price('AirPods4') is None


def test_marketplace_verified_table_items_duplicate_names_prices_and_links():
    items=enrich.validate(answer(),example())
    assert [i['price'] for i in items]==[4123,2345,567,678]
    assert [i['name'] for i in items].count('AirPods4')==2
    assert all(i['attributes']['shipping'] for i in items)
    text=pipeline.format_message(example(),items,[])
    assert text.count('https://example.invalid/details')==1
    assert '详情／交易入口' in text and '2345 元' in text
    assert '567 元' in text and '678 元' in text


@pytest.mark.parametrize('mutation,reason',[
    (lambda a:a['items'][0].update(price=9999),'price_without_evidence'),
    (lambda a:a['items'][0].update(price_line=3),'price_outside_item'),
    (lambda a:a['items'][0].update(name='iPhone 99'),'invented_subject'),
    (lambda a:a['items'][0].update(category='component.gpu'),'unsupported_category'),
    (lambda a:a.update(shared_link_ids=['https://example.invalid/invented']),'invented_link'),
    (lambda a:a.update(shared_link_ids=[]),'omitted_link'),
    (lambda a:a.update(common_lines=[]),'omitted_constraint'),
    (lambda a:a['items'].pop(),'omitted_product'),
    (lambda a:a['items'][1].update(start_line=2),'overlapping_items'),
])
def test_marketplace_rejects_unsupported_or_omitted_evidence(mutation,reason):
    value=answer();mutation(value)
    with pytest.raises(enrich.AnalysisError,match=reason):enrich.validate(value,example())


def test_marketplace_local_region_and_sold_facts_remain_authoritative():
    post=example();post['body']=post['body'].replace('全国可邮寄，北京自提优先','仅广州自提，不邮寄')
    items=enrich.validate(answer(),post)
    assert policy.evaluate(items,{'enabled':True,'groups':['phone'],'categories':[]})==[]
    post=example();post['body']=post['body'].replace('4,123 |','4,123 | 已售')
    with pytest.raises(enrich.AnalysisError,match='status_conflict'):
        enrich.validate(answer(),post)
    value=answer();value['items'][0]['status']='sold'
    assert enrich.validate(value,post)[0]['status']=='sold'


def test_marketplace_single_verified_span_keeps_negative_condition():
    post=example();post['body']=post['body'].replace('4,123 |','4,123 | 使用两年，电池健康80%')
    items=enrich.validate(answer(),post)
    assert items[0]['attributes']['condition']!='全新'
    assert '电池健康80%' in items[0]['text']


def test_marketplace_item_link_cannot_leak_to_other_item():
    post=example();post['body']=post['body'].replace('4,123 |','4,123 | https://example.invalid/phone')
    value=answer();value['shared_link_ids']=['L1'];value['items'][0]['link_ids']=['L0']
    items=enrich.validate(value,post)
    text=pipeline.format_message(post,items[1:],[])
    assert 'example.invalid/phone' not in text
    assert 'example.invalid/details' in text
    value['items'][1]['link_ids']=['L0']
    with pytest.raises(enrich.AnalysisError,match='unbound_item_link'):enrich.validate(value,post)


def test_marketplace_preserves_omitted_reference_in_verified_item_span():
    post=dict(source='chh',source_id='43',title='出售 iPhone 15 Pro',body_complete=True,
              body='iPhone 15 Pro 256G 售价 4123元\n'
                   '屏幕资料 https://example.invalid/manufacturer/display')
    value={'items':[dict(name='iPhone 15 Pro',category='phone.smartphone',start_line=1,
                        end_line=2,price=4123,price_line=1,status='available',link_ids=[])],
           'common_lines':[],'shared_link_ids':[]}
    items=enrich.validate(value,post)
    assert items[0]['price']==4123
    assert items[0]['attributes']['links']==['https://example.invalid/manufacturer/display']


def test_marketplace_omitted_owned_link_stays_with_its_product():
    post=example();post['body']=post['body'].replace('4,123 |','4,123 | https://example.invalid/phone')
    value=answer();value['shared_link_ids']=['L1']
    items=enrich.validate(value,post)
    assert items[0]['attributes']['links']==['https://example.invalid/phone']
    assert all(not item['attributes']['links'] for item in items[1:])
    text=pipeline.format_message(post,items[1:],[])
    assert 'example.invalid/phone' not in text and 'example.invalid/details' in text


def test_marketplace_link_prefix_does_not_prove_item_ownership():
    post=example();post['body']=post['body'].replace('4,123 |','4,123 | https://example.invalid/details-other')
    value=answer();value['shared_link_ids']=['L1'];value['items'][0]['link_ids']=['L1']
    with pytest.raises(enrich.AnalysisError,match='unbound_item_link'):
        enrich.validate(value,post)
    value['items'][0]['link_ids']=[]
    items=enrich.validate(value,post)
    assert items[0]['attributes']['links']==['https://example.invalid/details-other']
    assert all(item['attributes']['shared_links']==['https://example.invalid/details'] for item in items)


def test_marketplace_analysis_restart_cache_and_delivery_once(tmp_path,monkeypatch):
    store,post=seed(tmp_path,monkeypatch)
    args=dict(current_policy={'enabled':True,'version':1,'groups':['phone'],'categories':[]},
              wanted_policy={'version':1,'enabled':False,'targets':[]})
    monkeypatch.setattr('pylon.tasks.quotes.health.inspect',lambda *_:{'available':False})
    calls=[]
    def request(p):
        calls.append(p['source_id']);return enrich.validate(answer(),p),'fixture-request'
    monkeypatch.setattr(enrich,'request',request)
    assert pipeline.process(store,**args)==0
    assert pipeline.process(store,**args)==0
    store=Store(Outbox(store.box.path))
    assert enrich.run_pending(store)==1
    assert pipeline.process(store,**args)==1
    assert enrich.run_pending(store)==0
    assert pipeline.process(store,**args)==0
    assert calls==['42']
    with store.box.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==1
        assert db.execute('SELECT COUNT(*) FROM marketplace_analyses').fetchone()[0]==1
        assert enrich.prepare(db,post,json.loads(post['items']))[0]['price']==4123


def test_marketplace_analysis_timeout_bounded_and_no_raw_error(tmp_path,monkeypatch):
    store,post=seed(tmp_path,monkeypatch)
    with store.box.connect() as db:assert enrich.prepare(db,post,json.loads(post['items'])) is None
    def request(_):raise TimeoutError('private raw text must not enter state')
    monkeypatch.setattr(enrich,'request',request)
    for _ in range(3):
        assert enrich.run_pending(store)==0
        with store.box.connect() as db:db.execute('UPDATE marketplace_analyses SET next_attempt=0')
    with store.box.connect() as db:
        row=dict(db.execute('SELECT * FROM marketplace_analyses').fetchone())
        assert row['status']=='error' and row['attempts']==3 and row['error']=='TimeoutError'
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
    assert enrich.run_pending(store)==0


def test_marketplace_analysis_recovers_interrupted_read_only_work(tmp_path,monkeypatch):
    store,post=seed(tmp_path,monkeypatch)
    with store.box.connect() as db:
        enrich.prepare(db,post,json.loads(post['items']))
        db.execute("UPDATE marketplace_analyses SET status='running',started_at=?,attempts=1",(time.time()-200,))
    monkeypatch.setattr(enrich,'request',lambda p:(enrich.validate(answer(),p),'resumed'))
    assert enrich.run_pending(Store(Outbox(store.box.path)))==1


def test_marketplace_history_never_calls_grok_or_creates_analysis(tmp_path,monkeypatch):
    store,post=seed(tmp_path,monkeypatch,historical=True)
    with store.box.connect() as db:
        items=json.loads(post['items'])
        assert enrich.prepare(db,post,items)==items
        assert db.execute('SELECT COUNT(*) FROM marketplace_analyses').fetchone()[0]==0
    monkeypatch.setattr(enrich,'request',lambda p:pytest.fail('history called Grok'))
    assert enrich.run_pending(store)==0


def test_marketplace_grok_has_no_tools_or_contact_input(monkeypatch):
    post=example();post['body']+='\n邮箱 sample@example.invalid\n微信 fixture_contact'
    prompt=enrich.build_prompt(post)
    assert 'sample@example.invalid' not in prompt and 'fixture_contact' not in prompt
    assert '不得联网' in prompt and '不能执行' in prompt


def test_marketplace_source_changes_in_flight_return_to_fresh_analysis(tmp_path,monkeypatch):
    store,post=seed(tmp_path,monkeypatch)
    with store.box.connect() as db:enrich.prepare(db,post,json.loads(post['items']))
    def request(p):
        with store.box.connect() as db:db.execute("UPDATE marketplace_posts SET title='新标题'")
        return enrich.validate(answer(),p),'changed'
    monkeypatch.setattr(enrich,'request',request)
    assert enrich.run_pending(store)==0
    with store.box.connect() as db:
        assert db.execute('SELECT status FROM marketplace_posts').fetchone()[0]=='ready'
        assert db.execute('SELECT error FROM marketplace_analyses').fetchone()[0]=='source_changed'


def test_marketplace_analysis_wait_does_not_hold_simple_posts(tmp_path,monkeypatch):
    store,post=seed(tmp_path,monkeypatch)
    store.ingest('chiphell',[dict(id='43',title='出 iPhone 14',url='https://example.invalid/43',
                                 body_full='iPhone 14 明盘2345元，可邮寄',body_complete=True)])
    monkeypatch.setattr('pylon.tasks.quotes.health.inspect',lambda *_:{'available':False})
    assert pipeline.process(store,current_policy={'enabled':True,'version':1,'groups':['phone'],'categories':[]},
                            wanted_policy={'version':1,'enabled':False,'targets':[]})==1
    with store.box.connect() as db:
        states=dict(db.execute('SELECT source,status FROM marketplace_posts').fetchall())
    assert states=={'v2ex':'pending_analysis','chiphell':'queued'}
    assert enrich.summary(store)=='复杂帖待分析 1，AI 报价待分析 0，待核对 0'


def test_marketplace_requests_bounded_read_only_structured_analysis(monkeypatch):
    from pylon.tasks.software import monitor
    calls=[]
    def request(prompt,**kwargs):
        calls.append(kwargs)
        return {},'fixture-request'
    monkeypatch.setattr(monitor,'_request_grok_structured',request)
    monkeypatch.setattr(monitor,'extract_grok_json',lambda *_,**__:answer())
    items,request_id=enrich.request(example())
    assert len(items)==4 and request_id=='fixture-request'
    assert calls[0]['schema'] is enrich.SCHEMA
    assert calls[0]['timeout']==20 and calls[0]['total_timeout']==120


def test_marketplace_grok_total_deadline_closes_stream(monkeypatch):
    from pylon.tasks.software import monitor
    clock=[0.0];captured={}
    monkeypatch.setattr(monitor.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(monitor,'read_env',lambda _:{'GROK_MODELS_BASE_URL':'https://example.invalid',
                                                 'XAI_API_KEY':'fixture-not-a-credential'})
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):captured['closed']=True
        def iter_lines(self):
            clock[0]=121.0
            yield 'data: {}'
    class Client:
        def __init__(self,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def stream(self,*args,**kwargs):
            captured['payload']=kwargs['json']
            return Response()
    monkeypatch.setattr(monitor.httpx,'Client',Client)
    with pytest.raises(monitor.GrokDigestError,match='total deadline'):
        monitor._request_grok_structured('fixture',schema=enrich.SCHEMA,name='marketplace_items',
                                         timeout=20,total_timeout=120)
    assert captured['closed']
    assert captured['payload']['store'] is False and captured['payload']['tools']==[]
    assert captured['payload']['reasoning']['effort']=='medium'


def narrative():
    post=dict(source='chiphell',source_id='45',title='成色一般的 iQOO11 12+256G 1543 附言',
              body='用过的主力手机。\n挂一个 1543 试试。要盒子+45，90W充电器自己留着。\n可以邮寄。',
              body_complete=True,url='https://example.invalid/t/45',history=0)
    value={'items':[dict(name='iQOO11',category='phone.smartphone',start_line=0,end_line=2,
                         price=1543,price_line=2,status='available',link_ids=[])],
           'common_lines':[3],'shared_link_ids':[]}
    return post,value


def test_marketplace_narrative_price_uses_grok_without_changing_addon_into_price():
    post,value=narrative()
    local=classifier.classify_listing(post['title'],post['body'],post['source'])
    assert local[0]['price'] is None and enrich.needs_analysis(post,local)
    item=enrich.validate(value,post)[0]
    assert item['price']==1543 and item['status']=='available'
    assert item['attributes']['condition']!='全新'
    candidates=classifier.price_analysis(post['body']).candidates
    assert any(c.amount==45 and c.reason=='optional_surcharge' for c in candidates)
    value['items'][0]['price']=45
    with pytest.raises(enrich.AnalysisError,match='price_without_evidence'):enrich.validate(value,post)


@pytest.mark.parametrize('body',[
    '官网价1543元，要盒子+45','买入1543元，电池90%','型号1543，90W充电器',
])
def test_marketplace_grok_cannot_reinterpret_excluded_price_roles(body):
    post,value=narrative();post['body']=body
    value['items'][0].update(end_line=1,price_line=1)
    value['common_lines']=[]
    with pytest.raises(enrich.AnalysisError,match='price_without_evidence'):enrich.validate(value,post)


def test_marketplace_grok_prompt_removes_wx_contact():
    post,_=narrative();post['body']+='\nwx: fixture_contact'
    assert 'fixture_contact' not in enrich.build_prompt(post)


@pytest.mark.parametrize('label', ['电话手表', '儿童手表', '儿童智能手表'])
def test_marketplace_watch_subject_owns_its_model_and_price(label):
    post=dict(source='chiphell',source_id='46',title=label+' Z8 白色',
              body='平时使用不多，配件齐全。\n定价1567，走平台验货。',
              body_complete=True,url='https://example.invalid/t/46',history=0)
    items=classifier.classify_listing(post['title'],post['body'],post['source'])
    assert len(items)==1 and items[0]['category']=='wearable.child_watch'
    assert items[0]['price']==1567 and not enrich.needs_analysis(post,items)
    value={'items':[dict(name=label+' Z8',category='wearable.child_watch',start_line=0,end_line=2,
                        price=1567,price_line=2,status='available',link_ids=[])],
           'common_lines':[],'shared_link_ids':[]}
    assert enrich.validate(value,post)[0]['price']==1567
    # A contradictory provider category still fails source verification.
    value['items'][0]['category']='camera.body'
    with pytest.raises(enrich.AnalysisError,match='unsupported_category'):
        enrich.validate(value,post)


def test_marketplace_contact_redaction_preserves_product_nouns_and_model_names():
    post=example();post['title']='电话手表 Z8、电话卡、WX-1000 耳机、QQ音乐会员'
    post['body']='电话: 12345678901\n微信fixture_contact\nVX: sample_handle\nQQ123456\nWXfixture_direct\nV: fixture_short\n电话手表可邮寄'
    prompt=enrich.build_prompt(post)
    for term in ['电话手表 Z8','电话卡','WX-1000','QQ音乐会员','电话手表可邮寄']:
        assert term in prompt
    for term in ['12345678901','fixture_contact','sample_handle','123456','fixture_direct','fixture_short']:
        assert term not in prompt


def test_marketplace_watch_accessory_cannot_borrow_watch_classification():
    assert classifier.category_for('电话手表 Z8 保护壳，售价29元')[0]=='accessory.phone_case'
    assert classifier.category_for('尼康 Z8 相机，售价9000元')[0]=='camera.body'


@pytest.mark.parametrize('heading,name,category',[
    ('出售会员','88VIP会员','virtual.retail_membership'),
    ('出耳机','AirPods 4','audio.tws'),
    ('出内存','笔记本内存','component.memory_laptop'),
    ('出固态硬盘','NVMe SSD','storage.ssd_nvme'),
])
def test_marketplace_generic_heading_can_refine_from_owned_subject(heading,name,category):
    post=dict(source='nga',source_id='47',title=heading,body=name+'，成色见图。\n售价345元。',
              body_complete=True,url='https://example.invalid/t/47',history=0)
    value={'items':[dict(name=name,category=category,start_line=0,end_line=2,
                        price=345,price_line=2,status='available',link_ids=[])],
           'common_lines':[],'shared_link_ids':[]}
    item=enrich.validate(value,post)[0]
    assert item['category']==category and item['price']==345
    if category=='virtual.retail_membership':
        assert policy.evaluate([item],{'enabled':True,'categories':['virtual.membership']})==[]


@pytest.mark.parametrize('context',[
    '赠送AirPods 4','不是AirPods 4','兼容AirPods 4','不含AirPods 4','对比AirPods 4',
    'https://example.invalid/AirPods4',
])
def test_marketplace_generic_heading_cannot_refine_from_background_or_gift(context):
    assert not enrich.supported_category('audio.tws','AirPods 4','出耳机\n'+context)


def test_marketplace_specific_subject_cannot_be_replaced_by_other_family():
    assert not enrich.supported_category('phone.smartphone','iPhone 14','出手机壳\n适配iPhone 14')
    assert not enrich.supported_category('camera.body','尼康 Z8','电话手表 Z8\n拍照类似尼康 Z8')


def test_marketplace_single_item_can_own_the_whole_post_link():
    post=dict(source='nga',source_id='48',title='出售会员',body='88VIP会员\n详情 https://example.invalid/item',
              body_complete=True,url='https://example.invalid/t/48',history=0)
    value={'items':[dict(name='88VIP会员',category='virtual.retail_membership',start_line=0,end_line=2,
                        price=None,price_line=None,status='available',link_ids=[])],
           'common_lines':[],'shared_link_ids':['L0']}
    items=enrich.validate(value,post)
    assert items[0]['attributes']['shared_links']==['https://example.invalid/item']
    assert pipeline.format_message(post,items,[]).count('https://example.invalid/item')==1


def test_marketplace_multiple_items_cannot_share_one_items_private_link():
    post=example();post['body']=post['body'].replace('4,123 |','4,123 | https://example.invalid/phone')
    value=answer();value['shared_link_ids']=['L0','L1']
    with pytest.raises(enrich.AnalysisError,match='item_link_marked_shared'):
        enrich.validate(value,post)
