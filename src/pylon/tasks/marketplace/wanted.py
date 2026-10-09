"""An independent, explicit inventory for matching requests to buy."""
from __future__ import annotations

import json
import re
from pathlib import Path

from pylon.config import home
from .taxonomy import CATEGORIES, GROUPS, canonical, group_for

# Recognized product identities require a model, not merely a brand/category.
# Ordering keeps a more specific variant from becoming its base model.
MODEL_PATTERNS = (
    r'iPhone\s*(?:SE\s*[123]|\d{1,2})(?:\s*(?:Pro\s*Max|Pro|Plus|mini|Air))?',
    r'(?:小米|Xiaomi|Redmi|红米|POCO)\s*(?:Note\s*|[KFX])?\d{1,2}(?:\s*(?:Pro\s*Max|Pro\+?|Ultra|Turbo))?',
    r'(?:华为\s*|Huawei\s*)?(?:Mate|Pura|Nova|P)\s*\d{1,3}(?:\s*(?:Pro\+?|Ultra|RS|X))?',
    r'(?:Samsung\s*|三星\s*)?Galaxy\s*(?:[SA]\d{1,3}|Z\s*(?:Fold|Flip)\s*\d)(?:\s*(?:Ultra|Plus|\+))?',
    r'(?:vivo\s*X|OPPO\s*Find\s*X|iQOO\s*|荣耀\s*Magic|Honor\s*Magic|一加\s*|OnePlus\s*)\d{1,3}(?:\s*(?:Pro\+?|Ultra|s))?',
    r'(?:RTX|GTX|RX)\s*\d{3,4}(?:\s*(?:Ti\s*SUPER|Ti|SUPER|XTX|XT))?',
    r'(?:Intel\s*)?i[3579]-?\s*\d{4,5}[A-Z]{0,3}',
    r'(?:Ryzen\s*[3579]?\s*|锐龙\s*)?\d{4}(?:X3D|X|G|F)',
    r'MacBook\s*(?:Air|Pro)\s*(?:\d{2}(?:\.\d)?\s*(?:英寸|寸)?\s*)?(?:M[1-9](?:\s*(?:Pro|Max))?|20\d{2})',
    r'Mac\s*mini\s*(?:M[1-9](?:\s*Pro)?|20\d{2})',
    r'ThinkPad\s*[TEXPL]\d{2,3}(?:\s*(?:Gen\s*\d|20\d{2}))?',
    r'(?:拯救者|Legion)\s*[YR]\d{3,4}[A-Z]{0,2}(?:\s*20\d{2})?',
    r'iPad\s*(?:Pro\s*(?:M[1-9]|20\d{2})|(?:Air|mini)\s*(?:M[1-9]|20\d{2}|[1-9](?:\s*代)?))',
    r'Apple\s*Watch\s*(?:(?:Series\s*|S)\d{1,2}|Ultra\s*\d|SE\s*[123])',
    r'AirPods\s*(?:Pro\s*[123]|Max|[234])',
    r'(?:WH|WF)-?1000XM\d',
    r'(?:罗技\s*|Logitech\s*)?(?:G\s*\d{2,4}|MX\s*(?:Master|Anywhere)\s*\dS?|GPW\s*[123])',
    r'(?:雷蛇\s*)?(?:毒蝰|炼狱蝰蛇|巴塞利斯蛇)\s*(?:V\d|\d)(?:\s*Pro)?',
    r'(?:Sony\s*|索尼\s*)?A(?:[179]|6[0134567]00)(?:CR|R|C|S)?(?:\s*(?:III|II|IV|V|M\d))?',
    r'(?:Canon\s*|佳能\s*)?EOS\s*R\d{1,2}(?:\s*(?:Mark\s*II|II))?',
    r'(?:Nikon\s*|尼康\s*)Z\s*\d{1,2}(?:\s*(?:III|II))?',
)
MODELS = tuple(re.compile(r'(?<![A-Za-z0-9])(?:'+pattern+r')(?![A-Za-z0-9])', re.I)
               for pattern in MODEL_PATTERNS)
