"""Classify actual sale subjects, not every model mentioned in a post."""
from __future__ import annotations

import re
from .taxonomy import RULES, canonical

VERSION = 12
from . import subjects

PATTERNS = [(key, re.compile(pattern, re.I)) for key, _, pattern in RULES]
SOLD = re.compile(r'已售|已出售(?!中)|已出(?!售)|已预[订定]|售罄|已被预[订定]|预[订定]给|封帖|封贴|结贴|勿拍')
ALL_SOLD = re.compile(r'(?:全部|所有|全都|均)(?:已经|已)(?:售出|出售|出完|售罄|出清|出掉|出了|出(?=\W|$))|(?:全部|所有|全都|均)(?:售罄|出完|出清|出了)')
BUY = re.compile(r'求购|求收|想收|询价|价格咨询|什么价|值多少|行情多少|求推荐|求助|求问|询问|估价|不出售|不是出货|[\[【](?:收|求购|预收)[\]】]|(?:^|[\]】])\s*(?:收|预收)\s*(?:一|个|台|张|两|[A-Za-z0-9])')
ACQUIRE = r'收(?!到|齐|件|款|货|藏|据|入|益|费|音|纳|尾|拾)'
WANTED_BUDGET = re.compile(r'(?<![\d.A-Za-z])([¥￥]?\d+(?:\.\d+)?\s*(?:元|块)?)\s*(?='+ACQUIRE+r')')
WANTED = re.compile(r'求购|求收|想收|[\[【](?:收|求购|预收)[\]】]|(?:^|[\s。；;，,\]】])\s*(?:预)?'+ACQUIRE+r'\s*\S|'
                    +WANTED_BUDGET.pattern+r'|(?:长期|持续|一直)\s*收(?:\W|$)|求(?:一|两|二|几)[张台个只件]|有没有人(?:卖|出)')
WANTED_CLOSED = re.compile(r'已收(?:到|齐|到货)?(?:\W|$)|[\[【]已收[\]】]|不收了|停止收购|需求已(?:解决|满足)|已找到')
INQUIRY = re.compile(r'询价|价格咨询|行情多少|值多少|求推荐|求助|求问|询问|估价|只了解行情|请教.*(?:购买|怎么买|选择)')
SALE = re.compile(r'出售|闲置|转让|明盘|卖|(?:^|[\s，,])出|元出|包邮|可刀|走鱼|闲鱼|挂价|议价|私聊')
GIFT = re.compile(r'赠送|赠品|送的|随.{0,12}赠|附赠|(?:^|[\s|，,。；;])送(?!货|达|修|到|测|检)')
BUNDLE = re.compile(r'打包|整套|不拆卖|不单卖|一起出|合计')
EXCLUSIVE_BUNDLE = re.compile(r'不拆(?:卖|出)?|不单卖|(?:只|仅|必须|全部)打包|打包(?:一起)?(?:出|出售)|整套(?:出|出售)')
NUMBERED = re.compile(r'^\s*(?:\d{1,2}(?:[、)）]|\.(?!\d))|[一二三四五六七八九十][、.])\s*')
ATTACHMENT_HEADER = re.compile(
    r'^.{1,240}\.(?:jpe?g|png|gif|webp|bmp|heic)\s*[（(]\s*\d+(?:\.\d+)?\s*'
    r'(?:[KMG]?B|Bytes?)\s*[,，]\s*下载次数\s*[:：]\s*(?:\d+\s*[)）]?)?$', re.I)
# These describe how a device was protected, not a screen protector being
# offered separately. Keep the original text for condition and price checks.
FILM_USE = re.compile(
    r'(?:全程|一直|长期|到手|买来|拿到|已经|已|刚|自行|自己|自用|带壳|戴壳)'
    r'(?:就|都|有|在|后)?(?:带壳|戴壳)?(?:贴膜|贴(?:了|着|有)?(?:钢化|屏幕|保护)?膜)'
    r'|(?:贴膜|贴(?:钢化|屏幕|保护)膜)(?:使用|用了|保护|带壳|戴壳)'
    r'|(?:屏幕|机身)(?:上)?(?:贴|有|自带)(?:了|着|有)?(?:钢化|屏幕|保护)?膜'
    r'|(?:钢化膜|屏幕膜|贴膜)(?:已经|已|有|存在)?(?:一点|轻微|少量)?(?:碎|裂|破损|气泡|划痕)'
    r'|(?:撕|揭)(?:了|掉|开)?(?:钢化膜|屏幕膜|贴膜)')


