"""Read-only, offline comparison of retained trade evidence; no sender imports."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path
from . import classifier, policy, wanted, enrich


def audit(paths, before, after):
    records={};duplicates=0;linux_rows={}
    for path in paths:
        with sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row
            for raw in db.execute('SELECT * FROM marketplace_posts'):
                row=dict(raw);identity=(row['source'],row['source_id']);prior=records.get(identity)
                if prior:duplicates+=1
                if not prior or (row['body_complete'],row['updated_at'])>(prior['body_complete'],prior['updated_at']):records[identity]=row
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='linuxdo_topics'").fetchone():
                for row in db.execute('''SELECT t.topic_id,t.title,t.updated_at,t.historical,e.ref,e.body,e.complete
                    FROM linuxdo_topics t JOIN linuxdo_evidence e USING(topic_id)
                    WHERE e.post_number=1 AND e.origin IN ('telegram','topic') ORDER BY e.complete,e.observed_at'''):
                    linux_rows[row['topic_id']]=dict(row)
    for sid,row in linux_rows.items():
        records.setdefault(('linuxdo',sid),dict(source='linuxdo',source_id=sid,title=row['title'],body=row['body'],
            body_complete=row['complete'],updated_at=row['updated_at'],items='[]',platform_category='',
            status='historical',history=1,body_provenance=''))
    stats=Counter();coverage={};examples={key:[] for key in ('newly_matched','new_exclusion','price_changed','category_changed','body_suspect')}
    for identity,row in records.items():
        source=coverage.setdefault(row['source'],Counter());source['posts']+=1
        source['original_complete' if row['body_complete'] else 'missing_or_excerpt']+=1
        provenance=json.loads(row.get('body_provenance') or '{}')
        source['identity_verified' if provenance.get('identity_verified') else 'legacy_identity_unverified']+=1
        # Explicit known embedded UI modules are contamination evidence; general
        # prose mentioning login or a product is not enough to reject a post.
        from .content_quality import contamination
        suspect=bool(contamination(row['source'],row['title'],row['body']))
        complete=bool(row['body_complete']) and not suspect
        if suspect:source['body_suspect']+=1
        old=json.loads(row['items']);new=classifier.classify_listing(row['title'],row['body'],row['source'],complete,
            platform_category=row.get('platform_category',''))
        def decisions(items,content):
            result=[]
            for item in items:
                if item['status']=='buy':
                    result.append('match' if wanted.evaluate([item],content['wanted']) else 'exclude')
                else:result.append(policy.decision(item,content['sale'])[0])
            return result
        old_decisions=decisions(old,before);new_decisions=decisions(new,after)
        old_match='match' in old_decisions;new_match='match' in new_decisions
        stats['posts']+=1;stats['old_match']+=old_match;stats['new_match']+=new_match
        for state in new_decisions:stats['new_items_'+state]+=1
        analysis=enrich.needs_analysis(row,new)
        stats['needs_semantic_analysis']+=analysis
        stats['direct_match']+=new_match and not analysis
        states={'newly_matched':new_match and not old_match,'new_exclusion':old_match and not new_match,
            'category_changed':[(i['category'],i['status']) for i in old]!=[(i['category'],i['status']) for i in new],
            'price_changed':[i['price'] for i in old]!=[i['price'] for i in new],'body_suspect':suspect}
        for key,changed in states.items():
            stats[key]+=bool(changed)
            if changed and len(examples[key])<12:
                examples[key].append({'source':identity[0],'id':identity[1],
                    'before':[[i['category'],i['price']] for i in old],
                    'after':[[i['category'],i['price']] for i in new],'needs_analysis':analysis})
    return {'counts':dict(stats),'coverage':{k:dict(v) for k,v in coverage.items()},'duplicates':duplicates,
            'examples':examples,'notifications_created':0,
            'scope':'local deterministic replay; unresolved cases are not counted as verified notifications'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('databases',nargs='+',type=Path)
    parser.add_argument('--before',type=Path,required=True);parser.add_argument('--after',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(audit(args.databases,json.loads(args.before.read_text()),json.loads(args.after.read_text())),ensure_ascii=False))


if __name__=='__main__':main()
