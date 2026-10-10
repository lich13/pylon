from __future__ import annotations

import copy
from pylon.tasks.linuxdo import policy as linux
from pylon.config import profile_root

SOURCES={'linuxdo':'Linux.do','v2ex':'V2EX','xianbao':'线报'}
GROUPS={**linux.GROUPS,'I':'银行支付与日常缴费'}
CATALOG={**linux.CATEGORIES,'I1':'银行与支付权益','I2':'话费与水电缴费','I3':'交通与日常消费福利'}


def default():
    focus=linux.load(profile_root('webops')/'config/linuxdo_policy.json')
    allowed=linux.allowed_categories(focus)
    scopes={key:['v2ex']+(['linuxdo'] if key in allowed else []) for key in CATALOG}
    for key in ('G1','G2','I1','I2','I3'):scopes[key].append('xianbao')
    # These are refinements of the already selected practical welfare scope.
    if allowed & {'G1','G2'}:
        for key in ('I1','I2','I3'):scopes[key].append('linuxdo')
    return {'version':1,'enabled':True,'categories':list(CATALOG),'blocked_groups':[],
            'category_sources':scopes,'exclude_terms':{},'include_terms':{},
            'linuxdo_focus':focus,'value_mode':'evidenced_lead'}


def validate(value):
    if not isinstance(value,dict) or set(value)!={'version','enabled','categories','blocked_groups','category_sources','exclude_terms','include_terms','linuxdo_focus','value_mode'}:raise ValueError('情报策略字段无效')
    if value['version']!=1 or type(value['enabled']) is not bool or value['value_mode']!='evidenced_lead':raise ValueError('情报策略版本无效')
    for field,choices in (('categories',CATALOG),('blocked_groups',GROUPS)):
        if not isinstance(value[field],list) or any(v not in choices for v in value[field]):raise ValueError('情报分类无效')
    if not isinstance(value['category_sources'],dict) or set(value['category_sources'])!=set(CATALOG):raise ValueError('情报来源范围无效')
    for values in value['category_sources'].values():
        if not isinstance(values,list) or any(v not in SOURCES for v in values):raise ValueError('未知来源')
    for field in ('exclude_terms','include_terms'):
        if not isinstance(value[field],dict):raise ValueError('筛选条件无效')
        for key,values in value[field].items():
            if key not in {'*',*CATALOG,*GROUPS} or not isinstance(values,list) or len(values)>100 or any(not isinstance(v,str) or not 1<=len(v.strip())<=80 for v in values):raise ValueError('关键词条件无效')
    linux.validate(value['linuxdo_focus'])
    return copy.deepcopy(value)


def allowed(config,source):
    return {k for k in config['categories'] if k[0] not in config['blocked_groups'] and source in config['category_sources'][k]}


def model_focus(config,source):
    result=copy.deepcopy(config['linuxdo_focus'])
    result.update(groups=[],categories=sorted(allowed(config,source)&set(linux.CATEGORIES)),excluded_categories=[])
    return result


def decide(item,result,config):
    if not config['enabled']:return 'skip','情报推送未启用'
    if result.get('content_type') in {'question','chatter','advertisement'}:return 'skip','没有明确可用资源或方法'
    permitted=allowed(config,item['source'])
    identified=set(result.get('categories',[]))
    categories=identified & permitted
    if not categories and not identified and permitted and result.get('decision')=='hold':
        return 'hold',result.get('reason','缺少明确价值信息')
    if not categories:return 'skip','不在已选情报范围'
    facts=result.get('facts',[])
    # Match the supported content, never a quoted gift, comparison or sibling.
    text='\n'.join(str(f.get('subject',''))+' '+str(f.get('value','')) for f in facts if f.get('category') in categories).casefold()
    if not text:text=(item['title']+'\n'+item['body']).casefold()
    keys={'*',*categories,*(k[0] for k in categories)}
    if any(v.casefold() in text for k in keys for v in config['exclude_terms'].get(k,[])):return 'skip','命中排除条件'
    for k in keys:
        terms=config['include_terms'].get(k,[])
        if terms and not any(v.casefold() in text for v in terms):return 'skip','不符合关注关键词'
    return result.get('decision','hold'),result.get('reason','缺少明确价值信息')
