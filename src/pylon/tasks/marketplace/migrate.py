"""Scoped migration: preserve trading history; revoke retired CHH controls."""
import json
import time
from pylon.config import profile_env
from pylon.transport import client,telegram_request
from .store import Store
from . import control


def retire_cards(store,*,http=None):
    if http is None:
        with client() as connection:return retire_cards(store,http=connection)
    results=[]
    with store.box.connect() as db:
        rows=db.execute("SELECT * FROM events WHERE job_id='chiphell-category-review'").fetchall()
    for row in rows:
        previous=store.box.get('market:retired-card:'+row['id'])
        if previous and previous.get('status')=='removed':continue
        payload=json.loads(row['payload']);env=profile_env(row['profile'])
        outcome={'status':'no_buttons','at':time.time()}
        if row['receipt'] and payload.get('reply_markup'):
            receipts=json.loads(row['receipt']);receipts=receipts if isinstance(receipts,list) else [receipts]
            try:
                for receipt in receipts:
                    if str(receipt.get('chat_id'))!=str(env.get('TELEGRAM_HOME_CHANNEL')):raise ValueError('receipt_owner_mismatch')
                    telegram_request(http,env['TELEGRAM_BOT_TOKEN'],'editMessageReplyMarkup',data={
                        'chat_id':receipt['chat_id'],'message_id':receipt['message_id'],
                        'reply_markup':json.dumps({'inline_keyboard':[]})},read_only=True)
                outcome['status']='removed'
            except Exception as exc:
                if 'message is not modified' in str(exc):outcome['status']='removed'
                else:outcome={'status':'uneditable','error':type(exc).__name__,'at':time.time()}
        store.box.put('market:retired-card:'+row['id'],outcome);results.append(outcome)
    with store.box.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        control.baseline(db)
        db.execute("UPDATE events SET status='suppressed',error='retired_chh_review',updated_at=? WHERE job_id='chiphell-category-review' AND status IN ('pending','failed')",(time.time(),))
        db.execute("DELETE FROM kv WHERE key LIKE 'chiphell:unknown-review:%' OR key IN ('chiphell:unknown-last-prompt','chiphell:unknown-baseline')")
        # Past records remain reviewable; only the separately authorized
        # fresh-refetch path can make a recent missed record runnable again.
        db.execute("""INSERT OR IGNORE INTO market_reviews
            SELECT p.source,p.source_id,-1,'旧版分析未能确认：'||a.error,'pending',?
            FROM marketplace_posts p JOIN marketplace_analyses a USING(source,source_id)
            WHERE p.history=0 AND p.status='pending_analysis' AND a.status='error'""",(time.time(),))
        db.execute("""UPDATE marketplace_posts SET status='needs_review' WHERE history=0 AND status='pending_analysis'
            AND EXISTS(SELECT 1 FROM marketplace_analyses a WHERE a.source=marketplace_posts.source
              AND a.source_id=marketplace_posts.source_id AND a.status='error')""")
    return results


def main():
    results=retire_cards(Store())
    print(json.dumps({'retired_cards':len(results),'uneditable':sum(r['status']=='uneditable' for r in results)}))
    return 0


if __name__=='__main__':raise SystemExit(main())
