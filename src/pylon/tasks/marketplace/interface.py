"""One owner-only Telegram workspace for drafts, reviews, and explanations."""
from __future__ import annotations

import hmac
import json
import re
import secrets
import sqlite3
import time
from pathlib import Path

from pylon.config import home
from pylon.inline import owns_callback
from pylon.tasks.gmail.review import _signature
from . import control,policy,wanted,taxonomy
from .store import Store
from .subjects import FIELDS, applicable_fields


def card(text,rows=()):
    return {'text':text,'reply_markup':{'inline_keyboard':list(rows)}}


def button(db,label,action,payload,version):
    token=secrets.token_hex(8)
    db.execute('INSERT INTO market_buttons VALUES(?,?,?,?,?,NULL,?)',
               (token,'webops',action,control.dumps(payload),version,time.time()))
    data='mk1|'+token+'|'+_signature('webops','market',token)
    return {'text':label,'callback_data':data}


def _b(db,version,label,action,**payload):
    return button(db,label,action,payload,version)


def draft(db,identifier=None):
    version,value=control.baseline(db)
    row=(db.execute('SELECT * FROM market_drafts WHERE id=?',(identifier,)).fetchone() if identifier else
         db.execute("SELECT * FROM market_drafts WHERE profile='webops' AND status='draft' AND base=? ORDER BY updated_at DESC LIMIT 1",(version,)).fetchone())
    if row:
        result=dict(row);result['content']=json.loads(row['content']);return result
    key=secrets.token_hex(8)
    db.execute("INSERT INTO market_drafts(id,profile,base,content,updated_at) VALUES(?,'webops',?,?,?)",
               (key,version,control.dumps(value),time.time()))
    return draft(db,key)


def save(db,d):
    db.execute('UPDATE market_drafts SET content=?,revision=revision+1,preview=NULL,updated_at=? WHERE id=? AND status=\'draft\'',
               (control.dumps(d['content']),time.time(),d['id']))


def home_card(db):
    version,value=control.baseline(db)
    active=taxonomy.catalog(value.get('taxonomy',{}))
    count=len([k for k in active if policy.selected(k,value['sale'])])
    pending=db.execute("SELECT COUNT(*) FROM market_novelty WHERE status='pending'").fetchone()[0]
    check=db.execute("SELECT COUNT(*) FROM market_reviews WHERE status='pending'").fetchone()[0]
    rows=[[_b(db,version,'出售筛选','groups',scope='sale'),_b(db,version,'求购目标','wanted')],
          [_b(db,version,f'新品种待确认 {pending}','novelty'),_b(db,version,f'待核对 {check}','reviews')],
          [_b(db,version,'检查帖子','check'),_b(db,version,'草稿与预览','preview')],
          [_b(db,version,'恢复上一版本','rollback_preview')]]
    return card(f'交易关注 · 版本 {version}\n出售关注 {count} 个细类；求购目标 {len(value["wanted"].get("targets",[]))} 个。\n'
                '先在草稿中选择，预览并确认后生效。未关注的种类不推送，大类屏蔽优先。',rows)


def _back(db,v):return [_b(db,v,'返回交易管理','home')]


def groups_card(db,d):
    v=d['base'];value=d['content']['sale'];rows=[]
    for group,label in taxonomy.GROUPS.items():
        state='屏蔽' if group in value.get('blocked_groups',[]) else '全部关注' if group in value.get('groups',[]) else '细选'
        rows.append([_b(db,v,label+' · '+state,'group',draft=d['id'],group=group)])
    rows.append([_b(db,v,'预览所选','preview',draft=d['id'])]);rows.append(_back(db,v))
    return card('出售筛选 · 点击大类后可批量关注、屏蔽或只选其中细类。',rows)


