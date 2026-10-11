"""Persistent source queues with independent analysis and one delivery owner."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pylon.config import redact
from pylon.tasks.linuxdo import judge,trade
from pylon.tasks.linuxdo.common import timestamp
from pylon.tasks.marketplace.control import digest,dumps
from . import health,policy,attempts
from .store import Store,active

VERSION='intelligence-v1'


def helper_snapshot(snapshot):
    from pylon.tasks.linuxdo.helper import SOURCE
    rows=[{**r,'complete':False,'origin':'telegram'} for r in snapshot['evidence'] if r['ref'].startswith('tg:'+SOURCE+':')]
    return {**snapshot,'evidence':rows,'fingerprint':digest([{k:v for k,v in r.items() if k!='observed_at'} for r in rows]),
            'actors':{k:v for k,v in snapshot.get('actors',{}).items() if any(r['ref']==k for r in rows)},
            'source':'linuxdo','evidence_scope':'helper_excerpt','lead_mode':True}


def capture_linuxdo(store,snapshot):
    snap=helper_snapshot(snapshot)
    if not snap['evidence']:
        queue=Store(store.box)
        with queue.box.connect() as db:
            db.execute("""UPDATE intel_items SET status='excluded',lease_until=0,error='',phase='source',result=?
                WHERE source='linuxdo' AND source_id=? AND status IN ('pending','retry','needs_info')""",
                (dumps({'decision':'skip','categories':[],'facts':[],'reason':'没有可用的 Helper 公共摘要'}),snapshot['topic_id']))
        return False
    text='\n\n'.join(dict.fromkeys(r['body'] for r in snap['evidence']))
    return Store(store.box).ingest('linuxdo',snap['topic_id'],title=snap['title'],body=text,
        url='https://linux.do/t/topic/'+snap['topic_id'],
        published_at=next((r.get('source_published_at','') for r in snap['evidence'] if r.get('source_published_at')),''),
        evidence_scope='helper_excerpt',context={'snapshot':snap},historical=bool(snapshot.get('historical')))


def ingest_v2ex(posts,decisions,*,store=None):
    store=store or Store();count=0
    for p in posts:
        route=decisions[p.message_id]
        topic=re.search(r'/t/(\d+)',p.v2ex_url)
        count+=store.ingest('v2ex',topic[1] if topic else str(p.message_id),title=p.title,body=p.body_full or p.body_excerpt,
            url=p.v2ex_url,published_at=p.posted_at,evidence_scope='forum_body' if p.body_complete else 'excerpt',
            context={'route':route.route,'reason':route.reason,'categories':route.matched_categories})
    return count


def ingest_xianbao(post,decision,eligible,*,bark=None,store=None):
    from dataclasses import asdict
    from pylon.tasks.xianbao.worker import full_url
    store=store or Store()
    return store.ingest('xianbao',str(post.get('id') or full_url(post)),title=str(post.get('title') or ''),
        body=str(post.get('body') or post.get('content') or ''),url=full_url(post),
        published_at=str(post.get('published_at') or post.get('time') or ''),
        evidence_scope='forum_body' if post.get('body') else 'excerpt',
        context={'native':asdict(decision),'eligible':eligible,'bark':bark or {},
                 'first_seen':post.get('_review_first_seen')})


def snapshot_for(item):
    context=json.loads(item['context'])
    prior_active=bool(item.get('event_id')) and item.get('_delivery_status')!='suppressed'
    if context.get('snapshot'):
        snapshot={**context['snapshot'],'lead_mode':True,'source':item['source']}
        if prior_active and item.get('result'):
            prior=json.loads(item['result'])
            snapshot['notifications']=[*snapshot.get('notifications',[]),{'facts':prior.get('facts',[]),'delivery_status':item.get('_delivery_status') or 'queued'}]
            snapshot['previously_notified']=True
        return snapshot
    ref=item['source']+':'+item['source_id']
    row={'ref':ref,'origin':'telegram' if item['evidence_scope']=='excerpt' else 'topic',
         'post_number':1,'url':item['url'],'title':item['title'],'body':item['body'],
         'links':__import__('pylon.tasks.marketplace.enrich',fromlist=['links']).links(item['body']),
         'complete':item['evidence_scope']=='forum_body','observed_at':item['observed_at'],
         'source_published_at':item['published_at'],'source_edited_at':''}
    prior=json.loads(item['result']) if prior_active and item.get('result') else {}
    return {'topic_id':item['source_id'],'title':item['title'],'evidence':[row],
            'fingerprint':item['fingerprint'],
            'notifications':[{'facts':prior.get('facts',[]),'delivery_status':'queued'}] if prior else [],
            'previously_notified':prior_active,
            'actors':{},'source':item['source'],'lead_mode':True}


def native_result(item,context):
    native=context['native'];text=item['title']+'\n'+item['body']
    code='I2' if re.search(r'话费|电费|水费|燃气',text) else 'I3' if re.search(r'交通|公交|地铁|加油|打车',text) else 'I1'
    return {'decision':'push' if context['eligible'] else 'skip','categories':[code],
            'reason':native.get('value_label') or native['reason'],
            'action':'查看原文中的领取方式与参与条件','expires_at':'','change_kind':'initial',
            'missing_evidence':[], 'facts':[{'category':code,'kind':'resource','subject':item['title'],
            'value':item['body'][:600] or item['title'],'evidence':[]}],
            'native_tier':native['tier'],'native_reason':native['reason']}


def format_message(item,result):
    title=re.sub(r'[\[\]`*_\r\n]',' ',item['title'])[:180]
    lines=[f'## 情报 · {policy.SOURCES[item["source"]]} · {title}',result.get('reason','')]
    for fact in result.get('facts',[])[:5]:lines.append('- '+re.sub(r'\s+',' ',fact['value'])[:600])
    if item['evidence_scope']=='helper_excerpt':lines.append('依据 Helper 摘要；优惠与经验为原帖陈述。')
    missing=result.get('missing_evidence',[])
    if missing:lines.append('尚需确认：'+'；'.join(missing[:3]))
    if result.get('action'):lines.append('查看方式：'+result['action'])
    lines.append('原帖：'+item['url'])
    return '\n'.join(line for line in lines if line)


def finish(store,item,result,version,config):
    verdict,reason=policy.decide(item,result,config)
    stored={**result,'policy_decision':verdict,'policy_reason':reason}
    expiry=timestamp(result.get('expires_at',''))
    if expiry and expiry<=time.time():stored.update(policy_decision='skip',policy_reason='内容已经过期');verdict='skip'
    context=json.loads(item['context']);now=time.time()
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT fingerprint FROM intel_items WHERE source=? AND source_id=?',(item['source'],item['source_id'])).fetchone()
        if not current or current[0]!=item['fingerprint']:return {'progress':True,'stale':True}
        if item['source']=='linuxdo' and context.get('snapshot',{}).get('version') is not None:
            source=db.execute('SELECT version FROM linuxdo_topics WHERE topic_id=?',(item['source_id'],)).fetchone()
            if not source or source[0]!=context['snapshot']['version']:
                db.execute("UPDATE intel_items SET lease_until=0,next_attempt=0 WHERE source=? AND source_id=?",(item['source'],item['source_id']))
                return {'progress':True,'stale':True}
        if active(db)[0]!=version:
            db.execute("UPDATE intel_items SET status='pending',lease_until=0,next_attempt=0 WHERE source=? AND source_id=?",(item['source'],item['source_id']))
            return {'progress':True,'stale':True}
        event_id=None;state={'push':'queued','hold':'needs_info','skip':'excluded'}[verdict]
        if verdict=='push' and not item['historical']:
            normalized=lambda v:re.sub(r'\s+','',v).casefold()
            keys=[digest([f['kind'],normalized(f['subject']),normalized(f['value']),expiry or '']) for f in result['facts']]
            if result.get('change_kind') in {'restocked','correction'}:keys=[digest([k,item['fingerprint'],result['change_kind']]) for k in keys]
            fresh=[k for k in keys if not db.execute('SELECT 1 FROM intel_receipts WHERE fact_key=?',(k,)).fetchone()]
            if not fresh:state='duplicate'
            else:
                event_id=store.box.enqueue(profile='webops',job_id='intelligence-review',kind='text',
                    payload={'text':format_message(item,{**result,'reason':reason}),**({'expires_at':expiry} if expiry else {})},
                    key='intel:'+digest(sorted(fresh)),connection=db)
                for key in fresh:db.execute('INSERT INTO intel_receipts VALUES(?,?,?,?,?)',(key,event_id,item['source'],item['source_id'],now))
                if item['source']=='xianbao' and context.get('bark'):
                    store.box.enqueue(profile='webops',job_id='intelligence-review',kind='bark',
                        payload=context['bark'],key='intel:bark:'+digest(sorted(fresh)),connection=db)
        elif item['historical']:state='historical'
        db.execute('UPDATE intel_items SET result=?,status=?,event_id=COALESCE(?,event_id),lease_until=0,error=\'\',phase=\'complete\',policy_version=?,updated_at=? WHERE source=? AND source_id=?',
            (dumps(stored),state,event_id,version,now,item['source'],item['source_id']))
        from pylon.tasks.daily_review.store import record
        record(db,item['source'],item['source_id'],lane='intel',status=state,result=stored,rule_version=version,event_id=event_id)
    return {'progress':True,'decision':verdict,'queued':int(bool(event_id))}


def process_item(store,item,*,value_request=judge.request,trade_request=trade.request):
    with store.box.connect() as db:
        version,config=active(db)
        receipt=db.execute('SELECT status FROM events WHERE id=?',(item.get('event_id'),)).fetchone()
        if receipt:item={**item,'_delivery_status':receipt[0]}
    context=json.loads(item['context']);phase='route'
    try:
        if item['source']=='xianbao':return finish(store,item,native_result(item,context),version,config)
        if item['source']=='v2ex' and context.get('route')=='exclude' and not context.get('trade'):
            return finish(store,item,{'decision':'skip','reason':context['reason'],'categories':[],'facts':[]},version,config)
        snapshot=snapshot_for(item);focus=policy.model_focus(config,item['source'])
        if item['source']=='v2ex' and context.get('trade'):
            phase='trade_extraction'
            key=trade.cache_key(snapshot,{},judge.model_name(),focus=focus)
            extracted=store.cached(key)
            if extracted is None:
                snapshot={**snapshot,'analysis_correction':attempts.correction(store,item,phase)}
                extracted=attempts.call(store,item,phase,lambda:trade_request(snapshot),lambda value:trade.validate(value,snapshot));store.cache(key,extracted)
            if extracted['intent'] in {'offer','wanted','unknown'}:
                from .routing import residual
                snapshot=residual(snapshot,extracted)
                if not snapshot['evidence']:
                    with store.box.connect() as db:db.execute("UPDATE intel_items SET status='trade',result=?,lease_until=0,error='',phase='trade',updated_at=? WHERE source=? AND source_id=? AND fingerprint=?",(dumps(extracted),time.time(),item['source'],item['source_id'],item['fingerprint']))
                    return {'progress':True,'decision':'trade','queued':0}
        if item['source']=='linuxdo':
            from pylon.tasks.linuxdo.store import Store as LinuxStore
            from pylon.tasks.marketplace import linuxdo_bridge,ai_prices
            legacy=LinuxStore(store.box)
            owned=linuxdo_bridge.owned_snapshot(snapshot)
            if owned['evidence']:
                phase='trade_extraction';prices=ai_prices.config()
                key=trade.cache_key(owned,prices,judge.model_name(),focus=focus)
                extracted=store.cached(key) or legacy.cached(key)
                if extracted is None:
                    owned={**owned,'analysis_correction':attempts.correction(store,item,phase)}
                    extracted=attempts.call(store,item,phase,lambda:trade_request(owned),lambda value:trade.validate(value,owned));store.cache(key,extracted)
                else:extracted=trade.validate(extracted,owned)
                if extracted['intent'] in {'non_trade','cloud_offer'}:
                    with store.box.connect() as db:
                        if db.execute("SELECT 1 FROM sqlite_master WHERE name='marketplace_posts'").fetchone():
                            db.execute("UPDATE marketplace_posts SET status='skipped',error='Helper 更新后已分流情报' WHERE source='linuxdo' AND source_id=? AND status='awaiting_helper'",(item['source_id'],))
                if extracted['intent']=='unknown':
                    from .routing import residual
                    remaining=residual(snapshot,extracted)
                    if not remaining['evidence']:
                        return finish(store,item,{'decision':'hold','categories':[], 'facts':[],
                            'reason':'摘要尚不能确定交易主体或具体价值','missing_evidence':['需要 Helper 内容更新']},version,config)
                    snapshot=remaining
                elif extracted['intent'] not in {'non_trade','cloud_offer'}:
                    outcome=linuxdo_bridge.transfer(legacy,snapshot,extracted)
                    if outcome.get('decision')=='hold':
                        capture_linuxdo(legacy,legacy.snapshot(item['source_id']))
                        with store.box.connect() as db:
                            db.execute("UPDATE intel_items SET lease_until=0,next_attempt=0 WHERE source=? AND source_id=? AND fingerprint=? AND status IN ('pending','retry')",
                                       (item['source'],item['source_id'],item['fingerprint']))
                        return {'progress':True,'stale':True,'queued':0}
                    with store.box.connect() as db:db.execute("UPDATE intel_items SET status='trade',result=?,lease_until=0,error='',phase='trade',updated_at=? WHERE source=? AND source_id=? AND fingerprint=?",(dumps(extracted),time.time(),item['source'],item['source_id'],item['fingerprint']))
                    from .routing import residual
                    snapshot=residual(snapshot,extracted)
                    if not snapshot['evidence']:return {'progress':True,'decision':'trade','queued':0}
        phase='value';key=digest([VERSION,snapshot['fingerprint'],focus,judge.PROMPT_VERSION,judge.model_name()])
        result=store.cached(key)
        if result is None:
            snapshot={**snapshot,'analysis_correction':attempts.correction(store,item,phase)}
            result=attempts.call(store,item,phase,lambda:value_request(snapshot,focus),lambda value:judge.validate_result(value,snapshot,focus));store.cache(key,result)
        return finish(store,item,result,version,config)
    except Exception as exc:
        reason=type(exc).__name__+': '+attempts.safe_error(exc);now=time.time()
        with store.box.connect() as db:
            rows=attempts.history(db,item,phase)
            if isinstance(exc,attempts.BudgetError) and exc.next_attempt:
                db.execute("UPDATE intel_items SET status='retry',next_attempt=?,lease_until=0 WHERE source=? AND source_id=? AND fingerprint=?",
                           (exc.next_attempt,item['source'],item['source_id'],item['fingerprint']))
                return {'progress':False,'queued':0,'waiting':True}
            terminal=attempts.exhausted(rows) or not rows
            if isinstance(exc,attempts.BudgetError): terminal=not bool(exc.next_attempt)
            due=getattr(exc,'next_attempt',0) or now+(60 if len(rows)<=1 else 300)
            status='failed' if terminal else 'retry'
            # A failed new request has no result. The prior result remains in the
            # review ledger and any immutable event, never masquerading as fresh.
            changed=db.execute('UPDATE intel_items SET status=?,attempts=?,semantic_attempts=?,next_attempt=?,lease_until=0,error=?,phase=?,updated_at=?,result=NULL WHERE source=? AND source_id=? AND fingerprint=?',
                (status,len(rows),sum(r['semantic'] for r in rows),0 if terminal else due,reason,phase,now,item['source'],item['source_id'],item['fingerprint']))
            if not changed.rowcount:return {'progress':True,'stale':True,'queued':0}
            from pylon.tasks.daily_review.store import record
            record(db,item['source'],item['source_id'],lane='intel',status=status,
                result={'reason':reason,'phase':phase,'failed_at':rows[-1]['finished_at'] if rows else now,'attempts':len(rows),
                        'request_id':rows[-1]['request_id'] if rows else '',
                        'elapsed':rows[-1]['elapsed'] if rows else 0},rule_version=version)
        return {'progress':False,'exhausted':terminal,'error':phase+': '+reason,'queued':0}


def review_source(store,source,limit=2):
    # Native Xianbao decisions are already computed at collection; drain those
    # cheap records without making useful leads wait behind local rejections.
    rows=store.claim(source,200 if source=='xianbao' else limit);results=[process_item(store,row) for row in rows]
    with store.box.connect() as db:
        from pylon.tasks.daily_review.capture import intel_decision
        for item in rows:
            updated=db.execute('SELECT * FROM intel_items WHERE source=? AND source_id=?',(source,item['source_id'])).fetchone()
            intel_decision(db,updated)
    # Retire the coarse source-wide incident. Local exclusions must not reset a
    # failing model stage, and exhausted individual inputs are no longer work.
    health.record(store.box,source+':analysis',backlog=0)
    for phase in ('trade_extraction','value'):
        with store.box.connect() as db:
            pending=db.execute("SELECT count(*) FROM intel_items WHERE source=? AND historical=0 AND status IN ('pending','retry') AND phase=?",(source,phase)).fetchone()[0]
            success=db.execute("SELECT max(finished_at) FROM intel_attempts WHERE source=? AND phase=? AND status='success'",(source,phase)).fetchone()[0] or 0
            prior=db.execute('SELECT last_progress FROM intel_health WHERE component=?',(source+':'+phase,)).fetchone()
            error=db.execute("SELECT error FROM intel_items WHERE source=? AND phase=? AND status='retry' ORDER BY updated_at DESC LIMIT 1",(source,phase)).fetchone()
        health.record(store.box,source+':'+phase,backlog=pending,progress=success>(prior[0] if prior else 0),error=error[0] if error else '')
    return {'source':source,'processed':len(rows),'queued':sum(r.get('queued',0) for r in results),'errors':sum(bool(r.get('error')) for r in results)}


def review(store=None,limit=2,source=None):
    if source is not None and source not in policy.SOURCES:raise ValueError('unknown intelligence source')
    store=store or Store()
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        active(db)
        db.execute("UPDATE intel_items SET status='delivered' WHERE status='queued' AND event_id IN (SELECT id FROM events WHERE status='delivered')")
        db.execute(health.SCHEMA)
        prior=db.execute("SELECT last_progress FROM intel_health WHERE component='notification:delivery'").fetchone()
        delivery=db.execute("""SELECT count(*) n FROM events WHERE job_id='intelligence-review'
            AND status IN ('pending','sending','retry')""").fetchone()[0]
        completed=db.execute("SELECT max(updated_at) FROM events WHERE job_id='intelligence-review' AND status='delivered'").fetchone()[0] or 0
    health.record(store.box,'notification:delivery',backlog=delivery,
        progress=completed>(prior[0] if prior else 0),error='通知队列尚未推进' if delivery else '')
    if source is not None:return [review_source(store,source,limit)]
    with ThreadPoolExecutor(max_workers=3) as pool:return list(pool.map(lambda source:review_source(store,source,limit),policy.SOURCES))


def preserve_legacy_notice(key,text):
    """Move already-approved unsent notifications without repeating business work."""
    return Store().box.enqueue(profile='webops',job_id='intelligence-review',kind='text',
        payload={'text':text},key='intel:legacy:'+key)


def capture_v2ex_trade(post, *, store=None, historical=False):
    """Keep independent public information in mixed listings in a separate queue."""
    store=store or Store()
    topic=re.search(r'/t/(\d+)',post['url'])
    return store.ingest('v2ex',topic[1] if topic else str(post.get('id') or post['source_id']),title=post['title'],
        body=post.get('body_full') or post.get('body') or post.get('body_excerpt',''),url=post['url'],
        published_at=post.get('posted_at',''),evidence_scope='forum_body' if post.get('body_complete') else 'excerpt',
        context={'route':'candidate','trade':True},historical=historical)


def xianbao_bark(post):
    import os
    from urllib.parse import urlsplit
    from pylon.shared import bark
    from pylon.tasks.xianbao.worker import full_url
    key=bark.load_device_key(secret_name=os.getenv('XIANBAO_BARK_SECRET_NAME') or None)
    base=os.getenv('BARK_API_BASE') or bark.DEFAULT_API_BASE
    direct=os.getenv('BARK_URL','')
    if os.getenv('XIANBAO_USE_WEBOPS_BARK')!='1' and direct:
        parsed=urlsplit(direct);key=parsed.path.strip('/');base=parsed.scheme+'://'+parsed.netloc
    if not key:raise ValueError('线报 Bark 目标尚未配置')
    payload=bark.build_payload(title=str(post.get('title') or '线报'),
        body=str(post.get('body') or post.get('content') or post.get('title') or '')[:420],
        url=full_url(post),device_key=key,group=os.getenv('XIANBAO_BARK_GROUP','xianbao'),
        level=os.getenv('XIANBAO_BARK_LEVEL','active'),volume=int(os.getenv('XIANBAO_BARK_VOLUME','5')))
    return {'api_base':base,'body':payload}


def summary(box=None):
    store=Store(box)
    with store.box.connect() as db:
        counts=dict(db.execute('SELECT status,count(*) FROM intel_items WHERE historical=0 GROUP BY status').fetchall())
        latest=db.execute("SELECT source,phase,error FROM intel_items WHERE error<>'' ORDER BY updated_at DESC LIMIT 1").fetchone()
    line=(f"情报：待判断 {counts.get('pending',0)}；待重试 {counts.get('retry',0)}；"
          f"待补信息 {counts.get('needs_info',0)}；处理失败 {counts.get('failed',0)}；待投递 {counts.get('queued',0)}；已推送 {counts.get('delivered',0)}")
    if latest:line+=f"\n最近异常：{policy.SOURCES[latest['source']]} / {latest['phase']} / {redact(latest['error'])}"
    return line