def _sale_body(body: str, source: str) -> str:
    """Keep authored content; omit Discuz image controls from item parsing."""
    if source != 'chiphell':
        return body
    lines = []
    download_count = 0
    for original in body.splitlines():
        line = original.strip()
        if ATTACHMENT_HEADER.fullmatch(line):
            download_count = 1 if line.endswith((':','：')) else 0
            continue
        if download_count and not line:
            continue
        if download_count == 1 and re.fullmatch(r'\d+\s*[)）]?', line):
            download_count = 0 if line.endswith((')','）')) else 2
            continue
        if download_count == 2 and line in {')','）'}:
            download_count = 0
            continue
        download_count = 0
        if line == '下载附件' or re.fullmatch(r'\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?\s+上传',line):
            continue
        lines.append(original)
    return '\n'.join(lines)


def _clean_subject(text: str) -> str:
    # Gifts and descriptions of compatible devices are not sale subjects.
    text = subjects.subject_text(text)
    lines = ['，'.join(FILM_USE.sub('', clause) for clause in re.split(r'[，,。|]', line)
                      if (not GIFT.search(clause) or WANTED.search(clause)
                          or re.search(r'^\s*(?:\d+[、.)]\s*)?(?:出售|出(?!现)|转让|卖)|\d+\s*元出',clause))
                      and not re.match(r'^\s*(?:不含|不包括|不包含|不带)',clause)).strip()
             for line in text.splitlines()]
    return '\n'.join(line for line in lines if line).strip()