def group_card(db,d,group,page=0):
    v=d['base'];value=d['content']['sale'];catalog=taxonomy.catalog(d['content'].get('taxonomy',{}));keys=[k for k in catalog if taxonomy.group_for(k)==group]
    rows=[[_b(db,v,'关注整个大类','group_set',draft=d['id'],group=group,mode='all'),
           _b(db,v,'屏蔽整个大类','group_set',draft=d['id'],group=group,mode='block')],
          [_b(db,v,'只关注勾选细类','group_set',draft=d['id'],group=group,mode='partial')]]
    for key in keys[page*12:(page+1)*12]:
        selected=policy.selected(key,value)
        rows.append([_b(db,v,('✓ ' if selected else '□ ')+catalog[key].split('/')[-1],'toggle',draft=d['id'],category=key),
                     _b(db,v,'属性条件','fields',draft=d['id'],category=key)])
    if page:rows.append([_b(db,v,'上一页','group',draft=d['id'],group=group,page=page-1)])
    if (page+1)*12<len(keys):rows.append([_b(db,v,'下一页','group',draft=d['id'],group=group,page=page+1)])
    rows.append([_b(db,v,'预览影响','preview',draft=d['id']),_b(db,v,'返回大类','groups',draft=d['id'])])
    blocked=group in value.get('blocked_groups',[])
    return card(taxonomy.GROUPS[group]+' · 草稿\n'+('整个大类已屏蔽；勾选细类也不能绕过。先选“只关注勾选细类”解除。' if blocked else '可连续勾选多个类别，再统一预览。'),rows)


def item_rows(db):
    for row in db.execute('SELECT source,source_id,url,title,items,updated_at,body_complete FROM marketplace_posts ORDER BY first_seen DESC'):
        yield dict(row)
    path=home()/'data/marketplace-review.sqlite3'
    if path.exists():
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True) as history:
            history.row_factory=sqlite3.Row
            for row in history.execute('SELECT source,source_id,url,title,items,updated_at,body_complete FROM marketplace_posts WHERE history=1'):
                yield dict(row)


def options(db,category,field):
    values={};total=known=0;seen=set();samples=[]
    for row in item_rows(db):
        key=(row['source'],row['source_id'])
        if key in seen:continue
        seen.add(key)
        for item in json.loads(row['items']):
            if item['category']!=category or item['status'] not in {'available','sold'}:continue
            total+=1
            value=item['attributes'].get('facts',{}).get(field)
            if value in (None,'',[]):continue
            known+=1
            for option in value if isinstance(value,list) else [value]:
                option=str(option)
                if len(option)<=80:values[option]=values.get(option,0)+1
            if len(samples)<3:samples.append(row['url'])
    return sorted(values,key=lambda k:(-values[k],k))[:24],total,known,samples


def fields_card(db,d,category,scope='sale'):
    rules=d['content'][scope].get('filters',{}).get(category,{})
    rows=[[_b(db,d['base'],FIELDS[key]+(' ✓' if key in rules else ''),'field',draft=d['id'],category=category,field=key,scope=scope)] for key in applicable_fields(category)]
    rows.append([_b(db,d['base'],'返回求购' if scope=='wanted' else '返回类别','wanted' if scope=='wanted' else 'group',draft=d['id'],group=taxonomy.group_for(category))])
    return card(taxonomy.catalog(d['content'].get('taxonomy',{}))[category]+'\n同一字段多选满足其一；不同字段需同时满足，排除优先。\n'+
                ('当前草稿条件：\n'+describe_filters(rules) if rules else '当前没有属性门槛。'),rows)


def describe_filters(rules):
    result=[]
    for key,r in rules.items():
        parts=[]
        if r.get('include'):parts.append('包含 '+'、'.join(map(str,r['include'])))
        if r.get('exclude'):parts.append('排除 '+'、'.join(map(str,r['exclude'])))
        if 'min' in r:parts.append('最低 '+str(r['min']))
        if 'max' in r:parts.append('最高 '+str(r['max']))
        result.append(FIELDS[key]+'：'+'；'.join(parts))
    return '\n'.join(result)


