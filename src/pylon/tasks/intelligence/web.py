"""Intelligence policy editing under the shared owner-only web session."""
from __future__ import annotations

import json
import secrets
import time
from . import policy
from .store import active, initialize
from pylon.tasks.marketplace.control import digest, dumps


def draft(db):
    version, content=active(db)
    row=db.execute("SELECT * FROM intel_drafts WHERE status='draft' AND base=? ORDER BY rowid DESC LIMIT 1",(version,)).fetchone()
    if not row:
        key=secrets.token_hex(12)
        db.execute('INSERT INTO intel_drafts(id,base,content) VALUES(?,?,?)',(key,version,dumps(content)))
        row=db.execute('SELECT * FROM intel_drafts WHERE id=?',(key,)).fetchone()
    value=dict(row);value['content']=json.loads(value['content'])
    value['preview']=json.loads(value['preview']) if value['preview'] else None
    return value


def state(db, csrf):
    version, content=active(db)
    counts={}
    for row in db.execute('SELECT source,status,count(*) n FROM intel_items GROUP BY source,status'):
        counts.setdefault(row['source'],{})[row['status']]=row['n']
    return {'active':{'version':version,'content':content},'draft':draft(db),'csrf':csrf,
            'groups':policy.GROUPS,'catalog':policy.CATALOG,'sources':policy.SOURCES,'counts':counts}


def checked(db,data):
    from pylon.tasks.marketplace.web import WebError
    row=db.execute('SELECT * FROM intel_drafts WHERE id=?',(data.get('id',''),)).fetchone()
    if not row:raise WebError('草稿不存在，请重新打开页面',404)
    value=dict(row);value['content']=json.loads(value['content'])
    if value['status']!='draft' or value['revision']!=data.get('revision') or value['base']!=active(db)[0]:
        raise WebError('策略或草稿已变化，请刷新后重新预览',409)
    return value


def preview(db, value):
    counts={'push':0,'skip':0,'hold':0};examples={key:[] for key in counts};sources={}
    rows=db.execute('SELECT * FROM intel_items ORDER BY observed_at DESC LIMIT 20000').fetchall()
    for row in rows:
        item=dict(row);result=json.loads(item['result']) if item['result'] else None
        if item['status']=='trade':verdict,reason='skip','已分流到交易工作区'
        elif result is None:verdict,reason='hold','尚未完成摘要判断'
        else:verdict,reason=policy.decide(item,result,value['content'])
        if result:
            from pylon.tasks.linuxdo.common import timestamp
            expiry=timestamp(result.get('expires_at',''))
            if expiry and expiry<=time.time():verdict,reason='skip','内容已经过期'
        counts[verdict]+=1;sources[item['source']]=sources.get(item['source'],0)+1
        if len(examples[verdict])<12:
            examples[verdict].append({'title':item['title'],'url':item['url'],'source':item['source'],
                'reason':reason,'evidence_scope':item['evidence_scope'],'missing':(result or {}).get('missing_evidence',[])})
    result={'counts':counts,'examples':examples,'sources':sources,'sample_count':len(rows),
            'as_of':time.time(),'draft_hash':digest(value['content']),'base':value['base']}
    result['hash']=digest(result)
    return result


def dispatch(db,action,data,query,csrf):
    from pylon.tasks.marketplace.web import WebError
    initialize(db)
    if action=='state':return state(db,csrf)
    if action=='draft':
        value=checked(db,data);content=policy.validate(data.get('content'))
        if content['linuxdo_focus']!=value['content']['linuxdo_focus']:
            raise WebError('来源的基础约束不可在此修改')
        db.execute('UPDATE intel_drafts SET content=?,revision=revision+1,preview=NULL WHERE id=?',(dumps(content),value['id']))
        return state(db,csrf)
    if action=='preview':
        value=checked(db,data);result=preview(db,value)
        db.execute('UPDATE intel_drafts SET preview=? WHERE id=?',(dumps(result),value['id']))
        return result
    if action=='apply':
        old=db.execute('SELECT applied_version FROM intel_drafts WHERE id=?',(data.get('id',''),)).fetchone()
        if old and old[0]:return {'version':old[0]}
        value=checked(db,data);result=json.loads(value['preview'] or '{}')
        if result.get('hash')!=data.get('hash') or result.get('draft_hash')!=digest(value['content']):
            raise WebError('请先预览当前草稿',409)
        cursor=db.execute('INSERT INTO intel_versions(parent,content,created_at,origin) VALUES(?,?,?,?)',
            (value['base'],dumps(value['content']),time.time(),'intel-web'))
        version=cursor.lastrowid
        db.execute("UPDATE intel_drafts SET status='applied',applied_version=? WHERE id=?",(version,value['id']))
        db.execute("""UPDATE intel_items SET status='pending',next_attempt=0,attempts=0,semantic_attempts=0,error='',lease_until=0
            WHERE historical=0 AND event_id IS NULL AND observed_at>=?
              AND status IN ('needs_info','excluded','retry')""",(time.time()-86400,))
        return {'version':version}
    if action=='rollback':
        version,_=active(db);row=db.execute('SELECT parent FROM intel_versions WHERE id=?',(version,)).fetchone()
        if not row[0]:raise WebError('没有可恢复的上一版本')
        content=db.execute('SELECT content FROM intel_versions WHERE id=?',(row[0],)).fetchone()[0]
        value=draft(db)
        db.execute('UPDATE intel_drafts SET content=?,revision=revision+1,preview=NULL WHERE id=?',(content,value['id']))
        return state(db,csrf)
    if action=='items':
        status=query.get('status','');search=query.get('q','')[:100]
        rows=db.execute('''SELECT source,source_id,title,url,evidence_scope,status,result,error,phase,updated_at FROM intel_items
            WHERE (?='' OR status=?) AND (?='' OR instr(lower(title),lower(?))>0)
            ORDER BY updated_at DESC LIMIT 100''',(status,status,search,search)).fetchall()
        result=[]
        for row in rows:
            item=dict(row);item['result']=json.loads(item['result']) if item['result'] else None
            if item['result']:
                item['result']['reason']=item['result'].get('policy_reason') or item['result'].get('reason','')
            result.append(item)
        return {'items':result}
    raise WebError('接口不存在',404)