def category_for(text: str) -> tuple[str, str]:
    subject = _clean_subject(text)
    if '\n' in subject:
        heading = subject.split('\n',1)[0].strip()
        category, evidence = category_for(heading)
        if category == 'component.memory':
            for key in ('component.memory_ecc','component.memory_laptop','component.memory_desktop'):
                match = dict(PATTERNS)[key].search(subject)
                if match:
                    return key, match[0]
        if (category=='unknown' and re.search(r'[A-Za-z].*\d|\d.*[A-Za-z]',heading)
                and re.search(r'(?:^|[\]】])\s*(?:出|转让|出售|求购|收)',heading)
                and not re.search(r'多件|若干|一堆|一批|如下|内详|分别|配置',heading)):
            return 'unknown',''
        if category!='unknown' and not re.search(r'多件|若干|一堆|一批|如下|内详|分别',heading):
            return category,evidence
    hits = {}
    positions = {}
    for key, pattern in PATTERNS:
        match = pattern.search(subject)
        if not match:
            continue
        # Short model tokens have meaning only inside a product family. A
        # processor R5 and a phone Z9 are not evidence for a camera body.
        if key == 'camera.body' and not re.search(r'相机|微单|单反', match[0]):
            if not re.search(r'索尼|Sony|尼康|Nikon|佳能|Canon|相机|微单|单反|快门|镜头卡口', subject, re.I):
                continue
        # A phone's asking price such as 3090 yuan is not a graphics-card model.
        if key=='component.gpu' and match[0].isdigit() and re.match(r'\s*(?:元|块|人民币|RMB|包邮)',subject[match.end():],re.I):
            continue
        hits[key] = match[0]
        positions[key] = match.start()
    if not hits:
        from .taxonomy import extension_category
        return extension_category(subject) or ('unknown', '')
    # A manufacturer name is an attribute, not proof of a smartphone. Prefer
    # the adjacent explicit product noun (红米音箱, OPPO耳机, 荣耀路由器).
    # A concrete phone model between brand and another noun keeps its role.
    if hits.get('phone.android','').casefold() in {'红米','redmi','galaxy','iqoo','oneplus','一加','oppo','vivo','荣耀'}:
        end = positions['phone.android'] + len(hits['phone.android'])
        if any(not key.startswith('phone.') and 0 <= positions[key]-end <= 12
               and not re.search(r'\d|手机', subject[end:positions[key]])
               for key in hits):
            del hits['phone.android']
    if 'virtual.service' in hits and re.search(r'京东\s*(?:家政|保洁)|天鹅到家.{0,6}(?:家政|保洁)',subject):
        return 'virtual.service',hits['virtual.service']
    virtual = [key for key in ('virtual.cloud_host','virtual.sim') if key in hits]
    if virtual:
        key = min(virtual,key=positions.__getitem__)
        physical_start = min((positions[k] for k in hits if not k.startswith('virtual.')),default=len(subject))
        # Cloud servers and SIM cards contain physical-device words. Prefer
        # the entitlement only when it is the leading sale subject.
        if positions[key] <= physical_start:
            return key,hits[key]
    for key in ('component.memory_laptop','component.memory_desktop'):
        if key in hits and not re.search(r'整机|主机|整套电脑',subject):
            return key, hits[key]
    if 'game.controller' not in hits and 'game.media' not in hits and not any(k.startswith('accessory.') for k in hits):
        for key in ('game.handheld','game.console'):
            if key in hits and (key!='game.console' or 'network.switch' not in hits):
                return key, hits[key]
    if 'storage.nas' in hits and not re.search(r'整机|主机|含|搭载|自组',subject):
        for key in ('storage.hdd','storage.ssd_nvme','storage.ssd_sata','storage.ssd','storage.drive'):
            if key in hits:
                return key,hits[key]
    # An assembled system contains components; those are not separate sales.
    systems = ('computer.server', 'computer.aio', 'computer.laptop', 'computer.mini', 'computer.desktop')
    if not re.search(r'拆卖|单出|单卖|拆机件', subject):
        for key in systems:
            if key in hits and not re.search(r'笔记本内存|服务器内存|服务器.{0,12}(?:ECC|RDIMM)|主机电源|机箱电源', subject):
                if key == 'computer.mini' and 'network.router_soft' in hits:
                    key = 'network.router_soft'
                return key, hits[key]
    if 'component.gpu' in hits and 'component.custom_loop' in hits and '，' in subject:
        heading, rest = subject.split('，',1)
        if (category_for(heading)[0]=='component.gpu'
                and not re.search(r'单出|单卖|单独|适配|兼容|专用|配件',rest)):
            return 'component.gpu',hits['component.gpu']
    priorities = (
        'accessory.phone_case', 'accessory.film', 'accessory.stand', 'accessory.charger',
        'accessory.powerbank', 'accessory.cable', 'accessory.dock', 'accessory.stylus', 'accessory.battery',
        'component.cpu_board_bundle', 'component.memory_ecc', 'component.memory_laptop','component.memory_desktop',
        'component.custom_loop', 'component.aio', 'component.fan',
        'storage.enclosure', 'storage.nas', 'storage.external', 'storage.ssd_sata', 'storage.ssd_nvme',
        'network.router_soft', 'network.switch', 'network.ap', 'network.cpe', 'network.modem',
        'display.portable', 'input.keycaps', 'input.mousepad', 'game.media', 'game.controller',
        'game.handheld', 'game.vr', 'audio.portable', 'audio.tws', 'audio.inear', 'audio.overear',
        'audio.dac', 'camera.action', 'camera.pocket', 'camera.drone', 'camera.lens',
        'tablet.ipad', 'tablet.android', 'tablet.ereader', 'wearable.child_watch', 'wearable.watch', 'wearable.band',
    )
    for key in priorities:
        if key in hits:
            return key, hits[key]
    return next(iter(hits.items()))


