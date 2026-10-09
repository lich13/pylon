"""Contact labels belong to seller text, never random image identifiers."""
import base64

import pytest

from pylon.tasks.quotes.arbitrage import format_contact_note
from pylon.tasks.marketplace import classifier, pipeline


def encoded(text):
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize('text',[
    'ab12wxRandomImage.jpg (12 KB, 下载次数: 0)',
    'VXrandom_attachment.png (22 KB, 下载次数: 0)',
    '微信图片_fixture.png (22 KB, 下载次数: 0)',
    'https://example.invalid/images/wxRandomId.jpg?token=fixture',
    '微信：https://example.invalid/photo.png',
    '微信：fixture_photo.jpg',
    '微信：fixture_photo.jpg（明文）',
    '图片链接 www.example.invalid/wxRandomId.png',
    '出售 WX-1000 耳机，照片见附件。',
])
def test_marketplace_media_never_becomes_a_plain_contact(text):
    assert format_contact_note(text)==''


@pytest.mark.parametrize('value',['fixture_photo.jpg','https://example.invalid/photo.png','not/a/contact'])
def test_marketplace_encoded_media_and_paths_are_not_contacts(value):
    assert format_contact_note('微信 Base64: '+encoded(value))==''
    assert format_contact_note('微信 '+encoded(value))==''


@pytest.mark.parametrize('context',['','有意购买 ','附件编号 ','Base64: '])
def test_marketplace_decoding_requires_explicit_contact_intent(context):
    assert format_contact_note(context+encoded('fixture_handle'))==''


@pytest.mark.parametrize('label',['微信：','VX: ','wx','V: ','联系方式：'])
def test_marketplace_explicit_plain_contact_survives_image_noise(label):
    text='ab12wxRandomImage.jpg (12 KB, 下载次数: 0)\n'+label+'fixture_handle'
    assert format_contact_note(text).endswith('fixture_handle（明文）')
    assert 'RandomImage' not in format_contact_note(text)


@pytest.mark.parametrize('label',['微信 ','微信 Base64: ','VX: ','V: ','微信：\n'])
def test_marketplace_explicit_encoded_contact_is_preserved(label):
    note=format_contact_note(label+encoded('fixture_handle'))
    assert 'fixture_handle' in note and 'Base64 解码' in note


def test_marketplace_contact_context_does_not_cross_an_unrelated_line():
    assert format_contact_note('需要联系卖家\n没有联系账号\n'+encoded('fixture_handle'))==''


def test_marketplace_render_preserves_price_and_trade_url_without_false_contacts():
    post={'source':'chiphell','source_id':'49','title':'出售 iPhone 15 Pro 256G 4123',
          'url':'https://example.invalid/t/49','body_complete':True,
          'body':'可邮寄，商品链接 https://example.invalid/item/49\n'
                 'prefixwxRandomImage.jpg (12 KB, 下载次数: 0)\n2025-01-01 12:00 上传'}
    items=classifier.classify_listing(post['title'],post['body'],'chiphell',True)
    text=pipeline.format_message(post,items,[])
    assert '4123 元' in text and 'https://example.invalid/item/49' in text
    assert '明文' not in text and 'Base64' not in text and '联系方式' not in text