ENTITLEMENTS = (
    ('京东家政', r'京东\s*(?:家政|保洁)'),
    ('天鹅到家保洁', r'天鹅到家.{0,6}(?:保洁|家政)'),
    ('京东E卡', r'京东\s*(?:E卡|e卡|礼品卡)|狗东\s*(?:E卡|卡)'),
    ('ChatGPT Plus', r'(?:ChatGPT|GPT)\s*Plus'),
    ('ChatGPT Pro', r'(?:ChatGPT|GPT)\s*Pro'),
    ('Claude Pro', r'Claude\s*Pro'),
    ('Claude Max', r'Claude\s*Max'),
    ('Apple Music', r'Apple\s*Music'),
    ('Spotify Premium', r'Spotify\s*(?:Premium|会员)'),
)
REGIONS = re.compile(r'北京|上海|天津|重庆|广州|深圳|杭州|南京|苏州|成都|武汉|西安|长沙|郑州|济南|青岛|合肥|厦门|福州|全国|帝都|魔都')
REGION_ALIASES = {'帝都':'北京','魔都':'上海'}
SPEC_NUMBERS={'ram_gb','storage_gb','vram_gb','size_mm','screen_tenths','hours'}
SPEC_TEXT={'network':{'wifi','cellular'}}


def normalize(value: str) -> str:
    value = re.sub(r'Xiaomi', '小米', value, flags=re.I)
    value = re.sub(r'红米', 'Redmi', value)
    return re.sub(r'[\s_-]+', '', value).casefold()


def descriptor(text: str, category: str) -> dict | None:
    """Return one unambiguous identity; broad/alternative requests stay unselected."""
    category = canonical(category)
    if category not in CATEGORIES:
        return None
    if re.search(r'(?<=[A-Za-z0-9])\s*[/~～]\s*(?=[A-Za-z0-9])|或者|或是|或|二选一',text):
        return None
    from .classifier import category_for
    identities = {}
    if category.startswith('virtual.'):
        for label, pattern in ENTITLEMENTS:
            if re.search(pattern,text,re.I) and canonical(category_for(label)[0])==category:
                identities[normalize(label)] = label
    else:
        for pattern in MODELS:
            for match in pattern.finditer(text):
                label = re.sub(r'\s+', ' ', match[0]).strip()
                if canonical(category_for(label)[0]) == category:
                    identities[normalize(label)] = label
    if len(identities) != 1:
        return None
    key,label = next(iter(identities.items()))
    specs = {}
    # Model suffixes such as Ryzen 5600G are never storage capacities.
    specification_text=text
    for pattern in MODELS:
        specification_text=pattern.sub(' ',specification_text)
    capacities = set()
    for number, unit in re.findall(r'(?<![\d.A-Za-z])(\d+)\s*(TB|GB|T|G)(?![A-Za-z])',specification_text,re.I):
        value = int(number)*(1024 if unit.upper().startswith('T') else 1)
        capacities.add(value)
    # A RAM+storage pair is handled separately; conflicting storage choices
    # must not silently narrow a request to a model the owner happens to have.
    pair = re.search(r'(?<!\d)(\d{1,3})\s*(?:G[Bb]?)?\s*\+\s*(\d{2,4})\s*G',specification_text,re.I)
    if pair:
        specs.update(ram_gb=int(pair[1]),storage_gb=int(pair[2]))
    elif len(capacities)==1:
        size=next(iter(capacities))
        if category=='component.gpu':
            specs['vram_gb']=size
        elif category.startswith('component.memory'):
            specs['ram_gb']=size
        elif category.startswith(('phone.','tablet.','computer.')):
            specs['ram_gb' if size<=32 else 'storage_gb']=size
        elif category.startswith('storage.'):
            specs['storage_gb']=size
    elif len(capacities)>1:
        return None
    sizes = set(re.findall(r'(?<!\d)(\d{2})\s*mm\b',text,re.I))
    if len(sizes)>1:
        return None
    if sizes:
        specs['size_mm'] = int(next(iter(sizes)))
    screens={float(n) for n in re.findall(r'(?<![\d.])(\d{1,2}(?:\.\d)?)\s*(?:英寸|寸)',text)}
    if len(screens)>1:
        return None
    if screens:
        specs['screen_tenths']=int(round(screens.pop()*10))
    if category.startswith(('tablet.','wearable.')):
        cellular=bool(re.search(r'蜂窝|插卡|Cellular|LTE|4G版|5G版',text,re.I))
        wifi=bool(re.search(r'Wi[ -]?Fi\s*版|仅Wi[ -]?Fi',text,re.I))
        if cellular and wifi:
            return None
        if cellular or wifi:
            specs['network']='cellular' if cellular else 'wifi'
    durations = set(re.findall(r'(?<!\d)(\d{1,2})\s*(?:小时|小時)',text))
    if len(durations)>1:
        return None
    if durations:
        specs['hours'] = int(next(iter(durations)))
    region = ''
    if key in {normalize('京东家政'),normalize('天鹅到家保洁')}:
        regions={REGION_ALIASES.get(r,r) for r in REGIONS.findall(text)}
        if len(regions)!=1:
            return None
        region=regions.pop()
    return {'category':category,'model':label,'model_key':key,'specs':specs,'region':region}


