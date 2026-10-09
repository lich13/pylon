"""Price semantics across item formats, platforms and specification noise."""
import pytest

from pylon.shared.sale_price import analyse_price
from pylon.tasks.marketplace import classifier, pipeline, policy
from pylon.tasks.quotes import matching


@pytest.mark.parametrize('source', ['v2ex', 'chiphell', 'nga'])
@pytest.mark.parametrize('title,body,price', [
    ('上海iPhone16promax白256国行 6200', '支持邮寄，交易走平台。', 6200),
    ('iPhone 16 Pro Max 256 国行 6200', '电池健康100%，循环16次，2025年购入。', 6200),
    ('iPhone 16 Pro Max 256 国行 电池100% 循环16 6200', '支持邮寄。', 6200),
    ('iPhone 16 Pro Max 256 国行 6200可小刀', '', 6200),
    ('iPhone 16 Pro Max 256 国行', '6200', 6200),
    ('iPhone 16 Pro Max 256 国行', '价格：6,200元，原价9999元。', 6200),
    ('iPad Air', 'iPad Air 2000元，支持邮寄。', 2000),
    ('iPhone 16 Pro Max 256 国行', '明盘６２００元，电池100%。', 6200),
    ('红米音箱 150', '官网199元。', 150),
    ('红米音箱 128', '官网199元。', 128),
    ('iPhone 16 Pro Max 256 国行', '明盘6200元，官网现价7999元。', 6200),
    ('iPhone 16 Pro Max 256 国行', '6200到付出，运费20元，定金50元。', 6200),
    ('iPhone 16 Pro Max 256 国行 6200', '补充：再刀到6000，邮费20元。', 6000),
    ('iPhone 16 Pro Max 256 国行', '售价0.62w，支持邮寄。', 6200),
    ('iPhone 16 Pro Max 256 国行', '售价6.2k，支持邮寄。', 6200),
    ('iPhone 16 Pro Max 256 国行', 'RMB6200，支持邮寄。', 6200),
    ('iPhone 16 Pro Max 256 国行', '人民币6200，支持邮寄。', 6200),
    ('出机械键盘', '售价89.9元，原价199元。', 89.9),
    ('儿童电话手表 Z8', '定价1567，走平台验货。', 1567),
    ('出耳机', '官网定价2399，我定价1567。', 1567),
])
def test_item_context_identifies_prices_without_sale_keywords(source,title,body,price):
    items=classifier.classify_listing(title,body,source,True)
    assert len(items)==1
    assert items[0]['price']==price
    assert items[0]['status']=='available'
    assert items[0]['attributes']['price_status']=='identified'


@pytest.mark.parametrize('text', [
    'iPhone 16 Pro Max 256 国行',
    'iPhone 16 Pro Max 256 国行，电池100%，循环16次',
    'iPhone 16 Pro Max 256 国行，2025年买入，保修至2027年',
    'iPhone 16 Pro Max 256 国行，电话12345678901，QQ123456',
    'iPhone 16 Pro Max 256 国行，IMEI123456789012345',
    'iPhone 16 Pro Max 256 国行，官网价格7999元',
    'iPhone 16 Pro Max 256 国行，200元定金',
    '出 4K 显示器，165Hz，27英寸',
    '出 65W 充电器',
    '出 i7 14700，RTX4090，DDR5 32GB',
    '出 Ryzen 7 7800X3D',
    '出 iPhone14 8+128，序列号88888',
    '出CPU，主频3.7',
    '出键盘，重量300',
    'MacBook Air 2020',
    'iPad Pro 2021',
    '出耳机001234',
    '电话手表 Z8，官方定价2399',
    '出耳机，原定价2399',
    '出耳机，厂商定价2399',
])
def test_only_specifications_reference_prices_and_metadata_are_not_sale_prices(text):
    assert classifier.asking_price(text) is None


@pytest.mark.parametrize('source', ['v2ex','chiphell','nga'])
def test_prices_belong_to_items_even_with_unlabelled_specifications(source):
    items=classifier.classify_listing('两台手机分别出售',
        '1. iPhone 16 Pro Max 256 国行 6200\n2. iPhone 14 128 国行 2200',source,True)
    assert [i['price'] for i in items]==[6200,2200]


