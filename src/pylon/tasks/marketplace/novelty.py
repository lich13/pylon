"""New product kinds require an owner's decision, never model-driven opt-in."""
from __future__ import annotations

import json
import re
import time
from . import control
from .taxonomy import CATEGORIES,GROUPS


def normal(value):
    return re.sub(r'[\W\d_]+','',value).casefold()


def validate_suggestion(value,category,evidence):
    from .enrich import AnalysisError
    if category!='unknown' or not isinstance(value,dict):raise AnalysisError('novelty_requires_unknown_kind')
    kind=value.get('kind','');parent=value.get('parent','');quote=value.get('evidence','');terms=value.get('terms',[])
    if (not isinstance(kind,str) or not 2<=len(kind)<=30 or re.search(r'\d',kind)
            or parent not in GROUPS or not isinstance(quote,str) or not quote or quote not in evidence
            or not isinstance(value.get('difference'),str) or len(value['difference'])<8
            or not isinstance(terms,list) or not 1<=len(terms)<=5
            or any(not isinstance(t,str) or not 2<=len(t)<=20 or t not in quote or re.search(r'\d',t) for t in terms)):
        raise AnalysisError('invalid_novelty_evidence')
    from .classifier import category_for
    if category_for(kind)[0]!='unknown':raise AnalysisError('existing_kind_is_not_novel')
    if any(normal(kind)==normal(label.split('/')[-1]) for label in CATEGORIES.values()):
        raise AnalysisError('existing_kind_is_not_novel')
    return {k:value[k] for k in ('kind','parent','difference','evidence','terms')}


def record(db,post,item):
    if post.get('history') or not post.get('body_complete'):return
    proposal=item['attributes']['novelty'];kind=proposal['kind'];parent=proposal['parent']
    identity=control.digest([parent,normal(kind)])[:20]
    # A stable noun or a prior proposed synonym joins the same review even
    # when price, location and title differ. The model also sees prior kinds.
    for row in db.execute('SELECT * FROM market_novelty WHERE parent=?',(parent,)):
        previous=__import__('json').loads(row['suggestion'])
        if ({normal(t) for t in previous['terms']} & {normal(t) for t in proposal['terms']}
                or normal(row['kind'])==normal(kind)):
            identity=row['id'];break
    old=db.execute('SELECT * FROM market_novelty WHERE id=?',(identity,)).fetchone()
    scope='wanted' if item['status']=='buy' else 'sale'
    ref={'source':post['source'],'id':post['source_id'],'url':post['url'],'subject':item.get('name') or kind,'scope':scope}
    if old:
        posts=__import__('json').loads(old['posts'])
        if not any(p['source']==ref['source'] and p['id']==ref['id'] for p in posts):posts.append(ref)
        db.execute('UPDATE market_novelty SET posts=?,last_seen=? WHERE id=?',(control.dumps(posts[-20:]),time.time(),identity))
    else:
        active=control.baseline(db)[1]
        blocked=parent in active[scope].get('blocked_groups',[])
        db.execute('INSERT INTO market_novelty(id,kind,parent,suggestion,announced,first_seen,last_seen,posts) VALUES(?,?,?,?,?,?,?,?)',
                   (identity,kind,parent,control.dumps(proposal),int(blocked),time.time(),time.time(),control.dumps([ref])))


def main():
    from .store import Store
    from .interface import card
    from .web_auth import public_url
    store=Store()
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        version,active=control.baseline(db)
        pending=db.execute("SELECT id,kind,parent,posts FROM market_novelty WHERE status='pending' AND announced=0 ORDER BY first_seen").fetchall()
        rows=[]
        for row in pending:
            scopes={ref.get('scope','sale') for ref in json.loads(row['posts'])} or {'sale'}
            if all(row['parent'] in active.get(scope,{}).get('blocked_groups',[]) for scope in scopes):
                db.execute('UPDATE market_novelty SET announced=1 WHERE id=?',(row['id'],))
            else:
                rows.append(row)
        if rows:
            payload=card('新增商品种类待确认：'+str(len(rows))+' 种\n'+'、'.join(row['kind'] for row in rows[:30]),
                         [[{'text':'打开交易筛选','url':public_url()}]])
            store.box.enqueue(profile='webops',job_id='marketplace-review',kind='text',payload=payload,
                              key='market-novelty:'+control.digest([row['id'] for row in rows]),connection=db)
            db.executemany('UPDATE market_novelty SET announced=1 WHERE id=?',[(row['id'],) for row in rows])
    print('{"wakeAgent":false}')
    return 0


if __name__=='__main__':raise SystemExit(main())