def field_card(db,d,category,field,scope='sale'):
    choices,total,known,samples=options(db,category,field)
    rule=d['content'][scope].get('filters',{}).get(category,{}).get(field,{})
    rows=[]
    for value in choices:
        rows.append([_b(db,d['base'],('✓ ' if value in rule.get('include',[]) else '□ ')+value,'value',draft=d['id'],category=category,field=field,scope=scope,value=value,mode='include'),
                     _b(db,d['base'],('已排除' if value in rule.get('exclude',[]) else '排除'),'value',draft=d['id'],category=category,field=field,scope=scope,value=value,mode='exclude')])
    rows.extend([[_b(db,d['base'],'输入指定值／范围','input',draft=d['id'],category=category,field=field,scope=scope),
                  _b(db,d['base'],'清除此字段限制','clear_field',draft=d['id'],category=category,field=field,scope=scope)],
                 [_b(db,d['base'],'返回属性','fields',draft=d['id'],category=category,scope=scope),_b(db,d['base'],'预览','preview',draft=d['id'])]])
    return card(FIELDS[field]+f' · 历史 {total} 件，明确记录 {known} 件，缺失 {total-known} 件。\n'
                '缺少受限字段时先分析原文，仍不明则暂缓到待核对。\n'+describe_filters({field:rule})+
                ('\n典型记录：\n'+'\n'.join(samples) if samples else ''),rows)


def preview(db,d):
    counts={'match':0,'exclude':0,'review':0};examples={k:[] for k in counts};seen=set();fingerprints=[];complete=0
    for row in item_rows(db):
        identity=(row['source'],row['source_id'])
        if identity in seen:continue
        seen.add(identity);complete+=int(bool(row['body_complete']));fingerprints.append([*identity,row['updated_at'],control.digest(row['items'])])
        for item in json.loads(row['items']):
            draft_catalog=taxonomy.catalog(d['content'].get('taxonomy',{}))
            if item['category']=='unknown':
                owned=__import__('pylon.tasks.marketplace.subjects',fromlist=['subject_text']).subject_text(item['text'])
                matches={v.get('target') or k for k,v in d['content'].get('taxonomy',{}).items() if any(t in owned for t in v.get('terms',[]))}
                if len(matches)==1:item['category']=matches.pop()
            if item['status']=='buy':
                eligible=wanted.evaluate([item],{**d['content']['wanted'],'filters':{}})
                state=policy.attribute_decision(item,d['content']['wanted'])[0] if eligible else 'exclude'
            else:state=policy.decision(item,{**d['content']['sale'],'extension_categories':list(draft_catalog)})[0]
            counts[state]+=1
            if len(examples[state])<3:examples[state].append(row['url'])
    return {'counts':counts,'examples':examples,'posts':len(seen),'complete':complete,'missing':len(seen)-complete,'hash':control.digest([d['content'],fingerprints]),'at':time.time()}


def preview_card(db,d):
    p=preview(db,d)
    db.execute('UPDATE market_drafts SET preview=? WHERE id=?',(control.dumps(p),d['id']))
    sale=d['content']['sale'];catalog=taxonomy.catalog(d['content'].get('taxonomy',{}));follow=[catalog[k].split('/')[-1] for k in catalog if policy.selected(k,sale)]
    text=f'草稿预览 · {p["posts"]} 帖（完整 {p["complete"]}，缺正文 {p["missing"]}）\n'
    text+='关注：'+('、'.join(follow) or '无')+'\n屏蔽大类：'+('、'.join(taxonomy.GROUPS[k] for k in sale.get('blocked_groups',[])) or '无')
    for cat,rules in sale.get('filters',{}).items():
        if rules:text+='\n'+catalog[cat]+'\n'+describe_filters(rules)
    for state,label in (('match','保留'),('exclude','排除'),('review','待核对')):
        text+=f'\n{label} {p["counts"][state]} 件'
        if p['examples'][state]:text+='\n'+'\n'.join(p['examples'][state])
    changes=d['content'].get('review_actions',{})
    if changes:
        text+='\n新品种处理：'+'、'.join((db.execute('SELECT kind FROM market_novelty WHERE id=?',(k,)).fetchone() or [k])[0]+' → '+{'classified':'归类','ignored':'忽略','later':'稍后'}.get(v,v) for k,v in changes.items())
    for cat,rules in d['content']['wanted'].get('filters',{}).items():
        if rules:text+='\n求购条件 '+catalog[cat]+'\n'+describe_filters(rules)
    text+='\n求购目标：'+('、'.join(t['model']+' '+t.get('region','')+' '+str(t.get('specs') or '') for t in d['content']['wanted'].get('targets',[])) or '无')
    text+='\n仅影响后续处理；历史样本不会补发。'
    return card(text,[[_b(db,d['base'],'确认应用','apply',draft=d['id'],revision=d['revision'],hash=p['hash']),
                       _b(db,d['base'],'继续调整','groups',draft=d['id'])],_back(db,d['base'])])


