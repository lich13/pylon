import argparse
import json
from pathlib import Path
from pylon import events
from pylon.outbox import Outbox
from . import feedback, store


def main():
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['digest','migrate','batches','batch','reconcile','retry'])
    p.add_argument('--id');p.add_argument('--status',choices=['processing','fixed','needs_info'])
    p.add_argument('--result-file',type=Path);p.add_argument('--apply',action='store_true')
    args=p.parse_args();box=Outbox()
    if args.command=='digest':
        from .notifications import digest
        result=digest(box)
        events.result('ok' if result['queued_days'] else 'no_change','每日审核汇总已处理')
    elif args.command=='migrate':
        if not args.apply:p.error('暂停相关在途任务后使用 --apply')
        from .migrate import migrate,cap_legacy_retries
        result=migrate(box);result['exhausted_legacy']=cap_legacy_retries(box)
    else:
        with box.connect() as db:
            store.initialize(db)
            if args.command=='batches':result=feedback.batches(db)
            elif args.command=='batch':
                result=[{**dict(r),'value':json.loads(r['value'])} for r in db.execute('SELECT * FROM review_feedback WHERE batch_id=?',(args.id,))]
            elif args.command=='retry':
                if not args.apply:p.error('retry needs --apply')
                from pylon.tasks.intelligence.attempts import retry
                row=db.execute("SELECT source,source_id FROM review_origins WHERE entry_id=? AND source IN ('linuxdo','v2ex')",(args.id,)).fetchone()
                if not row:raise ValueError('未找到可重试的情报输入')
                retry(db,row[0],row[1]);result={'retry':args.id}
            else:
                if not args.id or not args.status or not args.result_file:p.error('reconcile needs --id --status --result-file')
                feedback.update_batch(db,args.id,status=args.status,result=json.loads(args.result_file.read_text()))
                result={'updated':args.id,'status':args.status}
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
