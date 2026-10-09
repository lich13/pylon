"""Sale-price formats use one parser while keeping item and price roles separate."""
import pytest

from pylon.tasks.marketplace import classifier
from pylon.tasks.marketplace.pipeline import format_message, _quote_matches
from pylon.tasks.quotes import matching


@pytest.mark.parametrize('text,expected', [
    ('出 iPhone 15 Pro 256G，再刀3600到付出', 3600),
    ('出 iPhone 15 Pro 256G，3600到付出', 3600),
    ('出 iPhone 15 Pro 256G，3600出', 3600),
    ('出 iPhone 15 Pro 256G，明盘3600', 3600),
    ('出 iPhone 15 Pro 256G，一口价3600', 3600),
    ('出 iPhone 15 Pro 256G，现价3.6k', 3600),
    ('出 iPhone 15 Pro 256G，售价0.36万', 3600),
    ('出 iPhone 15 Pro 256G，原价6000元 现价3600元', 3600),
    ('出 iPhone 15 Pro 256G，原价6000元，现在3600到付', 3600),
    ('出个红米音箱，官网现价199元，明盘70元', 70),
    ('出个红米音箱，官网售价199元，明盘70元', 70),
    ('出个红米音箱，京东现在价格199元，明盘70元', 70),
    ('出个红米音箱，官方价格199元，卖价70元', 70),
    ('出个红米音箱，官网现价199元 明盘70元', 70),
    ('出个红米音箱，官网199元 现价70元', 70),
    ('出个红米音箱，官网价太高我卖价70元', 70),
    ('出个红米音箱，官网现价199元', None),
    ('出个红米音箱，市场价199元，明盘70元', 70),
    ('出个红米音箱，参考价199元，明盘70元', 70),
    ('出个红米音箱，明盘70元，运费10元', 70),
    ('出个红米音箱，明盘70元，另一种套餐90元', None),
    ('出 iPhone 15 Pro 256G\n3600', 3600),
    ('出北京京东家政50', 50),
    ('【二手交易】出北京京东家政50', 50),
    ('出北京京东家政2小时，售价50', 50),
    ('出 iPhone 15 Pro 256G，电池健康90%，3600到付出，运费20元', 3600),
    ('出 iPhone 15 Pro 256G，明盘3600元，定金200元', 3600),
    ('出 iPhone 15 Pro 256G，原价6000元', None),
    ('出 iPhone 15 Pro 256G，预算3600元', None),
    ('出 iPhone 15 Pro 256G，运费20元', None),
    ('出 iPhone 15 Pro 256G，定金200元', None),
    ('出 iPhone 15 Pro 256G，电池90%，2025年购入', None),
    ('出 iPhone14 128', None),
    ('出小米15', None),
    ('出 i7 14700', None),
    ('出 Ryzen 7 7800', None),
    ('出 iPhone 15 Pro 256G\n发表于2026-10-09 10:30', None),
    ('出 iPhone 15 Pro 256G，联系方式12345678901', None),
    ('出 iPhone 15 Pro 256G，3600元或3800元', None),
    ('出 iPhone 15 Pro 256G，3600元，iPad Air 2000元', None),
])
def test_normal_price_formats_and_non_price_numbers(text, expected):
    assert classifier.asking_price(text) == expected


@pytest.mark.parametrize('ending', ['3600到付出','3600出','明盘3600','一口价3600','现价3.6k'])
def test_normal_and_strict_prices_agree_on_explicit_sale(ending):
    text = '出 iPhone 15 Pro 256G 全新未激活，'+ending
    strict = matching.asking_price(text)
    assert strict and strict[0] == classifier.asking_price(text) == 3600


def test_normal_low_value_service_and_bare_price_do_not_relax_priority():
    assert classifier.asking_price('出北京京东家政50') == 50
    assert matching.asking_price('出北京京东家政50') is None
    assert classifier.asking_price('出 iPhone 15 Pro 256G\n3600') == 3600
    assert matching.asking_price('出 iPhone 15 Pro 256G\n3600') is None


@pytest.mark.parametrize('source', ['v2ex','chiphell','nga'])
def test_multi_item_bare_and_delivery_prices_stay_with_each_item(source):
    items = classifier.classify_listing('两件闲置分别出售',
        '1. 出 iPhone 15 Pro 256G\n3600\n2. 出 iPhone 14 128G，2200到付出',source,True)
    assert [i['price'] for i in items] == [3600,2200]


def test_external_link_without_text_price_is_honest_and_never_prioritised():
    body = 'MacBook Air M2 出售，价格详见闲鱼链接 https://example.invalid/item'
    item = classifier.classify_listing('出 MacBook Air M2',body,'nga',True)[0]
    assert item['price'] is None
    assert _quote_matches([item],True,[]) == []
    post = {'source':'nga','title':'出 MacBook Air M2','body':body,'body_complete':True,
            'url':'https://example.invalid/post'}
    message = format_message(post,[item],[])
    assert '正文未标价，见原帖商品链接' in message
    assert '0 元' not in message and '重点比价' not in message


@pytest.mark.parametrize('subject,category', [
    ('红米音箱','audio.speaker'), ('Redmi音箱','audio.speaker'),
    ('redmi音箱','audio.speaker'), ('oppo耳机','audio.headphone'),
    ('红米小爱智能音箱','audio.portable'), ('OPPO耳机','audio.headphone'),
    ('荣耀路由器','network.router'), ('红米K70手机','phone.smartphone'),
    ('荣耀Magic7手机','phone.smartphone'),
])
def test_brand_does_not_override_the_product_type(subject,category):
    items = classifier.classify_listing('出个'+subject, '官网现价199元，明盘70元','v2ex',True)
    assert len(items)==1 and items[0]['category']==category and items[0]['price']==70


def test_redmi_speaker_has_a_price_but_does_not_hit_phone_watch_list():
    from pylon.tasks.marketplace import policy
    items = classifier.classify_listing('【二手交易】出个红米音箱',
        '官网现价199元，明盘70元','v2ex',True)
    assert items[0]['category']=='audio.speaker' and items[0]['price']==70
    assert policy.evaluate(items,{'enabled':True,'groups':['phone'],'categories':[]})==[]
    assert policy.evaluate(items,{'enabled':True,'categories':['audio.speaker']})==items


def test_reference_price_does_not_block_strict_quote_sale_price():
    text='出 iPhone 15 Pro 256G 全新未激活，官网现价6999元，明盘3600元'
    assert matching.asking_price(text)[0]==classifier.asking_price(text)==3600