def wanted_card(db,d):
    targets=d['content']['wanted'].get('targets',[]);rows=[]
    for i,t in enumerate(targets):
        label=t['model']+' '+t.get('region','')+' '+str(t.get('specs') or '')
        rows.append([_b(db,d['base'],'移除：'+label[:55],'wanted_remove',draft=d['id'],target_hash=control.digest(t))])
    for category in dict.fromkeys(t['category'] for t in targets):
        rows.append([_b(db,d['base'],'求购条件：'+taxonomy.CATEGORIES[category].split('/')[-1],'fields',draft=d['id'],category=category,scope='wanted')])
    rows.extend([[_b(db,d['base'],'从实际求购记录选择','wanted_options',draft=d['id']),
                  _b(db,d['base'],'输入具体求购目标','wanted_input',draft=d['id'])],
                 [_b(db,d['base'],'预览并确认','preview',draft=d['id'])],_back(db,d['base'])])
    return card('求购目标 · 独立草稿\n只匹配你设置的具体型号／权益及规格；出售关注不会自动成为求购目标。',rows)


def novel_card(db,d,page=0):
    rows=db.execute("SELECT * FROM market_novelty WHERE status IN ('pending','later') ORDER BY first_seen LIMIT 8 OFFSET ?",(page*8,)).fetchall()
    keys=[];buttons=[];lines=['新品种待确认 · 选择处理方式后统一预览确认。']
    for row in rows:
        proposal=json.loads(row['suggestion']);action=d['content'].get('review_actions',{}).get(row['id'],'未选择')
        keys.append(row['id']);lines.append(f"\n{row['kind']}（{taxonomy.GROUPS[row['parent']]}）· {action}\n建议依据：{proposal['evidence'][:300]}\n区别：{proposal['difference'][:300]}")
        lines.extend(p['url'] for p in json.loads(row['posts'])[:2])
        buttons.append([_b(db,d['base'],row['kind']+' · 选择处理','novel_item',draft=d['id'],id=row['id'])])
    buttons.append([_b(db,d['base'],'本页全部稍后','novel_bulk',draft=d['id'],ids=keys,mode='later'),
                    _b(db,d['base'],'本页全部忽略','novel_bulk',draft=d['id'],ids=keys,mode='ignored')])
    if page:buttons.append([_b(db,d['base'],'上一页','novelty',draft=d['id'],page=page-1)])
    if len(rows)==8:buttons.append([_b(db,d['base'],'下一页','novelty',draft=d['id'],page=page+1)])
    buttons.append([_b(db,d['base'],'预览并确认已选','preview',draft=d['id'])]);buttons.append(_back(db,d['base']))
    return card('\n'.join(lines),buttons)


def check_post(db,url):
    match=re.search(r'(?:thread-|/t/|tid=)(\d+)',url)
    source='chiphell' if 'chiphell.com/' in url else 'v2ex' if 'v2ex.com/' in url else 'nga' if 'nga.cn/' in url else ''
    if not match or not source:return card('请发送 CHH、V2EX 或 NGA 原帖链接。')
    row=db.execute('SELECT * FROM marketplace_posts WHERE source=? AND source_id=?',(source,match[1])).fetchone()
    if not row:return card('尚未采集到此帖。检查不会补发历史内容。')
    labels={'queued':'已入通知队列','skipped':'已筛选排除','needs_review':'待核对','pending_analysis':'等待 Grok 分析',
            'pending_body':'等待补抓正文','ready':'等待筛选','historical':'历史记录（不推送）','refresh_required':'补发前重新读取原帖'}
    lines=[row['title'],row['url'],'处理状态：'+labels.get(row['status'],row['status']),
           '正文：'+('完整' if row['body_complete'] else '缺失或不完整')]
    if row['platform_category']:lines.append('原站分类：'+row['platform_category'])
    decision=json.loads(row['decision'] or '{}')
    for item in decision.get('items',[]):lines.append(taxonomy.CATEGORIES.get(item['category'],'未归类')+'：'+('、'.join(item['reasons']) or '符合关注条件'))
    if row['error']:lines.append('处理原因：'+row['error'][:150])
    if row['event_id']:
        event=db.execute('SELECT status FROM events WHERE id=?',(row['event_id'],)).fetchone()
        if event:lines.append('投递状态：'+{'delivered':'Telegram 已回执','pending':'待发送','uncertain':'回执不确定'}.get(event[0],event[0]))
    return card('\n'.join(lines))


