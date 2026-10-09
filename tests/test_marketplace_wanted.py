from unittest.mock import patch

import pytest

from pylon.outbox import Outbox
from pylon.tasks.marketplace import classifier, policy, wanted
from pylon.tasks.marketplace.pipeline import format_message, process, _quote_matches
from pylon.tasks.marketplace.store import Store


def items(title, body=''):
    return classifier.classify_listing(title,body,'v2ex',True)


def inventory(model='iPhone 15 Pro', category='phone.smartphone', **fields):
    return wanted.validate({'version':1,'enabled':True,'targets':[
        {'category':category,'model':model,**fields}]})


@pytest.mark.parametrize('title',[
    '【二手交易】北京 65 收 2 小时京东家政（长期）',
    '北京65收2小时京东家政','65元收北京京东家政2小时',
    '北京 京东家政 2小时，65块收','北京京东家政2小时长期收，预算65元',
])
def test_wanted_budget_before_verb_and_long_term_requests(title):
    requests=items(title,'长期收')
    assert len(requests)==1 and requests[0]['status']=='buy'
    assert requests[0]['attributes']['intent']=='wanted' and requests[0]['price']==65
    assert wanted.evaluate(requests,inventory('京东家政','virtual.service',region='北京',specs={'hours':2}))==requests
    assert not policy.evaluate(requests,{'enabled':True,'groups':['virtual'],'categories':[]})


@pytest.mark.parametrize('suffix',['65元收货前确认','65元收款后发货','65元，支持验收','65元，附送收纳盒'])
def test_wanted_verb_does_not_capture_receipt_or_payment(suffix):
    requests=items('出售 iPhone 15 Pro',suffix)
    assert all(i['status']!='buy' and i['attributes']['intent']!='wanted' for i in requests)


def test_wanted_budget_does_not_change_service_city_or_duration():
    selected=inventory('京东家政','virtual.service',region='北京',specs={'hours':2})
    assert not wanted.evaluate(items('上海65收2小时京东家政','长期收'),selected)
    assert not wanted.evaluate(items('北京65收4小时京东家政','长期收'),selected)


def test_wanted_inventory_is_independent_from_sales_and_not_enabled_by_default(tmp_path):
    requests=items('求购 iPhone 15 Pro','预算4500元')
    assert not wanted.evaluate(requests,wanted.load(tmp_path/'missing.json'))
    assert not policy.evaluate(requests,{'enabled':True,'groups':['phone']})
    assert wanted.evaluate(requests,inventory())==requests
    assert not wanted.evaluate(items('出售 iPhone 15 Pro','4800元包邮'),inventory())


@pytest.mark.parametrize('title',[
    '求购 iPhone 15 Pro Max','求购 iPhone 15','求购 iPhone 16 Pro',
    'iPhone 15 Pro 询价','求推荐 iPhone 15 Pro','[已收] 求购 iPhone 15 Pro',
])
def test_other_models_inquiries_and_finished_requests_are_not_matches(title):
    assert not wanted.evaluate(items(title),inventory())


def test_specifications_are_inventory_requirements_not_a_category_subscription():
    selected=inventory(specs={'storage_gb':256})
    assert wanted.evaluate(items('求购 iPhone 15 Pro 256G','预算4500元'),selected)
    assert not wanted.evaluate(items('求购 iPhone 15 Pro 128G','预算4500元'),selected)
    assert not wanted.evaluate(items('求购 iPhone 15 Pro 256G'),inventory())
    assert wanted.evaluate(items('求购 iPhone 15 Pro','容量不限'),selected)


def test_incomplete_request_cannot_hide_a_required_capacity_or_region():
    incomplete=classifier.classify_listing('求购 iPhone 15 Pro','仅收128G','v2ex',False)
    assert not wanted.evaluate(incomplete,inventory())


def test_beijing_housekeeping_is_a_specific_regional_entitlement():
    selected=inventory('京东家政','virtual.service',region='北京',specs={'hours':2})
    requests=items('求购北京京东家政','收2小时保洁，出价50元')
    assert wanted.evaluate(requests,selected)
    assert requests[0]['price']==50
    assert not wanted.evaluate(items('求购上海京东家政','收2小时保洁，出价50元'),selected)
    assert not wanted.evaluate(items('求购北京京东家政','收4小时保洁，出价50元'),selected)
    assert not wanted.evaluate(items('求购京东家政','出价50元'),selected)
    assert not wanted.evaluate(items('求购北京天鹅到家保洁','出价50元'),selected)


