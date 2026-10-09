"""Stable facet comparison, independent of product category recognition."""
import re
import unicodedata

ALIASES = {
    'brand': {'apple':'苹果','huawei':'华为','xiaomi':'小米','samsung':'三星',
              'honor':'荣耀','sony':'索尼','lenovo':'联想','asus':'华硕','nvidia':'英伟达'},
    'product': {'苹果音乐':'applemusic'},
}


def normalize(value, field=''):
    result=re.sub(r'[\s_‐‑–—-]+','',unicodedata.normalize('NFKC',str(value))).casefold()
    return ALIASES.get(field,{}).get(result,result)


def text_matches(actual, expected, field):
    actual,expected=normalize(actual,field),normalize(expected,field)
    if not expected:return False
    if field not in {'keywords','product'}:return actual==expected
    # Short Latin words must not match unrelated model strings (SE in Bose).
    if expected.isascii() and expected.isalpha() and len(expected)<4:
        return bool(re.search(r'(?<![a-z0-9])'+re.escape(expected)+r'(?![a-z0-9])',actual))
    return expected in actual