def clear_novel_choice(d,key):
    extension_keys={k for k in d['content'].get('taxonomy',{}) if k.endswith('.ext_'+key[:12])}
    for k in extension_keys:d['content']['taxonomy'].pop(k)
    for field in ('categories','blocked_categories'):
        d['content']['sale'][field]=[k for k in d['content']['sale'].get(field,[]) if k not in extension_keys]


def route(db,action,p,version):
    if action=='home':return home_card(db)
    if action=='check':return card('请回复或直接发送要检查的原帖链接。',[_back(db,version)])
    if action=='reviews':
        page=max(0,int(p.get('page',0)))
        rows=db.execute("SELECT r.reason,p.title,p.url FROM market_reviews r JOIN marketplace_posts p USING(source,source_id) WHERE r.status='pending' ORDER BY r.updated_at DESC LIMIT 10 OFFSET ?",(page*10,)).fetchall()
        lines=['待核对 · 不冒充新品种，也不自动推送。']
        for r in rows:lines.append(r['title'][:80]+'\n'+r['reason']+'\n'+r['url'])
        buttons=[_back(db,version)]
        if len(rows)==10:buttons.insert(0,[_b(db,version,'下一页','reviews',page=page+1)])
        return card('\n\n'.join(lines),buttons)
    if action=='rollback_preview':
        row=db.execute('SELECT parent FROM market_versions WHERE id=?',(version,)).fetchone()
        if not row or not row[0]:return card('当前版本没有可恢复的上一版本。',[_back(db,version)])
        previous=db.execute('SELECT content FROM market_versions WHERE id=?',(row[0],)).fetchone()
        d=draft(db);d['content']=json.loads(previous[0]);d['content']['review_actions']={};save(db,d)
        return preview_card(db,draft(db,d['id']))
    d=draft(db,p.get('draft'))
    scope=p.get('scope','sale')
    if scope not in {'sale','wanted'}:raise ValueError('无效关注范围')
    if d['base']!=version:raise ValueError('草稿基于旧版本，请返回 /market 重新创建草稿。')
    if action=='groups':return groups_card(db,d)
    if action=='group':return group_card(db,d,p['group'],p.get('page',0))
    if action=='group_set':
        group=p['group'];sale=d['content']['sale']
        if p['mode']=='partial' and group in sale.get('groups',[]):
            sale['categories']=sorted(set(sale.get('categories',[])) | {k for k in taxonomy.catalog(d['content'].get('taxonomy',{})) if taxonomy.group_for(k)==group and policy.selected(k,sale)})
        sale['groups']=[k for k in sale.get('groups',[]) if k!=group]
        sale['blocked_groups']=[k for k in sale.get('blocked_groups',[]) if k!=group]
        if p['mode']=='all':
            sale['groups'].append(group)
            sale['blocked_categories']=[k for k in sale.get('blocked_categories',[]) if taxonomy.group_for(k)!=group]
        if p['mode']=='block':sale['blocked_groups'].append(group)
        save(db,d);return group_card(db,d,group)
    if action=='toggle':
        key=p['category'];sale=d['content']['sale'];group=taxonomy.group_for(key)
        if group in sale.get('blocked_groups',[]):return group_card(db,d,group)
        selected=policy.selected(key,sale)
        chosen=set(sale.get('categories',[]));excluded=set(sale.get('blocked_categories',[]))
        if selected:
            chosen.discard(key);excluded.add(key)
        else:
            chosen.add(key);excluded.discard(key)
        sale['categories']=sorted(chosen);sale['blocked_categories']=sorted(excluded)
        save(db,d);return group_card(db,d,group)
    if action=='fields':return fields_card(db,d,p['category'],scope)
    if action=='field':return field_card(db,d,p['category'],p['field'],scope)
    if action in {'value','clear_field'}:
        rules=d['content'][scope].setdefault('filters',{}).setdefault(p['category'],{})
        if action=='clear_field':rules.pop(p['field'],None)
        else:
            rule=rules.setdefault(p['field'],{});values=rule.setdefault(p['mode'],[])
            values.remove(p['value']) if p['value'] in values else values.append(p['value'])
        save(db,d);return field_card(db,d,p['category'],p['field'],scope)
    if action in {'input','wanted_input'}:
        field=p.get('field','wanted');category=p.get('category','')
        cancel=_b(db,version,'取消输入','cancel_input')
        db.execute('INSERT OR REPLACE INTO market_inputs VALUES(?,?,?,?,?)',('webops',cancel['callback_data'],d['id'],field if field=='wanted' else scope+':'+field,category))
        text=('请回复具体型号／权益与地区，例如“iPhone 15 Pro 256G”或“北京 京东家政 2小时”。' if field=='wanted' else
              FIELDS[field]+'：回复“包含 苹果、三星；排除 某品牌”，数值字段可写“最低 100；最高 3000”。未写的条件将清空。')
        return card(text,[[cancel]])
    if action=='cancel_input':
        db.execute("DELETE FROM market_inputs WHERE profile='webops'");return home_card(db)
    if action=='wanted':return wanted_card(db,d)
    if action=='wanted_remove':
        d['content']['wanted']['targets']=[t for t in d['content']['wanted']['targets'] if control.digest(t)!=p['target_hash']]
        save(db,d);return wanted_card(db,d)
    if action=='wanted_options':
        choices={}
        for row in item_rows(db):
            for item in json.loads(row['items']):
                for identity in wanted.catalog([item]):choices[control.digest(identity)]=identity
        page=p.get('page',0);values=list(choices.values());buttons=[]
        for target in values[page*10:(page+1)*10]:buttons.append([_b(db,version,target['model']+' '+target.get('region','')+' '+str(target.get('specs') or ''),'wanted_add',draft=d['id'],target=target)])
        if (page+1)*10<len(values):buttons.append([_b(db,version,'下一页','wanted_options',draft=d['id'],page=page+1)])
        buttons.append([_b(db,version,'返回求购','wanted',draft=d['id'])]);return card('选择你实际能卖出的具体目标；添加后仍需预览确认。',buttons)
    if action=='wanted_add':
        target={k:v for k,v in p['target'].items() if k in {'category','model','specs','region'}}
        targets=d['content']['wanted'].setdefault('targets',[])
        if target not in targets:targets.append(target)
        d['content']['wanted']['enabled']=True;save(db,d);return wanted_card(db,d)
    if action=='novelty':return novel_card(db,d,p.get('page',0))
    if action=='novel_item':
        row=db.execute('SELECT * FROM market_novelty WHERE id=?',(p['id'],)).fetchone()
        if not row:raise ValueError('审核记录不存在')
        labels=[('修改建议上级','parent'),('归入现有类别','map'),('新增细类并关注','follow'),('归类但不关注','classify'),('忽略此品种','ignored'),('稍后','later')]
        return card(row['kind']+'\n'+json.loads(row['suggestion'])['difference'],
                    [[_b(db,version,label,'novel_action',draft=d['id'],id=p['id'],mode=mode)] for label,mode in labels])
    if action=='novel_bulk':
        for key in p['ids']:
            clear_novel_choice(d,key)
            d['content'].setdefault('review_actions',{})[key]=p['mode']
        save(db,d);return novel_card(db,d)
    if action=='novel_action':
        row=db.execute('SELECT * FROM market_novelty WHERE id=?',(p['id'],)).fetchone()
        if not row:raise ValueError('审核记录不存在')
        mode=p['mode'];target=p.get('target','')
        parent=d['content'].get('novel_parents',{}).get(p['id'],row['parent'])
        if mode=='parent':
            if not p.get('parent'):
                return card('请选择实际商品所属的大类。',[[ _b(db,version,label,'novel_action',draft=d['id'],id=p['id'],mode='parent',parent=group)] for group,label in taxonomy.GROUPS.items()])
            d['content'].setdefault('novel_parents',{})[p['id']]=p['parent'];save(db,d)
            return route(db,'novel_item',{'draft':d['id'],'id':p['id']},version)
        if mode=='map' and not target:
            keys=[k for k in taxonomy.CATEGORIES if taxonomy.group_for(k)==parent];page=p.get('page',0)
            buttons=[[_b(db,version,taxonomy.CATEGORIES[k].split('/')[-1],'novel_action',draft=d['id'],id=p['id'],mode='map',target=k)] for k in keys[page*16:(page+1)*16]]
            if (page+1)*16<len(keys):buttons.append([_b(db,version,'下一页','novel_action',draft=d['id'],id=p['id'],mode='map',page=page+1)])
            return card('归入哪一个现有类别？关注范围沿用当前策略。',buttons)
        clear_novel_choice(d,p['id'])
        if mode in {'map','follow','classify'}:
            proposal=json.loads(row['suggestion']);key=parent+'.ext_'+row['id'][:12]
            d['content'].setdefault('taxonomy',{})[key]={'parent':parent,'name':row['kind'],'terms':proposal['terms'],'target':target}
            if mode=='follow':
                d['content']['sale'].setdefault('categories',[]).append(key)
                d['content']['sale']['blocked_categories']=[k for k in d['content']['sale'].get('blocked_categories',[]) if k!=key]
            if mode=='classify':d['content']['sale'].setdefault('blocked_categories',[]).append(key)
            d['content'].setdefault('review_actions',{})[row['id']]='classified'
        else:d['content'].setdefault('review_actions',{})[row['id']]=mode
        save(db,d);return novel_card(db,d)
    if action=='preview':return preview_card(db,d)
    if action=='apply':
        if d['status']!='draft':return card('该草稿已处理。',[_back(db,version)])
        saved=json.loads(d['preview'] or '{}');now=preview(db,d)
        if d['revision']!=p['revision'] or saved.get('hash')!=p['hash'] or now['hash']!=p['hash']:
            return card('草稿或样本已变化，请重新预览。',[[_b(db,version,'重新预览','preview',draft=d['id'])]])
        policy.validate_filters(d['content']['sale'].get('filters',{}));policy.validate_filters(d['content']['wanted'].get('filters',{}));wanted.validate(d['content']['wanted'])
        new=control.activate(db,d['content'],d['base'],'telegram')
        db.execute("UPDATE market_drafts SET status='applied' WHERE id=?",(d['id'],))
        return card(f'已应用版本 {new}。后续帖子使用新策略；历史样本不会补发。',[_back(db,new)])
    raise ValueError('操作已失效，请重新打开 /market')


