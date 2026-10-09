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
    for field,allowed in (('groups',GROUPS),('blocked_groups',GROUPS),('types',types)):
        selection=value.get(field,[])
        if not isinstance(selection,list) or any(not isinstance(key,str) or key not in allowed for key in selection):
            raise ValueError('marketplace policy contains invalid '+field)
    return value


def selected(category: str, policy: dict) -> bool:
    category=canonical(category)
    group=group_for(category)
    if group in policy.get('blocked_groups',[]) or category in policy.get('blocked_categories',[]):
        return False
    return (group in policy.get('groups',[]) or type_for(category) in policy.get('types',[])
            or category in {canonical(key) for key in policy.get('categories',[])})


def effective(policy: dict) -> list[str]:
    return [key for key in CATEGORIES if selected(key,policy)] if policy.get('enabled') else []


def attribute_decision(item: dict, policy: dict) -> tuple[str,list[str]]:
    from .subjects import FIELDS
    attrs=item.get('attributes',{});facts=attrs.get('facts',{}).copy()
    if item.get('price') is not None:facts['price']=item['price']
    missing=[]
    rules=policy.get('filters',{}).get(item['category'],{})
    for field,rule in rules.items():
        if field not in FIELDS:raise ValueError('不支持的属性条件')
        if not rule.get('include') and not rule.get('exclude') and 'min' not in rule and 'max' not in rule:continue
        actual=facts.get(field)
        if actual in (None,'',[]):
            missing.append(field);continue
        values=actual if isinstance(actual,list) else [actual]
        norm=lambda value: ''.join(str(value).casefold().split())
        values={norm(v) for v in values}
        excluded={norm(v) for v in rule.get('exclude',[])}
        if values & excluded:return 'exclude',[field+':排除条件']
        allowed={norm(v) for v in rule.get('include',[])}
        if allowed and not values & allowed:return 'exclude',[field+':不符合关注条件']
        if 'min' in rule or 'max' in rule:
            if type(actual) not in (int,float):missing.append(field);continue
            if actual < rule.get('min',actual) or actual > rule.get('max',actual):
                return 'exclude',[field+':不在范围']
    return ('review',sorted(set(missing))) if missing else ('match',[])


def decision(item: dict, policy: dict) -> tuple[str,list[str]]:
    attrs=item['attributes']
    if not policy.get('enabled'):return 'exclude',['出售推送未启用']
    if group_for(item['category']) in policy.get('blocked_groups',[]):return 'exclude',['大类已屏蔽']
    if item['category'] not in CATEGORIES and item['category'] not in policy.get('extension_categories',[]):return 'review',['类别未确认']
    if not selected(item['category'],policy):return 'exclude',['未关注此类别']
    if item['status']!='available':return 'exclude',['不是在售商品']
    if attrs.get('excluded_device'):return 'exclude',['设备限制']
    if attrs.get('transaction_scope') in {'non_beijing_local_only','unknown_region_local_only'}:
        return 'exclude',['仅限非北京本地或地区未明']
    if attrs.get('pickup_only') and not (attrs.get('beijing') and policy.get('allow_beijing_pickup',True)):
        return 'exclude',['自提地区不符合']
    return attribute_decision(item,policy)


def evaluate(items: list[dict], policy: dict) -> list[dict]:
    return [item for item in items if decision(item,policy)[0]=='match']


def validate_filters(filters):
    from .subjects import FIELDS
    if not isinstance(filters,dict):raise ValueError('属性条件必须是对象')
    for category,rules in filters.items():
        if category not in CATEGORIES or not isinstance(rules,dict):raise ValueError('类别无效')
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
