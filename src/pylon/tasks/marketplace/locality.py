"""Item-scoped delivery and service eligibility for a buyer in Beijing."""
from __future__ import annotations

import re

from pylon.tasks.forums.v2ex import (
    BEIJING_LOCALITY_TERMS, NON_BEIJING_LOCALITY_TERMS, LocalityFacts,
)

REGIONS = re.compile('|'.join(re.escape(x) for x in sorted(
    set(BEIJING_LOCALITY_TERMS + NON_BEIJING_LOCALITY_TERMS + ['帝都', '魔都', '羊城']),
    key=len, reverse=True)))
ALIASES = {'帝都': '北京', '魔都': '上海', '羊城': '广州'}
REGION_LIMIT = re.compile(r'(?<!不)(?:仅限|只限|限于|只出|仅出|仅|限)\s*(?:' + REGIONS.pattern + ')')
PICKUP = re.compile(r'自提|面交|面取|当面|线下交易')
PREFERENCE = re.compile(
    r'(?:本地|同城|自提|面交|面取|当面).{0,8}(?:优先|优选|更好|最好)'
    r'|(?:优先|最好).{0,8}(?:本地|同城|自提|面交|面取|当面)')
OPTIONAL = re.compile(
    r'(?:支持|可以|可|接受|欢迎).{0,8}(?:自提|面交|面取)'
    r'|(?:自提|面交|面取).{0,8}(?:也可|亦可|优惠|减|少|便宜)')
EXCLUSIVE = re.compile(
    r'(?:仅限|只限|限定|限于|只出|仅出|只接受|仅接受|只支持|仅支持|只能|只可|只做)'
    r'.{0,12}(?:本地|同城|自提|面交|面取|当面|线下)'
    r'|(?:仅|限).{0,8}(?:自提|面交|面取)'
    r'|(?:不支持|不接受|不能|无法|不走|不发|不)(?:邮寄|快递|发货)'
    r'|(?:不出|不发|不接|谢绝)(?:外地|异地)'
    r'|(?:外地|异地).{0,6}(?:不出|不发|勿扰)')
SHIPPING = re.compile(r'邮寄|快递|包邮|发货|到付|顺丰')
OFFLINE_SERVICE = re.compile(r'家政|保洁|钟点工|到店|上门|核销|洗车|洗衣|洗鞋|寄洗|维修|护理|餐饮|健身|酒店|门票')
NATIONWIDE = re.compile(r'全国(?:通用|可用|可使用|都可用|服务|上门)|不限(?:城市|地区|地域)|各地通用')
REMOTE_SERVICE = re.compile(r'全国[^，,。；;\n]{0,10}(?:寄洗|寄修|取件|收件)|(?:寄洗|寄修)[^，,。；;\n]{0,8}全国')


def regions(text: str) -> list[str]:
    return list(dict.fromkeys(ALIASES.get(x, x) for x in REGIONS.findall(text)))


def service_regions(text: str, title: str = '') -> list[str]:
    """Usage areas outrank the seller's mailing address or current location."""
    clauses=[c for c in re.split(r'[\n，,。；;！!]',text) if c.strip()]
    explicit=[c for c in clauses if regions(c) and re.search(
        r'可用|使用|适用|核销|服务范围|服务地区|仅限|只限|限于|限定|全国',c)]
    if explicit:return regions('\n'.join(explicit))
    business=[c for c in clauses if regions(c) and OFFLINE_SERVICE.search(c)
              and not re.search(r'卖家|本人在|人在|所在地|发货地',c)]
    if business:return regions('\n'.join(business))
    remaining=[c for c in clauses if not re.search(r'卖家|本人在|人在|所在地|发货地',c)]
    return regions('\n'.join(remaining)) or regions(title)


def _beijing(names: list[str]) -> bool:
    # An explicit province/city wins over ambiguous district names such as
    # 朝阳 or 通州, which also exist outside Beijing.
    return '北京' in names or (bool(set(names) & set(BEIJING_LOCALITY_TERMS))
                              and not set(names) & set(NON_BEIJING_LOCALITY_TERMS))


def build_locality_facts(text: str, *, title: str = '', category: str = '',
                         title_constraints: bool = False) -> LocalityFacts:
    context = text + ('\n' + title if title_constraints else '')
    clauses = [x.strip() for x in re.split(r'[\n，,。；;！!]', context) if x.strip()]
    own_names = regions(text)
    title_names = regions(title)
    fallback_names = own_names or (title_names if len(title_names) == 1 else [])
    restricted = []
    pickup_clauses = []
    preference = []
    remote = []
    for clause in clauses:
        # Neither a preference nor an optional pickup offer excludes shipping.
        optional = bool(PREFERENCE.search(clause) or OPTIONAL.search(clause))
        clean = re.sub(r'(?:并非|不是|不)(?:仅限|只限)(?:本地|同城)'
                       r'|不限(?:于)?(?:本地|同城|地区|地域)', '', clause)
        exclusive = bool(EXCLUSIVE.search(clean) or REGION_LIMIT.search(clean))
        shipping = bool(SHIPPING.search(clean))
        if re.search(r'(?:自提|面交|面取).{0,4}(?:或|也可).{0,4}(?:邮寄|快递|发货)', clean):
            exclusive = False
        if exclusive:
            restricted.append(clause)
        elif PICKUP.search(clean) and not optional and not shipping:
            pickup_clauses.append(clause)
        elif optional:
            preference.append(clause)
        if shipping and not exclusive:
            remote.append(clause)
    if not restricted and not remote and not preference:
        restricted = pickup_clauses

    # A city-specific service has an intrinsic usage area even when the seller
    # omits words such as "only". Mailing a coupon does not change its validity.
    from .subjects import service_subject
    service = bool(service_subject(text) or (category == 'virtual.service' and OFFLINE_SERVICE.search(text + '\n' + title)))
    service_names = service_regions(text,title if title_constraints else '')
    if service and re.search(r'(?:北京|帝都).{0,4}(?:除外|不可用|不能用|不适用)'
                             r'|(?:不支持|不适用|不含).{0,4}(?:北京|帝都)', context):
        return LocalityFacts('non_beijing_local_only', service_names, ['regional_service'], preference, [])
    if service and not (NATIONWIDE.search(context) or REMOTE_SERVICE.search(context)):
        scope = ('beijing_local_ok' if _beijing(service_names) else
                 'non_beijing_local_only' if service_names else 'unknown_region_local_only')
        return LocalityFacts(scope, service_names, ['regional_service'], preference, [])

    if restricted:
        # Read the location of the restriction before unrelated purchase or
        # warranty locations elsewhere in the item description.
        names = regions('\n'.join(restricted)) or fallback_names
        scope = ('beijing_local_ok' if _beijing(names) else
                 'non_beijing_local_only' if names else 'unknown_region_local_only')
        return LocalityFacts(scope, names, restricted, preference, [])
    scope = ('beijing_local_ok' if _beijing(fallback_names) and preference else
             'optional_pickup_or_remote' if preference or remote else 'not_local')
    return LocalityFacts(scope, fallback_names, [], preference, remote)
