"""Conservative, evidence-based matching of one listing to one quote."""
from __future__ import annotations

import re
from typing import Any

from . import arbitrage as catalog

UNAVAILABLE = re.compile(r"已售|已出(?!售中)|售出|售罄|已预[订定]|已被预[订定]|\d+楼预[订定]|预[订定]了|封贴|结贴|勿拍|求购|想收|收一个|收个|求收|打包价|打包出|一起出|合计", re.I)
USED = re.compile(r"几乎全新|接近全新|近乎全新|充新|充全新|靓机|[九八七六五四三二一\d][成\.\d]*新|成色如图|二手自用|自用机|使用全程|全程(?:带|戴)壳|(?:买来|一直)贴膜|过保|电池(?:健康|寿命|手机显示|效率)?\s*[:：]?\s*(?:[1-9]\d|100)\s*%?|循环\s*\d+|激活.*?保修", re.I)
ACCESSORY = re.compile(r"(?:原盒原线|原装线|原线|数据线|充电线|线材|充电器|充电头|包装|原盒|盒子|配件|手机壳|保护壳)[^。；;，,\n]{0,35}(?:全新|未拆(?:封)?|未开封|未使用|未用|原封)", re.I)
NON_SALE_PRICE = re.compile(r"原价|官网|官方价|购入|买入|入手|买时|到手价|购价|购买价|收购价|回收价|报价|预算|定金|订金|押金|补贴|优惠券|省了|便宜了|立减|直降|降了|运费|邮费|维修|换屏|电池更换|首付|每月|分期|微信|QQ|电话|联系|编号|信用|IMEI|序列号", re.I)
SALE_PRICE = re.compile(r"明盘|现价|售价|卖价|价格|挂价|自刀到|降至|降到|现在|现出|现卖|出价", re.I)
MULTI = re.compile(r"另有|另出|另外|同时出|再出|第二台|两台|两部|两件|多台|多部|多件|各一台|价格分别|价格内详", re.I)
MONEY = re.compile(r"(?<![\dA-Za-z+])(?P<prefix>[¥￥])?\s*(?P<n>\d{2,5}(?:\.\d+)?|\d(?:\.\d+)?)(?P<unit>\s*[万wWkK])?\s*(?P<suffix>元|块|RMB|人民币)?(?![\dA-Za-z+%％]|\s*[GgTt款年代])", re.I)


def newness_evidence(text: str) -> str:
    cleaned = ACCESSORY.sub(" ", text)
    if USED.search(cleaned) or not catalog.has_device_newness(cleaned):
        return ""
    match = catalog.NEW_RE.search(cleaned)
    return cleaned[max(0, match.start() - 16):min(len(cleaned), match.end() + 16)].strip() if match else ""


def listing_blocks(title: str, body: str) -> list[str]:
    # Only separate clearly delimited listings. A later item's condition/price
    # must never inherit an earlier item's title or condition.
    body = re.sub(r"(?:另有|另出|另外出售|同时出|再出)", "\n\n", body)
    parts = [p.strip() for p in re.split(r"\n[ \t]*\n|\n(?=\s*\d+[.、）)]\s*)", body) if p.strip()]
    if len(parts) > 1 and asking_price(parts[0]) and any(catalog.NEW_RE.search(p) and asking_price(p) for p in parts[1:]):
        return [f"{title}\n{parts[0]}", *parts[1:]]
    return [f"{title}\n{body}"]