def command(profile,argument='',*,store=None):
    if profile!='webops':return card('交易管理请在库库干使用 /market。')
    store=store or Store()
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        return check_post(db,argument) if argument else home_card(db)


def callback(profile,payload,*,store=None):
    store=store or Store()
    if profile!='webops' or not owns_callback(profile,payload,box=store.box):raise ValueError('按钮不属于本人会话或原消息')
    parts=str(payload.get('data','')).split('|')
    if len(parts)!=3 or parts[0]!='mk1' or not hmac.compare_digest(parts[2],_signature(profile,'market',parts[1])):raise ValueError('按钮签名无效')
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM market_buttons WHERE id=? AND profile=?',(parts[1],profile)).fetchone()
        if not row:raise ValueError('按钮已失效')
        if row['result']:return json.loads(row['result'])
        version,content=control.baseline(db)
        taxonomy.refresh_extensions(content.get('taxonomy',{}))
        if row['version']!=version:return card('策略已更新，请重新打开交易管理。',[_back(db,version)])
        params=json.loads(row['payload'])
        if params.get('draft'):
            saved=db.execute('SELECT base,status FROM market_drafts WHERE id=?',(params['draft'],)).fetchone()
            if not saved or saved['status']!='draft' or saved['base']!=version:return card('草稿已处理或过期，请重新打开 /market。',[_back(db,version)])
        result=route(db,row['action'],params,version)
        # Repeated clicks on the same card never toggle back or apply twice.
        if row['action'] in {'apply','toggle','group_set','value','clear_field','novel_action','novel_bulk','wanted_add','wanted_remove'}:
            db.execute('UPDATE market_buttons SET result=? WHERE id=?',(control.dumps(result),parts[1]))
    taxonomy.refresh_extensions()
    return result