@pytest.mark.parametrize('text', [
    'iPhone 16 Pro Max 256，售价6200元或6000元',
    'iPhone 16 Pro Max 256 6200，另一个套餐6500元',
])
def test_conflicting_sale_prices_remain_ambiguous(text):
    result=classifier.price_analysis(text)
    assert result.amount is None and result.status=='ambiguous'
    assert result.reason=='multiple_sale_prices'


def test_ambiguous_notification_is_distinct_from_unstated_price():
    body='售价6200元或6000元'
    items=classifier.classify_listing('出iPhone 16 Pro Max 256',body,'chiphell',True)
    message=pipeline.format_message({'source':'chiphell','title':'出iPhone 16 Pro Max 256',
        'body':body,'body_complete':True,'url':'https://example.invalid/post'},items,[])
    assert '售价有歧义' in message and '价格未标' not in message


def test_bare_price_does_not_weaken_strict_priority_evidence():
    text='iPhone 16 Pro Max 256 国行 6200'
    assert classifier.asking_price(text)==6200
    assert matching.asking_price(text) is None


@pytest.mark.parametrize('capacity', [64,128,256,512,1024])
@pytest.mark.parametrize('price', [2100,3400,6200,8900])
def test_changing_capacity_does_not_change_seller_price(capacity,price):
    # Variants exercise the semantic boundary, not one reported title.
    text=f'iPhone 16 Pro Max {capacity} 国行 {price}'
    assert classifier.asking_price(text)==price


def test_history_replay_never_enqueues_even_after_reclassification(tmp_path):
    from pylon.outbox import Outbox
    from pylon.tasks.marketplace.store import Store
    store=Store(Outbox(tmp_path/'history.sqlite3'))
    store.ingest('chiphell',[{'id':'123','url':'https://example.invalid/post',
        'title':'iPhone 16 Pro Max 256 国行 6200','body_full':'支持邮寄','body_complete':True}],history=True)
    assert pipeline.process(store,current_policy={'version':1,'enabled':True,'groups':['phone'],'categories':[]},
                            wanted_policy={'version':1,'enabled':False,'rules':[]})==0
    with store.box.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0


def test_currency_price_after_model_family_is_not_a_generation():
    assert classifier.asking_price('出 iPhone 15 Pro 256G，3600元，iPad Air 2000元') is None
    assert classifier.asking_price('iPad Air 2000元') == 2000
    assert classifier.asking_price('RTX4090元件') is None


@pytest.mark.parametrize('reference', ['官网现价9999元', '原价9999元', '购入价9999元'])
@pytest.mark.parametrize('position', ['before', 'after'])
def test_reference_order_does_not_change_actual_price(reference,position):
    clauses=[reference,'明盘6200元'] if position=='before' else ['明盘6200元',reference]
    assert classifier.asking_price('iPhone 16 Pro Max 256 国行，'+'，'.join(clauses)) == 6200


def test_comma_between_independent_amounts_is_not_a_thousands_separator():
    result=classifier.price_analysis('出耳机，售价3200,500元')
    assert result.amount is None and result.status=='ambiguous'


@pytest.mark.parametrize('text,expected', [
    ('iPhone 16 Pro Max 256，明盘6200元 原价9999元', 6200),
    ('iPhone 16 Pro Max 256，6200元 原价9999元', 6200),
    ('iPhone 16 Pro Max 256，明盘6200元 200元定金', 6200),
    ('iPhone 16 Pro Max 256，原价9999元 明盘6200元 20元邮费', 6200),
    ('iPhone 16 Pro Max 256，9999元买的', None),
])
def test_role_labels_bind_their_own_amounts(text,expected):
    assert classifier.asking_price(text)==expected


@pytest.mark.parametrize('text,expected', [
    ('DDR5 32GB 6000', None),
    ('DDR5 32GB 6000 300', 300),
    ('DDR5 5600 16Gx2 2700', 2700),
    ('DDR5 32GB 2700', 2700),
    ('DDR5 32GB 6000，售价800', 800),
    ('出 ATK 鼠标，JD售价199元，卖价150元', 150),
    ('红米音箱2', None),
    ('红米音箱2 150', 150),
    ('京东家政：50', 50),
])
def test_subject_numbers_need_a_price_boundary(text,expected):
    assert classifier.asking_price(text)==expected
