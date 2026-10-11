"""Adapter hooks for public inputs discarded before downstream queues."""
import json
import time
from pylon.outbox import Outbox
from . import store


def v2ex(posts, *, box=None):
    import re
    box=box or Outbox()
    with box.connect() as db:
        for post in posts:
            topic=re.search(r'/t/(\d+)',post.v2ex_url)
            source_id=topic[1] if topic else str(post.message_id)
            store.capture(db,'v2ex',source_id,lane='intel',title=post.title,
                body=post.body_full or post.body_excerpt,url=post.v2ex_url,published_at=post.posted_at,
                evidence_scope='forum_body' if post.body_complete else 'excerpt')


def xianbao(post, native_db, *, box=None, decision=None, duplicate='', cached_reason=''):
    from dataclasses import asdict
    from pylon.tasks.xianbao.worker import full_url
    box = box or Outbox()
    source_id = str(post.get('id') or full_url(post))
    old = native_db.execute('SELECT min(first_seen_at) FROM seen_posts WHERE id=?', (source_id,)).fetchone()
    first_seen = old[0] if old and old[0] else time.time()
    post['_review_first_seen']=first_seen
    with box.connect() as db:
        sid = store.capture(db, 'xianbao', source_id, lane='intel', title=str(post.get('title') or ''),
                            body=str(post.get('body') or post.get('content') or ''), url=full_url(post),
                            published_at=str(post.get('published_at') or post.get('time') or ''), first_seen=first_seen,
                            evidence_scope='forum_body' if post.get('body') else 'excerpt')
        if not sid: return
        if duplicate:
            store.record(db, 'xianbao', source_id, lane='intel', status='duplicate',
                         result={'reason':'活动或内容重复','duplicate_of':duplicate,'native':asdict(decision) if decision else {}})
        elif cached_reason:
            existing = db.execute("SELECT 1 FROM review_decisions WHERE snapshot_id=? AND status<>'pending'", (sid,)).fetchone()
            if not existing:
                previous = native_db.execute('SELECT decision_keep,tier,score,reason,rules_hash FROM seen_posts WHERE id=? ORDER BY last_seen_at DESC LIMIT 1', (source_id,)).fetchone()
                if previous:
                    store.record(db, 'xianbao', source_id, lane='intel', status='unknown' if previous[0] else 'excluded',
                                 result={'reason':previous[3],'tier':previous[1],'score':previous[2],'cached_reason':cached_reason},
                                 rule_version=previous[4], restored=True)


def intel_decision(db, item):
    """Capture both regular results and trade routing outcomes."""
    value = dict(item)
    result = json.loads(value['result']) if value.get('result') else {}
    if value.get('error'):
        result = {'reason':value['error'],'phase':value['phase'],'attempts':value['attempts'],'failed_at':value['updated_at']}
        from pylon.tasks.intelligence.attempts import history
        rows=history(db,value,value['phase'])
        if rows:result.update(request_id=rows[-1]['request_id'],elapsed=rows[-1]['elapsed'],failed_at=rows[-1]['finished_at'])
    return store.record(db,value['source'],value['source_id'],lane='intel',status=value['status'],result=result,
                        event_id=value.get('event_id') if value['status'] in {'queued','delivered'} else None,
                        rule_version=value.get('policy_version'),decided_at=value['updated_at'])