def test_request_specs_distinguish_memory_vram_model_numbers_and_generation():
    assert wanted.descriptor('收 RTX4060Ti 16G','component.gpu')['specs']=={'vram_gb':16}
    assert wanted.descriptor('收 Ryzen 5700X，原来用5600G','component.cpu')['specs']=={}
    assert wanted.descriptor('收拯救者R9000P 16GB','computer.laptop')['specs']=={'ram_gb':16}
    assert wanted.descriptor('收iPad Pro 12.9寸','tablet.tablet') is None
    assert wanted.descriptor('收A16','camera.body') is None
    assert wanted.descriptor('收iPhone15Pro/16Pro','phone.smartphone') is None
    assert wanted.descriptor('收iPad Pro M1 12.9寸蜂窝版','tablet.tablet')['specs']=={'screen_tenths':129,'network':'cellular'}


@pytest.mark.parametrize('target',[
    {'model':'iPhone','category':'phone.smartphone'},
    {'model':'手机','category':'phone.smartphone'},
    {'model':'京东家政','category':'virtual.service'},
    {'model':'iPhone 15 Pro','category':'component.gpu'},
    {'model':'iPhone 15 Pro','category':'phone.smartphone','specs':{'storage_gb':'256'}},
])
def test_broad_or_invalid_inventory_targets_cannot_be_enabled(target):
    with pytest.raises(ValueError):
        wanted.validate({'version':1,'enabled':True,'targets':[target]})


def test_mixed_sale_and_request_are_matched_separately_and_not_price_compared(tmp_path):
    parsed=items('处理两件事情','出售 iPhone 16 Pro 5000元包邮\n求购 iPhone 15 Pro，预算4000元')
    assert {item['status'] for item in parsed}=={'available','buy'}
    hits=wanted.evaluate(parsed,inventory())
    assert len(hits)==1 and hits[0]['price']==4000
    assert _quote_matches(hits,True,[])==[]
    post={'source':'v2ex','title':'处理两件事情','body':'','body_complete':True,'url':'https://example.invalid/t/1'}
    message=format_message(post,hits,[])
    assert '【求购】' in message and '求购出价 4000 元' in message
    assert 'iPhone 16' not in message and '差价' not in message

    store=Store(Outbox(tmp_path/'outbox.sqlite3'))
    store.ingest('v2ex',[{**post,'id':'1','body_full':'求购 iPhone 15 Pro，预算4000元'}])
    sale_policy={'enabled':False,'version':7,'categories':[],'blocked_groups':['phone']}
    with patch('pylon.tasks.quotes.health.inspect',return_value={'available':False}):
        assert process(store,current_policy=sale_policy,wanted_policy=inventory())==1
        assert process(store,current_policy=sale_policy,wanted_policy=inventory())==0
    with store.box.connect() as db:
        assert db.execute('SELECT wanted_policy_version FROM marketplace_posts').fetchone()[0]==1
        assert db.execute("SELECT COUNT(*) FROM events WHERE kind='bark'").fetchone()[0]==0


def test_wanted_own_block_overrides_model_without_changing_sales():
    selected=inventory()
    selected['blocked_groups']=['phone']
    assert not wanted.evaluate(items('求购 iPhone 15 Pro'),selected)
    assert policy.evaluate(items('出售 iPhone 15 Pro','5000元包邮'),{'enabled':True,'groups':['phone']})


def test_catalog_has_model_counts_without_raw_mail_contacts_or_links(tmp_path):
    store=Store(Outbox(tmp_path/'history.sqlite3'))
    for n,title in enumerate(['求购 iPhone 15 Pro','iPhone 15 Pro 求推荐','求购北京京东家政'],1):
        store.ingest('v2ex',[{'id':str(n),'url':f'https://example.invalid/t/{n}',
                            'title':title,'body_full':title,'body_complete':True}],history=True)
    stats=store.stats(fresh=True)
    assert len(stats['wanted_catalog'])==2
    assert {entry['model'] for entry in stats['wanted_catalog']}=={'iPhone 15 Pro','京东家政'}
    assert all(entry['posts']=={'v2ex':1} for entry in stats['wanted_catalog'])
    with store.box.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
