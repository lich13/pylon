"""Explicit opt-in policy. An absent policy never silently enables a new feed."""
import json
from pathlib import Path
from pylon.config import home
from .taxonomy import CATEGORIES, GROUPS, canonical, group_for, type_for, hierarchy


def load(path: Path | None = None) -> dict:
    if path is None:
        from .control import current
        active=current()
        if active:return active[1]['sale']
    path = path or home() / 'config/marketplace.json'
    if not path.is_file():
        return {'version': 1, 'enabled': False, 'categories': []}
    value = json.loads(path.read_text())
    if not isinstance(value.get('version'), int) or value['version'] < 1:
        raise ValueError('marketplace policy version is invalid')
    if not isinstance(value.get('enabled'), bool) or not isinstance(value.get('categories'), list):
        raise ValueError('marketplace policy must explicitly declare enabled and categories')
    value['categories']=[canonical(key) for key in value['categories']]
    if set(value['categories']) - CATEGORIES.keys():
        raise ValueError('marketplace policy contains unknown categories')
    types={key for group in hierarchy().values() for key in group['types']}
    for field,allowed in (('groups',GROUPS),('blocked_groups',GROUPS),('types',types),('blocked_types',types),('partial_types',types)):
        selection=value.get(field,[])
        if not isinstance(selection,list) or any(not isinstance(key,str) or key not in allowed for key in selection):
            raise ValueError('marketplace policy contains invalid '+field)
    return value


def selected(category: str, policy: dict) -> bool:
    category=canonical(category)
    group=group_for(category)
    if group in policy.get('blocked_groups',[]) or type_for(category) in policy.get('blocked_types',[]) or category in policy.get('blocked_categories',[]):
        return False
    if type_for(category) in policy.get('partial_types',[]):
        return category in {canonical(key) for key in policy.get('categories',[])}
    return (group in policy.get('groups',[]) or type_for(category) in policy.get('types',[])
            or category in {canonical(key) for key in policy.get('categories',[])})


def effective(policy: dict) -> list[str]:
    return [key for key in CATEGORIES if selected(key,policy)] if policy.get('enabled') else []


def attribute_decision(item: dict, policy: dict) -> tuple[str,list[str]]:
    from .subjects import FIELDS
    attrs=item.get('attributes',{});facts=attrs.get('facts',{}).copy()
    if item.get('price') is not None:facts['price']=item['price']
    missing=[]
    selectors=dict.fromkeys(['*',group_for(item['category']),type_for(item['category']),item['category']])
    rules=[(field,rule) for selector in selectors for field,rule in policy.get('filters',{}).get(selector,{}).items()]
    from .entities import normalize, text_matches
    for field,rule in rules:
        if field not in FIELDS:raise ValueError('不支持的属性条件')
        if not rule.get('include') and not rule.get('exclude') and 'min' not in rule and 'max' not in rule:continue
        actual=facts.get(field)
        if actual in (None,'',[]):
            # Only positively evidenced provenance is excluded. Its absence
            # does not make every unrelated entitlement an unresolved item.
            if field=='origin' and rule.get('exclude') and not rule.get('include'):continue
            missing.append(field);continue
        values=actual if isinstance(actual,list) else [actual]
        values={normalize(v,field) for v in values}
        matches=lambda choices: any(text_matches(v,c,field) for v in values for c in choices)
        if matches(rule.get('exclude',[])):return 'exclude',[field+':排除条件']
        allowed=rule.get('include',[])
        if allowed and not matches(allowed):return 'exclude',[field+':不符合关注条件']
        if 'min' in rule or 'max' in rule:
            if type(actual) not in (int,float):missing.append(field);continue
            if actual < rule.get('min',actual) or actual > rule.get('max',actual):
                return 'exclude',[field+':不在范围']
    return ('review',sorted(set(missing))) if missing else ('match',[])


def decision(item: dict, policy: dict) -> tuple[str,list[str]]:
    attrs=item['attributes']
    if not policy.get('enabled'):return 'exclude',['出售推送未启用']
    if item['status'] in {'sold','buy'} or attrs.get('intent') in {'inquiry','discussion'}:
        return 'exclude',['不是在售商品']
    if group_for(item['category']) in policy.get('blocked_groups',[]):return 'exclude',['大类已屏蔽']
    if item['category'] not in CATEGORIES and item['category'] not in policy.get('extension_categories',[]):return 'review',['类别未确认']
    if not selected(item['category'],policy):return 'exclude',['未关注此类别']
    if attrs.get('trade_intent_pending'):return 'review',['交易意图尚待确认']
    if item['status']!='available':return 'exclude',['不是在售商品']
    if attrs.get('excluded_device'):return 'exclude',['设备限制']
    if attrs.get('transaction_scope')=='unknown_region_local_only':return 'review',['region']
    if attrs.get('transaction_scope')=='non_beijing_local_only':return 'exclude',['仅限非北京本地']
    if attrs.get('pickup_only') and not (attrs.get('beijing') and policy.get('allow_beijing_pickup',True)):
        return 'exclude',['自提地区不符合']
    state,reasons=attribute_decision(item,policy)
    if state!='match':return state,reasons
    result=attrs.get('ai_price_result')
    if result and result['decision']!='push':
        return ('review' if result['decision']=='hold' else 'exclude'),[result['reason']]
    return state,reasons


def evaluate(items: list[dict], policy: dict) -> list[dict]:
    return [item for item in items if decision(item,policy)[0]=='match']


def validate_filters(filters,*,categories=None):
    from .subjects import FIELDS
    if not isinstance(filters,dict):raise ValueError('属性条件必须是对象')
    for category,rules in filters.items():
        catalog=CATEGORIES if categories is None else categories
        allowed=set(catalog)|set(GROUPS)|{type_for(k) for k in catalog}|{'*'}
        if category not in allowed or not isinstance(rules,dict):raise ValueError('类别无效')
        for field,rule in rules.items():
            if field not in FIELDS or not isinstance(rule,dict) or set(rule)-{'include','exclude','min','max'}:
                raise ValueError('属性条件无效')
            for key in ('include','exclude'):
                values=rule.get(key,[])
                if not isinstance(values,list) or len(values)>30 or any(not isinstance(v,(str,int,float)) or len(str(v))>80 for v in values):
                    raise ValueError('属性选项无效')
            for key in ('min','max'):
                if key in rule and (type(rule[key]) not in (int,float) or not 0<=rule[key]<=1000000):raise ValueError('范围无效')
            if rule.get('min',0)>rule.get('max',1000000):raise ValueError('范围下限高于上限')
    return filters
