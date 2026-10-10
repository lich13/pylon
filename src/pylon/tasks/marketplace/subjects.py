"""Source-owned subjects and facts, separated from protection and background."""
from __future__ import annotations

import re

# These clauses explain ownership/use; the mentioned device is not an offer.
BACKGROUND = re.compile(r'原(?:来|先|本)(?:用|的)|之前(?:用|的)|以前(?:用|的)|换成|换了|换用|对比|相比|比起|相较|不如|类似|送(?:了|我|给我)|给(?:了)?我|我(?:哥|弟|姐|妹|爸|妈|朋友).{0,12}(?:给|送)|上一个(?!帖)|上一台')
PROTECTION = re.compile(r'(?:贴|撕|揭)(?:了|着|有|掉|开)?[^，,。；;\n]{0,16}(?:膜)|(?:全程|一直|到手|买来|开箱)(?:就|都|后)?(?:带壳|戴壳)|镜头膜|镜头保护(?:膜|盖)')
SEPARATE = re.compile(r'(?<!不)(?:另出|另售|单出|单卖|另外出售|同时出售|单独出售)')
COMPATIBLE = re.compile(r'^(?:也|还|仅|只|可|且)?(?:适配|兼容|支持|适用|用于|搭配|不含|不包括|不包含|不带)')
OPTIONAL = re.compile(r'(?:如果|假如|若).{0,32}(?:同意|需要|愿意|想要)|(?:以后|后续|将来).{0,12}(?:可以|可能|考虑)|(?:可以|考虑|打算)(?:再|另外)?(?:订阅|升级|加购)')

# The object of a service is not a physical item offered for sale. Match the
# action together with an entitlement noun; appliances and supplies stay goods.
SERVICE_SUBJECT = re.compile(
    r'(?:洗鞋|洗衣|洗车|修鞋|修表|清洗|清洁|维修|保养|安装|护理)(?!机|器|液|剂|刷|设备|工具)'
    r'[^，,。；;\n]{0,8}(?:服务|权益|券|次数|套餐|次卡)'
    r'|(?:家政|保洁|钟点工)(?!机|器|液|剂|工具)'
    r'|(?:上门|到店)(?:清洗|清洁|维修|安装|护理)')


def service_subject(text: str) -> str:
    head = subject_text(text).split('\n', 1)[0]
    match = SERVICE_SUBJECT.search(head)
    return match[0] if match else ''


def incidental(clause: str) -> bool:
    return bool(not SEPARATE.search(clause) and (BACKGROUND.search(clause) or COMPATIBLE.search(clause.strip()) or OPTIONAL.search(clause)))


def subject_text(text: str) -> str:
    """Remove role clauses for identity only; the original stays intact for audit."""
    result=[]
    for line in text.splitlines():
        clauses=[]
        for clause in re.split(r'[，,。；;|]',line):
            if incidental(clause):
                continue
            if PROTECTION.search(clause) and not SEPARATE.search(clause):
                # A listing explicitly selling a film/case is still an offer.
                if not re.search(r'^(?:\s*\d+[、.)]\s*)?(?:出售|出(?!现)|转让|单卖)',clause.strip()):
                    clause=PROTECTION.sub('',clause)
            clauses.append(clause)
        cleaned='，'.join(clauses).strip()
        if cleaned:result.append(cleaned)
    return '\n'.join(result)


def heading(text: str) -> str:
    clean=subject_text(text)
    return clean.split('\n',1)[0].strip() if clean else ''


def platform_hint(value: str, subject: str) -> str | None:
    # A hint needs device-level evidence and can never override a named accessory.
    if (re.search(r'手机|移动电话|smartphone',value,re.I)
            and re.search(r'\d{1,2}\s*\+\s*\d{2,4}\s*G?|(?:内存|容量)\s*\d+|国行|全网通',subject,re.I)
            and not re.search(r'手机壳|保护壳|保护膜|镜头膜|充电|数据线|支架',heading(subject))):
        return 'phone.smartphone'
    return None


FIELDS = {
    'brand':'品牌','series':'系列','model':'型号','price':'价格','condition':'成色',
    'ram_gb':'内存 GB','storage_gb':'容量 GB','size':'尺寸／尺码','network':'网络版本',
    'fault':'故障声明','repair':'维修声明','platform':'权益平台','plan':'套餐',
    'duration':'期限','region':'使用地区','transfer':'转让限制',
    'product':'产品名称','origin':'权益来源','keywords':'排除词',
}


def applicable_fields(category: str) -> list[str]:
    common={'brand','series','model','price','keywords','product'}
    group=category.split('.',1)[0]
    if group=='virtual':common.update({'platform','plan','duration','region','transfer','origin'})
    else:
        common.add('condition')
        if group=='life':common.add('size')
        else:common.update({'size','fault','repair'})
        if group in {'phone','tablet','computer','component','storage'}:common.update({'ram_gb','storage_gb'})
        if group in {'phone','tablet','computer','network','wearable'}:common.add('network')
    return [key for key in FIELDS if key in common]


