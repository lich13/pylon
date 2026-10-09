"""Small owner-only trading workspace, with no system administration routes."""
from __future__ import annotations

import hmac
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse
from starlette.routing import Route
from starlette.middleware.base import BaseHTTPMiddleware

from . import control, interface, policy, taxonomy, wanted, web_auth
from .subjects import FIELDS, applicable_fields
from .store import Store

BASE='/pylon/market/'


class WebError(Exception):
    def __init__(self, text, status=400):self.text,self.status=text,status


def _draft(db, data, *, mutable=True):
    row=db.execute("SELECT * FROM market_drafts WHERE id=? AND profile='webops'",(data.get('id',''),)).fetchone()
    if not row:raise WebError('草稿不存在，请重新打开页面',404)
    d=dict(row);d['content']=json.loads(d['content'])
    if mutable and (d['status']!='draft' or d['revision']!=data.get('revision')):
        raise WebError('草稿已变化，请刷新后重试',409)
    if mutable and control.baseline(db)[0]!=d['base']:
        raise WebError('生效策略已变化，请重新预览新草稿',409)
    return d


def _validate_content(value, previous):
    if not isinstance(value,dict) or set(value)-set(previous):raise WebError('无效的关注清单')
    # New product kinds can only enter through their evidence-backed review.
    for key in ('taxonomy','review_actions','novel_parents','ai_prices'):
        if value.get(key)!=previous.get(key):raise WebError('请通过对应审核或价格设置修改此项')
    catalog=taxonomy.catalog(value.get('taxonomy',{}));types={taxonomy.type_for(k) for k in catalog}
    for scope in ('sale','wanted'):
        item=value.get(scope)
        if not isinstance(item,dict) or type(item.get('enabled')) is not bool:raise WebError('关注范围无效')
        if set(item)-{'version','enabled','groups','types','categories','blocked_groups','blocked_types','partial_types','blocked_categories',
                       'allow_beijing_pickup','filters','targets'}:raise WebError('存在不支持的筛选字段')
        for key,allowed in (('groups',taxonomy.GROUPS),('blocked_groups',taxonomy.GROUPS),
                            ('types',types),('blocked_types',types),('partial_types',types),('categories',catalog),('blocked_categories',catalog)):
            rows=item.get(key,[])
            if not isinstance(rows,list) or len(rows)>500 or any(not isinstance(v,str) or v not in allowed for v in rows):
                raise WebError('分类选择无效')
        policy.validate_filters(item.get('filters',{}),categories=catalog)
    wanted.validate(value['wanted'])
    return value


def _state(db, csrf):
    version,content=control.baseline(db)
    d=interface.draft(db)
    d['preview']=json.loads(d['preview']) if d['preview'] else None
    catalog=taxonomy.catalog(d['content'].get('taxonomy',{}))
    return {'active':{'version':version,'content':content},'draft':d,'catalog':catalog,
            'groups':taxonomy.GROUPS,'hierarchy':taxonomy.hierarchy(catalog),'fields':FIELDS,
            'applicable':{k:applicable_fields(k) for k in catalog},'csrf':csrf}


