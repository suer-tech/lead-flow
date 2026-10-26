"""One explicit new review of saved prospects after a policy change.

The original terminal run is immutable. Discovery never repeats. Saved sources
are reused; budget-interrupted prospects fetch only previously unread URLs.
New reviews/letters and CRM acknowledgements belong to a new real run.
"""
import argparse
import json
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from .contracts import accepted_potential_review, validate_assessment
from .engine import Engine, fingerprint, load_config
from .store import Store, dumps, now


def seed(store, config, source_id):
    if not config.get('discovery_outreach'):
        raise ValueError('discovery_policy_required')
    source=store.run(source_id)
    if source['campaign']!=config['campaign'] or source['state'] not in ('completed','completed_with_gaps'):
        raise ValueError('terminal_same_campaign_source_required')
    slot='reconsider-'+source_id
    prior=store.rows('SELECT * FROM runs WHERE campaign=? AND slot=?',(config['campaign'],slot))
    if prior:
        if prior[0]['config_hash']!=fingerprint(config):raise ValueError('reconsideration_policy_changed')
        return prior[0]['id']
    if store.rows("SELECT id FROM runs WHERE campaign=? AND state IN ('running','failed','waiting_resources','pending_publish')",(config['campaign'],)):
        raise ValueError('finish_pending_research_first')
    selected=[]
    for row in store.rows("SELECT * FROM candidates WHERE run_id=? AND result IS NULL ORDER BY domain",(source_id,)):
        unfinished=source['error']=='model_budget_exhausted' and row['stage'] in (
            'fetch','assess','assessment_checked','citation_repair','followup_search','followup_fetch','review','write_letter','review_letter','letter_ready')
        if unfinished:
            selected.append((row,None,'fetch'));continue
        if row['stage']!='rejected':continue
        review=json.loads(row['review'] or '{}')
        pages=store.rows('SELECT url,data FROM pages WHERE run_id=?',(source_id,))
        candidate=json.loads(row['data'])
        sources=[json.loads(page['data']) for page in pages if page['url'] in candidate['source_urls']]
        check=validate_assessment(candidate,json.loads(row['assessment'] or '{}'),sources,
            datetime.now(ZoneInfo(config['timezone'])).date(),config['max_signal_age_days'])
        if check.get('valid') and accepted_potential_review(review):selected.append((row,check,'review'))
    if not selected:return None
    ident=uuid.uuid4().hex
    # Atomic seed: a process crash never leaves a discoverable empty run.
    with store.db:
        store.db.execute('INSERT INTO runs(id,campaign,slot,config_hash,state,created,updated) VALUES(?,?,?,?,?,?,?)',
            (ident,config['campaign'],slot,fingerprint(config),'running',now(),now()))
        store.db.executemany("INSERT INTO queries(run_id,position,query,state) VALUES(?,?,?,'done')",
            [(ident,index,query) for index,query in enumerate(config['queries'])])
        for row,check,stage in selected[:config['max_candidates']]:
            store.db.execute('INSERT INTO candidates(run_id,domain,data,stage,assessment,validation,followup_attempts,citation_attempts) VALUES(?,?,?,?,?,?,?,?)',
                (ident,row['domain'],row['data'],stage,row['assessment'] if check else None,dumps(check) if check else None,1 if check else 0,1 if check else 0))
            urls=json.loads(row['data'])['source_urls']
            for page in store.rows('SELECT url,data FROM pages WHERE run_id=?',(source_id,)):
                if page['url'] in urls:
                    store.db.execute('INSERT OR IGNORE INTO pages(run_id,url,data) VALUES(?,?,?)',(ident,page['url'],page['data']))
        store.db.execute('INSERT INTO events(run_id,at,stage,code) SELECT ?,updated,?,? FROM runs WHERE id=?',
            (ident,'reconsider','saved_sources_from_'+source_id,ident))
    return ident


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True);parser.add_argument('--source-run',required=True)
    args=parser.parse_args();config=load_config(args.config)
    store=Store(config['database'])
    try:
        with store.exclusive():ident=seed(store,config,args.source_run)
        if ident is None:
            print(json.dumps({'state':'no_reviewable_saved_prospects','models_called':0,'email_sending':False}));return 0
        from .runtime import IvaRuntime,CrmClient
        result=Engine(config,store,IvaRuntime(config),CrmClient(config) if config['mode']=='ingest' else None).run(slot='reconsider-'+args.source_run)
        print(json.dumps(result,ensure_ascii=False));return 1 if result.get('ok') is False else 0
    finally:store.close()


if __name__=='__main__':raise SystemExit(main())