def price_analysis(text: str):
    from pylon.shared.sale_price import analyse_price
    category = canonical(category_for(text)[0])
    return analyse_price(text, allow_bare=category != 'unknown',
                         subject=lambda line: category_for(line)[0] != 'unknown',
                         recurring=category.startswith('virtual.'),
                         capacity_context=category.startswith(('phone.','tablet.','computer.','storage.','component.memory')))


def asking_price(text: str) -> int | float | None:
    return price_analysis(text).amount


def entitlement_origin(title: str, body: str, item_text: str) -> str:
    """An explicitly named benefit group supplies its children's provenance."""
    if subjects.SEPARATE.search(item_text) or re.search(r'不是|非88|无关|不来自',title):return ''
    named=re.search(r'88\s*VIP\s*(?:的|开通的|附带的|各种|各类|全部)?\s*(?:会员|权益|赠送|赠品|拆卖|拆出)',title,re.I)
    shorthand=re.search(r'88\s*(?:各种|各类|全部)?\s*(?:会员|权益)',title)
    token=re.search(r'88\s*VIP',title,re.I)
    if not named and not token and not (shorthand and re.search(r'88\s*VIP',body,re.I)):return ''
    match=named or token or shorthand
    remainder=title[:match.start()]+title[match.end():]
    if subjects.SEPARATE.search(remainder):return ''
    if named and re.search(r'权益|赠送|赠品|拆卖|拆出',named[0]):return '88VIP'
    if token and re.search(r'开(?:通)?(?:了|的)?\s*$',title[:token.start()]):return '88VIP'
    # A title listing a second independent subscription is not a common origin.
    if any(key.startswith('virtual.') and key not in {'virtual.membership','virtual.retail_membership'} and pattern.search(remainder)
           for key,pattern in PATTERNS):return ''
    return '88VIP'


def _blocks(title: str, body: str) -> list[str]:
    text = re.sub(r'(?:另出|另外出售|同时出售|再出)', '\n另外出售 ', body)
    lines = [line.strip() for line in re.split(r'\n+|[；;]|(?=\b\d{1,2}[、）])', text) if line.strip()]
    blocks: list[str] = []
    title_category = category_for(title)[0]
    configuration = False
    for position, line in enumerate(lines):
        if (subjects.incidental(line) or subjects.PROTECTION.search(line)) and not subjects.SEPARATE.search(line) and not NUMBERED.match(line):
            if blocks:
                blocks[-1] += '\n' + line
            else:
                blocks.append(line)
            continue
        if GIFT.search(line) and not SALE.search(line) and not NUMBERED.match(line):
            if blocks:
                blocks[-1] += '\n' + line
            else:
                blocks.append(line)
            continue
        category, _ = category_for(line)
        current_category = category_for(blocks[-1])[0] if blocks else 'unknown'
        configuration = configuration or bool(re.search(r'配置(?:如下|清单|明细|[：:])',line))
        closed_price = bool(blocks and asking_price(blocks[-1]) is not None)
        # A product heading and its asking price often occupy different lines.
        # Once the preceding sale has a price, a new concrete subject with a
        # subsequent asking price is a separate sale, not a configuration row.
        priced_heading = (closed_price and category != 'unknown' and not configuration
                          and not re.search(r'适配|兼容|搭配|支持|对比|原来|原先|用于|因为|换成|内含|附带',line)
                          and any(asking_price(following) is not None and not GIFT.search(following)
                                  for following in lines[position:position+12]))
        starts = (NUMBERED.match(line) or
                  priced_heading or (category != 'unknown' and (asking_price(line) is not None or SALE.search(line) or SOLD.search(line))))
        starts = starts or (category != 'unknown' and WANTED.search(line))
        if not blocks or starts:
            # Configuration lines following an assembled system describe it.
            if (blocks and current_category.startswith('computer.') and category.startswith(('component.', 'storage.'))
                    and not priced_heading and not re.search(r'单出|单卖|另外|元出|出售', line)):
                blocks[-1] += '\n' + line
            else:
                blocks.append(line)
        else:
            blocks[-1] += '\n' + line
    if not blocks:
        return [title]
    if len(blocks) == 1:
        if (GIFT.search(blocks[0]) and category_for(blocks[0])[0]!='unknown'
                and re.search(r'和|与|及|、',title) and category_for(title)[0].startswith('accessory.')):
            return blocks
        body_category = category_for(blocks[0])[0]
        title_category = category_for(title)[0]
        # A clearly priced sale in the body owns its subject and amount. Do
        # not attach a different device title to a separately sold accessory.
        if (body_category!='unknown' and body_category!=title_category and SALE.search(blocks[0])
                and subjects.SEPARATE.search(subjects.heading(blocks[0]))
                and asking_price(blocks[0]) is not None and title_category!='unknown'
                and not (title_category.startswith('computer.') and not re.search(r'单出|单卖|另外',blocks[0]))):
            return blocks
        return [title + '\n' + blocks[0]]
    first_category = category_for(blocks[0])[0]
    if title_category != 'unknown' and first_category == title_category:
        blocks[0] = title + '\n' + blocks[0]
    if ((first_category == 'unknown' and title_category != 'unknown') or
            (title_category.startswith('computer.') and first_category.startswith(('component.','storage.'))
             and not re.search(r'单出|单卖|另外|元出|出售',blocks[0]))):
        blocks[0] = title + '\n' + blocks[0]
    return blocks