def validate(value: dict) -> dict:
    if (not isinstance(value,dict) or not isinstance(value.get('version'),int)
            or value['version']<1 or type(value.get('enabled')) is not bool
            or not isinstance(value.get('targets'),list) or len(value['targets'])>200):
        raise ValueError('invalid wanted policy')
    blocked=value.get('blocked_groups',[])
    if not isinstance(blocked,list) or any(key not in GROUPS for key in blocked):
        raise ValueError('invalid wanted blocked groups')
    for target in value['targets']:
        if not isinstance(target,dict) or target.get('category') not in CATEGORIES:
            raise ValueError('wanted target must name a known category and specific model')
        model=target.get('model')
        region=target.get('region','')
        if not isinstance(model,str) or len(model)>100 or not isinstance(region,str):
            raise ValueError('invalid wanted model')
        parsed=descriptor(model+' '+region,target['category'])
        if not parsed or parsed['model_key']!=normalize(model) or parsed['region']!=region:
            raise ValueError('wanted target needs an exact supported model or regional entitlement')
        specs=target.get('specs',{})
        if not isinstance(specs,dict) or set(specs)-SPEC_NUMBERS-SPEC_TEXT.keys():
            raise ValueError('invalid wanted specifications')
        if any(type(number) is not int or number<=0 or number>65536 for key,number in specs.items() if key in SPEC_NUMBERS):
            raise ValueError('invalid wanted specification value')
        if any(number not in SPEC_TEXT[key] for key,number in specs.items() if key in SPEC_TEXT):
            raise ValueError('invalid wanted specification value')
    return value


def load(path: Path | None = None) -> dict:
    if path is None:
        from .control import current
        active=current()
        if active:return active[1]['wanted']
    path=path or home()/'config/marketplace-wanted.json'
    return validate(json.loads(path.read_text())) if path.is_file() else {'version':1,'enabled':False,'targets':[]}


def evaluate(items: list[dict], policy: dict) -> list[dict]:
    if not policy.get('enabled'):
        return []
    result=[]
    for item in items:
        attrs=item['attributes']
        if (item['status']!='buy' or attrs.get('intent')!='wanted'
                or not attrs.get('body_complete')
                or group_for(item['category']) in policy.get('blocked_groups',[])
                or attrs.get('excluded_device')
                or attrs.get('transaction_scope') in {'non_beijing_local_only','unknown_region_local_only'}):
            continue
        identity=descriptor(item['text'],item['category'])
        if not identity:
            continue
        for target in policy.get('targets',[]):
            if (canonical(target['category'])!=identity['category']
                    or normalize(target['model'])!=identity['model_key']
                    or target.get('region','')!=identity['region']):
                continue
            if any(target.get('specs',{}).get(key)!=number for key,number in identity['specs'].items()):
                continue
            from .policy import attribute_decision
            if attribute_decision(item,policy)[0]!='match':continue
            result.append(item)
            break
    return result


def catalog(items: list[dict]) -> list[dict]:
    """Public product identities only; no message text, contacts or post URLs."""
    return [identity for item in items if item['status']=='buy' and item['attributes'].get('intent')=='wanted'
            for identity in [descriptor(item['text'],item['category'])] if identity]