def facts(text: str, attrs: dict, *, price=None, category='') -> tuple[dict,dict]:
    clean=subject_text(text)
    values={};proof={}
    def put(field,value,evidence):
        if value not in (None,'',[]):
            values[field]=value;proof[field]=[evidence]
    for field,old in (('brand','brands'),('series','series'),('model','models')):
        # These lists have already been extracted from source-owned text.
        put(field,attrs.get(old,[]),heading(clean))
    owned_name=attrs.get('subject') or heading(clean)
    put('product',owned_name,owned_name)
    # The text has already been bound to one item. Do not inherit another
    # item's product name or incidental replacement story into exclusions.
    put('keywords',clean,clean)
    if category.startswith('virtual.'):
        origin_text=re.sub(r'(?:不是|非|不来自|非来自)\s*88\s*VIP|与\s*88\s*VIP\s*无关','',clean,flags=re.I)
        origin=re.search(r'88\s*VIP',origin_text,re.I)
        if origin:put('origin',['88VIP'],origin[0])
    pair=re.search(r'(?<!\d)(\d{1,3})\s*(?:GB|G)?\s*\+\s*(\d{1,4})\s*(TB|T|GB|G)?(?![\dA-Za-z])',clean,re.I)
    if pair:
        capacity=int(pair[2])*(1024 if (pair[3] or '').upper().startswith('T') else 1)
        put('ram_gb',int(pair[1]),pair[0]);put('storage_gb',capacity,pair[0])
    else:
        for field,pattern in (
            ('ram_gb',r'(?:内存|RAM)\s*(\d{1,3})\s*(?:GB|G)?'),
            ('storage_gb',r'(?<![\dA-Za-z])(\d{1,4})\s*(TB|T|GB|G)(?![A-Za-z])')):
            found=re.search(pattern,clean,re.I)
            if found:
                factor=1024 if field=='storage_gb' and found[2].upper().startswith('T') else 1
                put(field,int(found[1])*factor,found[0])
    for field,pattern in (
        ('size',r'(?<![\dA-Za-z])(?:\d+(?:\.\d+)?\s*(?:mm|英寸|寸|码)|(?:XXL|XL|XS|L|M|S)码)'),
        ('network',r'全网通|蜂窝|cellular|Wi-?Fi|双卡|单卡|5G|4G'),
        ('platform',r'京东|天鹅到家|Spotify|Apple\s*Music|ChatGPT|Claude|Grok|Netflix|YouTube|山姆'),
        ('duration',r'\d+\s*(?:小时|个月)|(?:期限|时长|有效期|剩余)\s*[:：]?\s*\d+\s*(?:月|年|天)|永久|终身|有效期[^\n，。]{1,24}'),
        ('region',r'仅限[^\n，。]{1,16}(?:用|地区)|北京|上海|广州|深圳|全国'),
        ('transfer',r'不可转让|不能转让|可转让|支持过户|不可过户|仅限本人'),
        ('plan',r'(?:套餐|版本)[：:\s]*[^\n，。]{1,24}|Plus|Premium|Pro\s*Max|Team')):
        if category and field not in applicable_fields(category):continue
        found=re.findall(pattern,clean,re.I)
        if found:put(field,list(dict.fromkeys(found)),found[0])
    for field,negative,positive in (
        ('fault',r'无故障|功能正常|一切正常|无暗病',r'故障|坏了|不能开机|无法开机|花屏|漏液|屏碎'),
        ('repair',r'无拆无修|无拆修|未拆修|未维修|无维修|没有维修',r'已维修|维修过|修过|换过屏|换过电池|拆修过')):
        absent=re.search(negative,clean)
        present=re.search(positive,re.sub(negative,'',clean))
        # Conflicting claims are unresolved rather than a reassuring negative.
        if absent and present:
            attrs.setdefault('attribute_conflicts',[]).append(field)
            continue
        if absent:put(field,'无',absent[0])
        elif present:put(field,'有',present[0])
    condition=None;evidence=''
    condition_text=re.sub(r'(?:无|没有|没|未见|不存在)(?:明显)?(?:划痕|磕碰|磨损)', '', clean)
    for value,pattern in (('有故障',r'不能开机|无法开机|花屏|漏液|屏碎'),
                          ('近新',r'充新|几乎全新|[89](?:\.\d)?成新|9\.9新|99新'),
                          ('二手',r'自用|用过|用了|使用了|使用过|使用.{0,6}(?:天|月|年|小时)|划痕|磕碰|电池\s*\d{1,2}%'),
                          ('未拆封',r'全新未拆|未拆封'),('全新',r'全新|未使用|未激活')):
        found=re.search(pattern,condition_text)
        if found:
            # Accessory condition cannot certify the device's condition.
            clause=re.split(r'[\n，,。；;]',condition_text[max(0,found.start()-28):found.end()])[-1]
            if re.search(r'配件|线材|原装线|包装|充电器',clause) and value in {'全新','未拆封'}:continue
            condition=value;evidence=found[0];break
    put('condition',condition,evidence)
    put('price',price,attrs.get('price_evidence',''))
    return values,proof
