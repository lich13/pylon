"""Explicit opt-in policy. An absent policy never silently enables a new feed."""
import json
from pathlib import Path
from pylon.config import home
from .taxonomy import CATEGORIES, GROUPS, canonical, group_for, type_for, hierarchy


def load(path: Path | None = None) -> dict:
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
    if group in policy.get('blocked_groups',[]):
        return False
    return (group in policy.get('groups',[]) or type_for(category) in policy.get('types',[])
            or category in {canonical(key) for key in policy.get('categories',[])})


def effective(policy: dict) -> list[str]:
    return [key for key in CATEGORIES if selected(key,policy)] if policy.get('enabled') else []


def evaluate(items: list[dict], policy: dict) -> list[dict]:
    if not policy.get('enabled'):
        return []
    matches = []
    for item in items:
        attrs = item['attributes']
        if item['category'] not in CATEGORIES or not selected(item['category'],policy) or item['status'] != 'available' or attrs.get('excluded_device'):
            continue
        if attrs.get('transaction_scope') in {'non_beijing_local_only','unknown_region_local_only'}:
            continue
        if attrs.get('pickup_only') and not (attrs.get('beijing') and policy.get('allow_beijing_pickup', True)):
            continue
        matches.append(item)
    return matches