def _dispatch(store, action, method, data, query, cookie, csrf, origin, request_origin):
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if action=='login':
            if origin and origin!=request_origin:raise WebError('请求来源不符',403)
            try:return {'_session':web_auth.redeem(db,data.get('token'))}
            except ValueError as error:raise WebError(str(error),401) from None
        session=db.execute('SELECT * FROM market_web_sessions WHERE hash=? AND expires_at>?',
                           (web_auth.digest(cookie or ''),time.time())).fetchone()
        if not session or session['profile']!='webops':raise WebError('请在库库干发送 /market 获取登录链接',401)
        if method!='GET' and (origin!=request_origin or not hmac.compare_digest(csrf or '',session['csrf'])):
            raise WebError('请求校验失败，请重新打开页面',403)
        if action=='state':return _state(db,session['csrf'])
        if action=='logout':
            db.execute('DELETE FROM market_web_sessions WHERE hash=?',(web_auth.digest(cookie),))
            return {'logged_out':True}
        if action=='draft':
            d=_draft(db,data);d['content']=_validate_content(data.get('content'),d['content'])
            interface.save(db,d)
            return _state(db,session['csrf'])
        if action=='wanted-add':
            from .classifier import category_for
            d=_draft(db,data);text=data.get('text','')
            if not isinstance(text,str) or not 2<=len(text)<=300:raise WebError('请输入具体型号或权益名称')
            target=wanted.descriptor(text,taxonomy.canonical(category_for(text)[0]))
            if not target:raise WebError('请提供具体型号或权益名称，避免只写品牌')
            target={k:v for k,v in target.items() if k in {'category','model','region','specs'}}
            wanted.validate({'enabled':True,'version':1,'targets':[target]})
            targets=d['content']['wanted'].setdefault('targets',[])
            if target not in targets:targets.append(target)
            d['content']['wanted']['enabled']=True;interface.save(db,d)
            return _state(db,session['csrf'])
        if action=='preview':
            d=_draft(db,data);p=interface.preview(db,d)
            db.execute('UPDATE market_drafts SET preview=? WHERE id=?',(control.dumps(p),d['id']))
            return p
        if action=='apply':
            previous=db.execute('SELECT version FROM market_web_applies WHERE draft=?',(data.get('id',''),)).fetchone()
            if previous:return {'version':previous[0]}
            d=_draft(db,data);p=json.loads(d['preview'] or '{}')
            if p.get('hash')!=data.get('hash') or p.get('draft_hash')!=control.digest(d['content']):
                raise WebError('请先预览当前草稿',409)
            version=control.activate(db,d['content'],d['base'],'market-web')
            db.execute("UPDATE market_drafts SET status='applied' WHERE id=?",(d['id'],))
            db.execute('INSERT INTO market_web_applies VALUES(?,?)',(d['id'],version))
            return {'version':version}
        if action=='rollback':
            version,value=control.baseline(db)
            parent=db.execute('SELECT parent FROM market_versions WHERE id=?',(version,)).fetchone()[0]
            if not parent:raise WebError('没有可恢复的上一版本')
            d=interface.draft(db);d['content']=json.loads(db.execute('SELECT content FROM market_versions WHERE id=?',(parent,)).fetchone()[0])
            d['content']['review_actions']={};interface.save(db,d)
            return _state(db,session['csrf'])
        if action=='options':
            category,field=query.get('category',''),query.get('field','')
            if category not in taxonomy.catalog(control.baseline(db)[1].get('taxonomy',{})) or field not in FIELDS:raise WebError('属性无效')
            options=interface.options(db,category,field,detail=True)
            return {'options':options[:4],'counts':options[4]}
        if action=='reviews':
            rows=[dict(r) for r in db.execute("""SELECT r.*,p.title,p.url,p.body_complete,p.items FROM market_reviews r
                JOIN marketplace_posts p USING(source,source_id) WHERE r.status='pending' ORDER BY r.updated_at DESC LIMIT 200""")]
            for row in rows:row['items']=json.loads(row['items'])
            novel=[dict(r) for r in db.execute("SELECT * FROM market_novelty WHERE status IN ('pending','later') ORDER BY first_seen LIMIT 200")]
            for row in novel:
                for key in ('posts','suggestion'):row[key]=json.loads(row[key])
            return {'reviews':rows,'novelty':novel}
        if action=='review-choice':
            d=_draft(db,data)
            ids=data.get('ids',[]);mode=data.get('mode')
            if not isinstance(ids,list) or len(ids)>100 or mode not in {'map','follow','classify','ignored','later'}:
                raise WebError('审核操作无效')
            for key in ids:
                row=db.execute('SELECT * FROM market_novelty WHERE id=?',(key,)).fetchone()
                if not row:raise WebError('审核记录不存在')
                target=data.get('target','') if mode=='map' else ''
                if mode=='map' and target not in taxonomy.catalog(d['content'].get('taxonomy',{})):
                    raise WebError('请选择目标类别')
                parent=data.get('parent') or row['parent']
                if parent not in taxonomy.GROUPS:raise WebError('上级类别无效')
                interface.clear_novel_choice(d,key)
                if mode in {'map','follow','classify'}:
                    suggestion=json.loads(row['suggestion']);category=parent+'.ext_'+row['id'][:12]
                    d['content'].setdefault('taxonomy',{})[category]={'parent':parent,'name':row['kind'],'terms':suggestion['terms'],'target':target}
                    if mode=='follow':d['content']['sale'].setdefault('categories',[]).append(category)
                    if mode=='classify':d['content']['sale'].setdefault('blocked_categories',[]).append(category)
                    d['content'].setdefault('review_actions',{})[key]='classified'
                else:d['content'].setdefault('review_actions',{})[key]=mode
            interface.save(db,d)
            return _state(db,session['csrf'])
        if action=='check':return interface.check_post(db,query.get('url',''))
        raise WebError('接口不存在',404)


def create_app(store=None, secure=True):
    store=store or Store()
    with store.box.connect() as db:web_auth.initialize(db)
    async def api(request: Request):
        action=request.path_params['action']
        reads={'state','options','reviews','check'}
        if request.method!=('GET' if action in reads else 'POST'):
            return JSONResponse({'error':'请求方式无效'},405)
        try:
            raw=await request.body()
            if len(raw)>250_000:raise WebError('请求内容过大',413)
            try:data=json.loads(raw) if raw else {}
            except (ValueError,UnicodeDecodeError):raise WebError('请求格式无效') from None
            if not isinstance(data,dict):raise WebError('请求格式无效')
            origin=request.headers.get('origin','')
            parsed=urlsplit(str(request.url));expected=parsed.scheme+'://'+parsed.netloc
            result=await run_in_threadpool(_dispatch,store,action,request.method,data,dict(request.query_params),
                request.cookies.get(web_auth.COOKIE),request.headers.get('x-csrf-token'),origin,expected)
            if '_session' in result:
                token,csrf=result['_session'];response=JSONResponse({'csrf':csrf})
                response.set_cookie(web_auth.COOKIE,token,max_age=7*86400,path=BASE,httponly=True,secure=secure,samesite='strict')
            else:response=JSONResponse(result)
            if action=='logout':response.delete_cookie(web_auth.COOKIE,path=BASE)
            return response
        except WebError as error:return JSONResponse({'error':error.text},error.status)
        except (ValueError,KeyError,TypeError):return JSONResponse({'error':'筛选内容无效，请检查后重试'},400)
    async def page(request):
        name=request.path_params.get('name','index.html')
        if name not in {'index.html','market.js','market.css'}:return JSONResponse({'error':'不存在'},404)
        return FileResponse(Path(__file__).with_name('static')/name)
    app=Starlette(routes=[Route(BASE,page),Route(BASE+'api/{action}',api,methods=['GET','POST']),Route(BASE+'{name}',page)])
    async def headers(request,call_next):
        response=await call_next(request)
        response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer',
            'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
            'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"})
        return response
    app.add_middleware(BaseHTTPMiddleware,dispatch=headers)
    return app