def clear_input(profile,*,store=None):
    if profile!='webops':return
    store=store or Store()
    with store.box.connect() as db:db.execute('DELETE FROM market_inputs WHERE profile=?',(profile,))


def reply(profile,message_id,text,*,store=None):
    if profile!='webops':return None
    store=store or Store()
    if re.fullmatch(r'https://(?:www\.)?(?:chiphell\.com/thread-\d+-1-1\.html|v2ex\.com/t/\d+|bbs\.nga\.cn/read\.php\?tid=\d+)',text.strip()):
        return command(profile,text.strip(),store=store)
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM market_inputs WHERE profile=?',(profile,)).fetchone()
        if not row:return None
        token=row['button'].split('|')[1]
        button_row=db.execute('SELECT created_at FROM market_buttons WHERE id=?',(token,)).fetchone()
        if not button_row or time.time()-button_row[0]>3600:
            db.execute('DELETE FROM market_inputs WHERE profile=?',(profile,))
            return None
        if message_id:
            events=db.execute('SELECT payload,receipt FROM events WHERE profile=? AND receipt IS NOT NULL',(profile,)).fetchall()
            if not any(row['button'] in event['payload'] and any(r.get('message_id')==message_id for r in (json.loads(event['receipt']) if isinstance(json.loads(event['receipt']),list) else [json.loads(event['receipt'])])) for event in events):return None
        row=dict(row)
        scope='sale'
        if ':' in row['field']:scope,row['field']=row['field'].split(':',1)
        d=draft(db,row['draft'])
        if control.baseline(db)[0]!=d['base']:raise ValueError('策略已变化，请重新打开 /market。')
        if row['field']=='wanted':
            from .classifier import category_for
            category=taxonomy.canonical(category_for(text)[0]);target=wanted.descriptor(text,category)
            if not target:raise ValueError('请提供具体型号或权益名称，避免只写品牌。')
            target={k:v for k,v in target.items() if k in {'category','model','region','specs'}}
            wanted.validate({'enabled':True,'version':1,'targets':[target]})
            targets=d['content']['wanted'].setdefault('targets',[])
            if target not in targets:targets.append(target)
            d['content']['wanted']['enabled']=True
        else:
            rule={}
            for clause in re.split(r'[;；\n]',text):
                m=re.fullmatch(r'\s*(包含|排除|最低|最高)\s*[:：]?\s*(.+?)\s*',clause)
                if not m:raise ValueError('用“包含 A、B；排除 C”或“最低 100；最高 3000”填写。')
                field={'包含':'include','排除':'exclude','最低':'min','最高':'max'}[m[1]]
                if field in {'min','max'}:
                    if row['field'] not in {'price','ram_gb','storage_gb'}:raise ValueError('此字段不支持数值区间。')
                    rule[field]=float(m[2])
                else:rule[field]=[s.strip() for s in re.split('[、，,]',m[2]) if s.strip()]
            policy.validate_filters({row['category']:{row['field']:rule}})
            d['content'][scope].setdefault('filters',{}).setdefault(row['category'],{})[row['field']]=rule
        save(db,d);db.execute('DELETE FROM market_inputs WHERE profile=?',(profile,))
        return wanted_card(db,d) if row['field']=='wanted' else field_card(db,d,row['category'],row['field'],scope)