def asking_price(text: str) -> tuple[int, str] | None:
    """A unique sale amount with local evidence; never the lowest number."""
    text = catalog.strip_non_price_numbers(text)
    text = re.sub(r"https?://\S+", " ", text)
    prices: dict[int, str] = {}
    for line in re.split(r"[\n。；;，,]", text):
        matches = list(MONEY.finditer(line))
        for match in matches:
            before = line[:match.start()]
            after = line[match.end():]
            # A role label applies up to the next explicit sale-price label.
            negative = list(NON_SALE_PRICE.finditer(before))
            positive = list(SALE_PRICE.finditer(before))
            if negative and (not positive or negative[-1].start() >= positive[-1].start()):
                continue
            unit = (match['unit'] or '').strip().lower()
            amount = int(float(match['n']) * (10000 if unit in {'w', '万'} else 1000 if unit == 'k' else 1))
            if not 100 <= amount <= 50000:
                continue
            explicit = bool(match['prefix'] or match['suffix'] or unit or positive
                            or re.match(r"\s*(?:出|包邮|包顺丰|顺丰|到付|不刀|可刀)", after))
            # Bare title prices are allowed only at the end of an actual sale
            # title with a device and explicit condition, not arbitrary IDs.
            if not explicit and not (len(matches) == 1 and catalog.NEW_RE.search(before)
                                     and not after.strip() and catalog.SALE_RE.search(before)):
                continue
            prices[amount] = line.strip()[:160]
    return next(iter(prices.items())) if len(prices) == 1 else None


def _dimensions(text: str, category: str) -> dict[str, set[str]]:
    norm = catalog.normalize_match_text(text)
    if category == 'phone':
        norm = re.sub(r'(\d{1,2})MAX', r'\1PROMAX', norm)
    dims: dict[str, set[str]] = {}
    dims['capacity'] = set(catalog.capacity_tokens(text))
    dims['edition'] = {t for t in ('PROMAX', 'PLUS', 'ULTRA', 'PRO', 'AIR', 'MINI', 'SE', 'RS', '非凡大师', '典藏', '柔光', '活力', '插卡')
                       if t in norm}
    # Pro Max contains Pro; do not treat that as a separate variant.
    if 'PROMAX' in dims['edition']:
        dims['edition'].discard('PRO')
    dims['region'] = {label for label, pattern in (
        ('CN', r'国行|大陆版'), ('US', r'美版|美行'), ('HK', r'港版|港行'),
        ('JP', r'日版|日行'), ('GLOBAL', r'海外版|国际版')) if re.search(pattern, text)}
    if category in {'watch', 'tablet', 'laptop'}:
        dims['network'] = {t for t in ('GPS', 'WIFI', 'CELLULAR') if t in norm}
        dims['size'] = set(re.findall(r'(\d{1,2}(?:\.\d+)?)\s*(?:mm|毫米|英寸|寸)', text, re.I))
        if category == 'tablet':
            dims['size'].update(re.findall(r'(\d{1,2})INCH', norm))
        if category == 'watch':
            dims['size'].update(re.findall(r'(?<!\d)(3[89]|4[0-9])(?=\s*(?:[-－]|GPS|蜂窝|铁|钛|铝|金属|运动|米兰|回环|野径|海洋))', text, re.I))
            dims['material'] = {t for t in ('钛金属', '铁金属', '铝金属', '不锈钢', '陶瓷') if t in text}
            dims['band'] = {t for t in ('米兰尼斯', '回环', '野径', '海洋', '运动版') if t in text}
            dims['generation'] = set(re.findall(r'(?:\bS|Series\s*)(\d{1,2})(?!\d)', text, re.I))
    if category in {'earbuds', 'accessory'}:
        dims['connector'] = {t for t in ('USBC', 'LIGHTNING') if t in norm}
        dims['noise'] = {'ANC'} if re.search(r'降噪|ANC', norm) else {'STANDARD'} if '标准' in norm else set()
    dims['bundle'] = {t for t in ('套装', '全能', '单机', '畅飞', '教育', '一拖一', '一拖二') if t in text}
    dims['year'] = set(re.findall(r'(20\d{2})\s*款', text))
    return dims


def _category_evidence(text: str, category: str) -> bool:
    patterns = {
        'phone': r'iPhone|手机|Mate(?!Pad)|Pura|小米\s*\d|红米|Redmi|Galaxy|三星\s*S\d|OPPO|VIVO|IQOO|荣耀|Honor|一加|OnePlus|摩托罗拉|魅族|8848',
        'watch': r'Watch|手表', 'band': r'Band|手环', 'tablet': r'iPad|MatePad|平板',
        'earbuds': r'AirPods|FreeBuds|Buds|耳机|耳塞',
        'laptop': r'MacBook|MateBook|MagicBook|RedmiBook|ThinkPad|Surface|笔记本|电脑',
        'drone': r'大疆|DJI|无人机', 'camera': r'影石|Insta360|GoPro|相机',
        'console': r'任天堂|Switch|NS2|PS5|PlayStation',
        'accessory': r'Pencil|手写笔|触控笔|键盘|鼠标|充电|数据线|保护壳',
    }
    return bool(re.search(patterns.get(category, r'(?!)'), text, re.I))