def _sale_parts(text: str, *, title: str = '') -> list[str]:
    """Split explicit coordinated items, never a system specification list."""
    category, _ = category_for(text)
    raw_parts = [p.strip() for p in re.split(r'(?<!\d)[、]|\s*(?:以及|另有|另外(?!出售))\s*|(?<!\d)\s*\+\s*(?!\d)|[，,]',text) if p.strip()]
    parts = []
    for part in raw_parts:
        related = re.match(r'^(?:也|还|仅|只|可|且)?(?:适配|兼容|支持|适用|用于|搭配|对比|不含|不包括|不包含|不带)',part)
        separate = re.search(r'(?<!不)(?:出售|单卖|单出|另售|另出)',part.split('\n',1)[0])
        if parts and related and not separate:
            parts[-1] += '\n' + part
        else:
            parts.append(part)
    if len(parts)==1:
        return parts
    categories = [category_for(p)[0] for p in parts]
    if category.startswith('computer.') and not re.search(r'(?<!不)(?:拆卖|单卖|单出)',text):
        configuration = re.search(r'配置|内含|内置|包含|搭载|整套|打包|不拆|不单卖',text)
        separate_sales = sum(
            key != 'unknown' and asking_price(part) is not None
            and (not key.startswith(('component.','storage.')) or bool(SALE.search(part)))
            for part,key in zip(parts,categories)
        )
        # A separately priced watch or second computer is not part of a
        # system's configuration. Component costs still need sale evidence.
        if configuration or separate_sales < 2:
            return [text]
    if (len(parts)>1 and categories[0]=='component.gpu' and asking_price(text) is not None
            and all(key in {'component.custom_loop','component.aio','component.fan','component.air_cooler'}
                    for key in categories[1:])
            and all(re.search(r'状态|完好|正常|随卡|自带|附带',part.split('\n',1)[0])
                    and not re.search(r'单出|单卖|单独|出售',part) for part in parts[1:])):
        return [text]
    starts = [category != 'unknown' and bool(asking_price(part) is not None or SOLD.search(part) or SALE.search(part))
              for category, part in zip(categories, parts)]
    if sum(starts) > 1:
        # Condition and delivery clauses do not erase boundaries between two
        # explicit sales. Keep those clauses with their preceding item so
        # one item's sold status and price never flow into the next item.
        grouped = []
        for part, start in zip(parts, starts):
            if not grouped or start:
                grouped.append(part)
            else:
                grouped[-1] += '，' + part
        # A title plus a repeated description of the same device is one sale.
        # Do not leave a live title-only phantom when its body says it sold.
        prefix_body=grouped[0].partition('\n')[2]
        if (len(grouped)>1 and title and grouped[0].startswith(title+'\n')
                and category_for(prefix_body)[0]=='unknown' and asking_price(prefix_body) is None):
            from .wanted import MODELS, normalize
            def identities(value):
                owned=_clean_subject(value)
                if canonical(category_for(value)[0])=='phone.smartphone':
                    owned=re.sub(r'(?<![A-Za-z0-9])((?:1[0-9])\s*(?:pro\s*max|pm|pro|plus|mini))(?![A-Za-z])',r'iPhone \1',owned,flags=re.I)
                    owned=re.sub(r'\b(iPhone\s*\d+)\s*pm\b',r'\1 Pro Max',owned,flags=re.I)
                return {normalize(m[0]) for pattern in MODELS for m in pattern.finditer(owned)}
            first=identities(grouped[0])
            if first and any(first & identities(p) for p in grouped[1:]):
                merged=[grouped[0]]
                capacities=lambda value:set(re.findall(r'(?<![A-Za-z0-9])(\d{2,4})\s*(?:GB|G)(?![A-Za-z])',value,re.I))
                original_capacity=capacities(grouped[0])
                for part in grouped[1:]:
                    current_capacity=capacities(part)
                    if (first==identities(part) and not subjects.SEPARATE.search(subjects.heading(part))
                            and not (original_capacity and current_capacity and original_capacity!=current_capacity)):
                        merged[0]+='，'+part
                    else:merged.append(part)
                grouped=merged
        return grouped
    explicit_sales = all(asking_price(part) is not None or SOLD.search(part) or SALE.search(part) for part in parts)
    if (len(parts)>1 and all(c!='unknown' for c in categories)
            and (len(set(categories))>1 or explicit_sales)):
        return parts
    return [text]


