from __future__ import annotations

import json
from . import feedback, store


def dispatch(db, action, data, query, csrf):
    from pylon.tasks.marketplace.web import WebError
    store.initialize(db)
    try:
        if action == 'state':
            store.sync_deliveries(db)
            days = [dict(r) for r in db.execute('SELECT day,count(*) count FROM review_entries GROUP BY day ORDER BY day DESC')]
            categories = [r[0] for r in db.execute('SELECT DISTINCT value FROM review_entries,json_each(categories) ORDER BY value')]
            drafts = [{**dict(r), 'value': json.loads(r['value'])} for r in db.execute('''SELECT f.*,r.title FROM review_feedback f
                JOIN review_entries r ON r.id=f.entry_id WHERE owner='webops' AND batch_id IS NULL ORDER BY f.updated_at DESC''')]
            from pylon.tasks.marketplace.taxonomy import catalog
            from pylon.tasks.intelligence.policy import CATALOG
            category_labels={**catalog(),**CATALOG}
            return {'csrf': csrf, 'days': days, 'sources': store.SOURCES, 'statuses': store.STATUSES,'category_labels':category_labels,
                    'categories': categories, 'actions': feedback.ACTIONS, 'scopes': feedback.SCOPES,
                    'drafts': drafts, 'batches': feedback.batches(db),
                    'coverage': {'entries': sum(r['count'] for r in days),
                        'origins': db.execute('SELECT count(*) FROM review_origins').fetchone()[0],
                        'snapshots': db.execute('SELECT count(*) FROM review_snapshots').fetchone()[0],
                        'restored_decisions': db.execute('SELECT count(*) FROM review_decisions WHERE restored=1').fetchone()[0],
                        'unknown_input_versions': db.execute("SELECT count(*) FROM review_snapshots WHERE json_extract(data,'$.metadata.original_input_version_unknown')=1").fetchone()[0],
                        'missing_decisions': db.execute('SELECT count(*) FROM review_entries WHERE status=\'unknown\'').fetchone()[0],
                        'unknown_dates': db.execute('SELECT count(*) FROM review_entries WHERE day=\'unknown\'').fetchone()[0]}}
        if action == 'items': return store.items(db, query)
        if action == 'detail': return store.detail(db, query.get('id', ''))
        if action == 'feedback': return feedback.save(db, data.get('targets'), data.get('value'))
        if action == 'discard': return feedback.discard(db, data.get('targets', []))
        if action == 'submit': return feedback.submit(db, data.get('targets'))
        if action == 'batch':
            row = db.execute('SELECT * FROM review_batches WHERE id=? AND owner=\'webops\'', (query.get('id', ''),)).fetchone()
            if not row: raise WebError('反馈批次不存在', 404)
            return {**dict(row), 'result': json.loads(row['result']), 'feedback': feedback.results(db, batch_id=row['id'])}
        raise WebError('接口不存在', 404)
    except feedback.Conflict as error: raise WebError(str(error), 409) from None
    except ValueError as error: raise WebError(str(error)) from None