def strict_quote_matches(quote: Any, text: str) -> bool:
    norm = catalog.normalize_match_text(text)
    if not catalog.quote_matches_text(quote, norm, raw_text=text):
        return False
    category = catalog.quote_category(quote)
    if not _category_evidence(text, category):
        return False
    spec = str(quote['model_spec'])
    expected, actual = _dimensions(spec, category), _dimensions(text, category)
    # Every explicitly stated differentiating dimension must agree. Missing
    # material/network/bundle is not permission to use a premium variant.
    for dimension in expected.keys() | actual.keys():
        left, right = expected.get(dimension, set()), actual.get(dimension, set())
        if dimension == 'region' and right == {'CN'} and not left:
            continue  # This domestic quote sheet omits 国行 on ordinary rows.
        if left != right:
            return False
    # All identity anchors must match, not merely one generic WATCH/BUDS token.
    anchors = {a for a in catalog.model_anchor_tokens(spec) if a not in {'WATCH', 'BUDS', 'PAD'}}
    if category == 'watch':
        anchors = {a for a in anchors if not re.fullmatch(r'S\d+|WATCH[A-Z]*', a)}
    if not anchors.issubset(set(catalog.model_anchor_tokens(text))):
        return False
    # Prevent base iPhone/Xiaomi models accepting another generation.
    if str(quote['brand']) in {'苹果', '小米'} and category == 'phone':
        spec_gen = re.search(r'(?<!\d)(\d{1,2})(?=\D|$)', spec)
        if spec_gen and not re.search(rf'(?:IPHONE|苹果|小米|XIAOMI){spec_gen[1]}(?!\d)', norm):
            return False
    return True


def select_match(quotes: list[Any], title: str, body: str, *, assume_sale: bool = False) -> dict[str, Any] | None:
    from .arbitrage import is_excluded_device_deal
    if '正文抓取失败' in body or body.rstrip().endswith(('…', '...')):
        return None
    if not body.strip():
        # Chiphell treats --body-limit as its soft snippet size; its full body
        # can be read from the full post. V2EX explicitly marks incomplete fetches.
        body = title
    matches = []
    blocks = listing_blocks(title, body)
    for text in blocks:
        if UNAVAILABLE.search(text) or MULTI.search(text) or is_excluded_device_deal(text):
            continue
        condition = newness_evidence(text)
        if not condition or (not assume_sale and not catalog.SALE_RE.search(text)):
            continue
        price = asking_price(text)
        if not price:
            continue
        candidates = [q for q in quotes if strict_quote_matches(q, text)]
        # Color may be omitted only when it cannot change the quote value.
        colors = {c for c in catalog.COLOR_TOKENS if c in re.sub(r'金属|顺丰|包邮', '', catalog.normalize_match_text(text))}
        if not colors and len({int(q['quote_price']) for q in candidates}) > 1:
            continue
        identities = {(str(q['brand']), catalog.normalize_match_text(str(q['model_spec']))) for q in candidates}
        if len(identities) != 1:
            continue
        quote = min(candidates, key=lambda q: (int(q['quote_price']), str(q['sku_key'])))
        sale, evidence = price
        reference = int(quote['quote_price'])
        if not reference * .35 <= sale < reference:
            continue
        matches.append(dict(product_name=quote['product_name'], quote_price=reference, sale_price=sale,
                            profit=reference-sale, sku_key=quote['sku_key'],
                            condition_evidence=condition, price_evidence=evidence))
    # Multiple independently verified listings are safe; retain the existing
    # one-alert-per-post interface without using one item's price for another.
    return max(matches, key=lambda m: m['profit']) if matches else None