def classify_listing(title: str, body: str, source: str = 'v2ex', body_complete: bool = True,
                     *, single_item: bool = False, platform_category: str = '') -> list[dict]:
    result = []
    from .content_quality import contamination
    if contamination(source,title,body):body_complete=False
    if not body_complete:
        body = ''
    # Normalise grouped amounts before segmentation, not after commas have
    # already split a table price into separate apparent products.
    normalise_amounts = lambda text: re.sub(r'(?<![\d,])\d{1,3}(?:,\d{3})+(?![\d,])',
                                          lambda m:m[0].replace(',', ''),text)
    title = normalise_amounts(title)
    body = normalise_amounts(_sale_body(body,source))
    blocks = ([(body or title, False)] if single_item else
              [(part, len(parts)>1 and bool(BUNDLE.search(block)))
               for block in _blocks(title, body) for parts in [_sale_parts(block,title=title)] for part in parts])
    title_buy = bool((BUY.search(title) or WANTED.search(title)) and not re.search(r'另出|同时出|出售', title))
    title_sold = bool(re.match(r'^\s*(?:\[|【)?(?:已售|已出|全出|全部已)', title)
                      and not re.search(r'另出|还有|继续出|其他|剩余|分别|、|，|,',title))
    # A named group (for example all memory sticks) closing does not close
    # another item's sale. Only unscoped body remarks can close the whole post.
    title_closed = ALL_SOLD.search(title) and not re.search(r'另出|另外|还有|剩余|继续出|其他',title)
    all_sold = bool(title_closed or any(ALL_SOLD.search(line) and category_for(line)[0]=='unknown'
                                       for line in re.split(r'[\n。；;，,]',body)))
    from pylon.tasks.quotes.arbitrage import is_excluded_device_deal
    from pylon.tasks.quotes.matching import newness_evidence
    from .locality import build_locality_facts
    # Only transaction clauses may be inherited; never another item's price or condition.
    common_terms = '\n'.join(line for line in re.split(r'[\n。；;，,]', body)
                             if re.search(r'邮寄|快递|包邮|顺丰|自提|面交|不出外地|本地|同城|全国|地区', line)
                             and category_for(line)[0] == 'unknown')
    # Unscoped, explicit all-or-nothing terms apply to every item. A named
    # system's bundle restriction must not affect another separately sold item.
    bundle_terms = [line for line in body.splitlines()
                    if category_for(line)[0] == 'unknown' and EXCLUSIVE_BUNDLE.search(line)
                    and not GIFT.search(line)]
    if EXCLUSIVE_BUNDLE.search(title) and not re.search(r'另出|另外|(?<!不)(?:单出|单卖)',title):
        bundle_terms.append(title)
    bundle_only = bool(bundle_terms)
    bundle_price = asking_price('\n'.join(bundle_terms)) if bundle_only else None
    for index, (text, shared_bundle) in enumerate(blocks):
        category, evidence = category_for(text)
        identity = subjects.subject_text(text)
        identity_head = subjects.heading(text)
        candidate_clauses=[clause.strip() for line in text.splitlines() for clause in re.split(r'[，,。；;]',line)
                           if clause.strip() and not subjects.incidental(clause)]
        subject=next((clause for clause in candidate_clauses if category_for(clause)[0]!='unknown'
                      or re.search(r'(?:^|[\]】])\s*(?:出|转让|出售|求购|收)',NUMBERED.sub('',clause))),subjects.heading(text))
        if category == 'unknown' or (category=='computer.desktop' and not re.search(r'台式|电脑|主机|配置',identity)):
            hinted = subjects.platform_hint(platform_category, identity)
            if hinted:
                category, evidence = hinted, subject
        if len(blocks)>1 and category=='unknown' and not NUMBERED.match(text) and not SALE.search(text):
            continue
        own_sale = bool(SALE.search(text) and not WANTED.search(text))
        wanted_text = re.sub(r'(?:不|非|并非|不是|不再)\s*(?:求购|求收|想收)', '', text)
        wanted_title = re.sub(r'(?:不|非|并非|不是|不再)\s*(?:求购|求收|想收)', '', title)
        intent = ('wanted' if WANTED.search(wanted_text) or (WANTED.search(wanted_title) and not own_sale) else
                  'inquiry' if INQUIRY.search(title+'\n'+text) else 'sale')
        closed_request = intent=='wanted' and WANTED_CLOSED.search(text+'\n'+title)
        status = ('sold' if title_sold or all_sold or SOLD.search(text) or closed_request else 'uncertain' if intent=='inquiry' else 'buy' if ((title_buy and not own_sale) or BUY.search(text) or WANTED.search(text))
                  else 'available' if SALE.search(title + '\n' + text) or source in {'chiphell', 'nga'} else 'uncertain')
        gifted_subject=bool(re.match(r'^\s*(?:赠送|赠品|送的|附赠|送(?!货|达|修|到|测|检))',subject)
                            and not SALE.search(subject))
        if gifted_subject and category != 'unknown':
            status = 'uncertain'
        context = text + '\n' + common_terms
        # An item owns its location. Only an unscoped title restriction may
        # apply to every item; another product's city must not leak across.
        locality = build_locality_facts(context, title=title, category=canonical(category),
                                       title_constraints=len(blocks) == 1 or category_for(title)[0] == 'unknown')
        only_local = locality.transaction_scope in {'non_beijing_local_only','unknown_region_local_only'}
        shipping = bool(locality.remote_trade_evidence) and not (only_local or locality.local_only_evidence)
        pickup = bool(locality.local_only_evidence)
        attributes = {
            'intent': intent,
            'shipping': shipping, 'pickup_only': pickup and not shipping,
            'beijing': locality.transaction_scope == 'beijing_local_ok',
            'transaction_scope': locality.transaction_scope,
            'regional_service': 'regional_service' in locality.local_only_evidence,
            'excluded_device': is_excluded_device_deal(text),
            'body_complete': body_complete, 'bundle': bundle_only or shared_bundle or bool(BUNDLE.search(text)),
            'bundle_only': bundle_only, 'bundle_price': bundle_price,
            'condition': '全新' if newness_evidence(text) else '二手/未注明',
            'brands': [brand for brand,pattern in (
                ('苹果',r'苹果|iPhone|iPad|MacBook|AirPods|Apple'),('华为',r'华为|Huawei|Mate(?:Pad|Book)?\s*\d|Pura'),
                ('小米',r'小米|Xiaomi|Redmi|红米'),('三星',r'三星|Samsung|Galaxy'),('荣耀',r'荣耀|Honor'),
                ('OPPO',r'OPPO|OnePlus|一加'),('vivo',r'vivo|iQOO'),('索尼',r'索尼|Sony|PlayStation'),
                ('大疆',r'大疆|DJI|Osmo|Mavic'),('联想',r'联想|Lenovo|ThinkPad|Legion'),('华硕',r'华硕|ASUS|ROG'),
                ('微星',r'微星|MSI'),('英伟达',r'NVIDIA|英伟达|RTX|GTX'),('AMD',r'AMD|Ryzen|锐龙'),
                ('Intel',r'Intel|英特尔|酷睿')) if re.search(pattern,identity_head,re.I)],
            'specs': re.findall(r'(?<![\dA-Za-z])\d+(?:\+\d+)?\s*(?:GB|TB|G|T|mm|英寸)(?![A-Za-z])', identity, re.I),
            'models': re.findall(r'(?:iPhone|iPad\s*(?:Pro|Air|mini)?|Mate(?:Pad)?|Pura|Galaxy\s*[AS]?|RTX|GTX|RX|小米|Redmi|Ryzen|ROG)\s*\d{1,5}(?:\s*(?:Pro\s*Max|Pro|Plus|Ultra|SUPER|Ti|X3D|X))?',identity_head,re.I),
            'series': re.findall(r'iPhone|iPad|MacBook|ThinkPad|Galaxy|MatePad|Mate|Pura|Redmi|RTX|GTX|Ryzen|Osmo|Mavic',identity_head,re.I),
            'generation': re.findall(r'(?:iPhone|iPad\s*(?:Pro|Air|mini)?|Mate|Pura|小米|Redmi)\s*(\d{1,3})',identity_head,re.I),
            'classifier_version': VERSION,
            'conditional_terms': bool(subjects.OPTIONAL.search(text)),
        }
        if not body_complete:
            # Only the untruncated title is evidence when the full body is absent.
            attributes['condition'] = '成色待确认'
        attributes['brand_confirmed'] = category!='phone.unspecified'
        if category=='phone.apple' and '苹果' not in attributes['brands']:
            attributes['brands'].append('苹果')
        price_text = re.sub(r'预算|收价|出价', '售价', text) if intent=='wanted' else text
        if intent=='wanted':
            price_text = WANTED_BUDGET.sub(r'出价\1 ',price_text)
        attributes['external_price'] = bool(re.search(r'https?://',text)
                                             and re.search(r'闲鱼|咸鱼|价格.{0,12}(?:链接|详见)',text))
        price = price_analysis(price_text)
        from .price_terms import extract as payment_terms
        attributes.update(payment_terms(price_text,price))
        attributes.update(price_status='bundled' if bundle_only or shared_bundle else price.status,
                          price_reason='shared_bundle_price' if bundle_only or shared_bundle else price.reason,
                          price_evidence=price.evidence)
        if (status == 'uncertain' and intent == 'sale' and category != 'unknown'
                and price.amount is not None and not gifted_subject):
            status = 'available'
        attributes['subject'] = subject
        attributes['platform_category'] = platform_category
        attributes['facts'], attributes['attribute_evidence'] = subjects.facts(identity,attributes,price=price.amount,category=canonical(category))
        # A listing of explicitly split benefits carries its named origin to
        # each entitlement, but never to a separately sold physical device.
        if category.startswith('virtual.') and entitlement_origin(title,body,text):
            attributes['facts']['origin']=['88VIP']
            attributes['attribute_evidence']['origin']=[title]
        attributes['condition'] = attributes['facts'].get('condition','成色未注明') if body_complete else '成色待确认'
        result.append(dict(index=index, category=canonical(category), text=text, status=status,
                           price=None if bundle_only or shared_bundle else price.amount,
                           evidence=evidence, attributes=attributes))
    return result
