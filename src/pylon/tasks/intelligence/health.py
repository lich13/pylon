"""Progress-based incident episodes; item uncertainty is never an outage."""
import json
import time
from pylon.config import redact

SCHEMA='''CREATE TABLE IF NOT EXISTS intel_health (
 component TEXT PRIMARY KEY, blocked_since REAL, last_progress REAL NOT NULL DEFAULT 0,
 open INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', backlog INTEGER NOT NULL DEFAULT 0);'''


def record(box,component,*,backlog,progress=False,error='',now=None):
    now=time.time() if now is None else now
    with box.connect() as db:
        db.execute(SCHEMA);db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT * FROM intel_health WHERE component=?',(component,)).fetchone()
        current=dict(row) if row else {'blocked_since':None,'last_progress':0,'open':0,'error':''}
        if progress or not backlog or not error:
            current.update(blocked_since=None,open=0)
            if progress:current['last_progress']=now
        elif current['blocked_since'] is None:current['blocked_since']=now
        reason=redact(error or (current.get('error','') if backlog and not progress else ''))[:300]
        if backlog and error and current['blocked_since'] is not None and now-current['blocked_since']>=3600 and not current['open']:
            box.enqueue(profile='webops',job_id='intelligence-review',kind='text',
                payload={'text':f'情报处理持续受阻超过 60 分钟：{component}，待处理 {backlog} 条。\n原因：{reason or "工作队列未取得进展"}\n可在每日审核查看具体记录。'},
                key=f'intel:incident:{component}:{current["blocked_since"]}',connection=db)
            current['open']=1
        db.execute('INSERT OR REPLACE INTO intel_health VALUES(?,?,?,?,?,?)',
            (component,current['blocked_since'],current['last_progress'],current['open'],
             reason,backlog))
