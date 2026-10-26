"""Explicit, resumable revision of never-attempted letters; never sends email."""
import argparse
from contextlib import closing
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
from urllib.error import HTTPError

from .contracts import LETTER_REVIEW_FLAGS
from .engine import load_config
from .probes.letter_draft import run_probe
from .runtime import CrmClient
from .store import now

VERSION='ai-benchmark-v1'


def content_hash(value):
    return hashlib.sha256(json.dumps({'subject':value['subject'],'body':value['body']},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def save(path,value):
    temp=path.with_name(path.name+'.new')
    with open(temp,'w',encoding='utf-8') as f:
        os.chmod(temp,0o600);json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(temp,path)


def refresh(config_path,directory):
    config=load_config(config_path);directory=Path(directory);directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    mail=json.loads(Path('/etc/lead-flow/mail.json').read_text())
    if mail.get('send_enabled') is not False:raise ValueError('Pause mail sending before revision')
    attempts=Path('/opt/iva/data/lead-flow/mail-dispatch.sqlite3')
    with closing(sqlite3.connect(attempts.as_uri()+'?mode=ro',uri=True)) as db:
        attempted={r[0] for r in db.execute('SELECT outreach_id FROM attempts')}
    plan_path=directory/'plan.json'
    if plan_path.exists():plan=json.loads(plan_path.read_text())
    else:
        plan=[];seen=set()
        with closing(sqlite3.connect(Path(config['database']).as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            for row in db.execute('SELECT c.*,r.created FROM candidates c JOIN runs r ON r.id=c.run_id WHERE c.draft_ack IS NOT NULL ORDER BY r.created DESC,c.domain'):
                ack=json.loads(row['draft_ack']);ident=ack.get('id')
                if not isinstance(ident,str) or not re.fullmatch('[a-f0-9]{32}',ident) or ident in attempted or ident in seen:continue
                result=json.loads(row['result']);seen.add(ident)
                plan.append({'id':ident,'source_run':row['run_id'],'domain':row['domain'],
                    'company':result['lead']['company'],'old_hash':content_hash(result['draft'])})
        if len(plan)>100:raise ValueError('Revision batch too large')
        save(plan_path,plan)
    crm=CrmClient(config);results=[]
    for item in plan:
        status_path=directory/(item['id']+'.status.json')
        previous=json.loads(status_path.read_text()) if status_path.exists() else {}
        if previous.get('published'):
            results.append(previous);continue
        output=directory/(item['id']+'.sqlite3');report_path=output.with_suffix('.report.json')
        if report_path.exists():report=json.loads(report_path.read_text())
        elif output.exists():
            results.append({'id':item['id'],'published':False,'error':'probe_checkpoint_requires_operator_resume'});continue
        else:report=run_probe(config_path,config['database'],item['source_run'],item['domain'],output)
        if not report.get('passed'):
            failure={'id':item['id'],'published':False,'error':'independent_review_not_passed'};save(status_path,failure);results.append(failure);continue
        draft=report['result']['draft'];review=report['letter_review']
        payload={'idempotency_key':VERSION+':'+item['id'],'outreach_id':item['id'],'expected_content_hash':item['old_hash'],
            'subject':draft['subject'],'body':draft['body'],'review_checks':{key:review[key] for key in ('approved',*LETTER_REVIEW_FLAGS)}}
        save(directory/(item['id']+'.request.json'),payload)
        try:
            ack=crm.post('/api/agent/drafts/revise',payload)
            if ack.get('id')!=item['id'] or ack.get('revised') is not True:raise ValueError('Revision acknowledgement invalid')
        except Exception as exc:
            failure={'id':item['id'],'published':False,'error':type(exc).__name__};save(status_path,failure);results.append(failure);continue
        # Append current-copy metadata without rewriting historical research.
        with closing(sqlite3.connect(config['database'])) as db:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS outreach_copy_overrides(outreach_id TEXT PRIMARY KEY,copy_version TEXT NOT NULL,draft_json TEXT NOT NULL,ack_json TEXT NOT NULL,updated_at TEXT NOT NULL)')
                db.execute('INSERT INTO outreach_copy_overrides VALUES(?,?,?,?,?) ON CONFLICT(outreach_id) DO UPDATE SET copy_version=excluded.copy_version,draft_json=excluded.draft_json,ack_json=excluded.ack_json,updated_at=excluded.updated_at',
                    (item['id'],VERSION,json.dumps({'subject':draft['subject'],'body':draft['body']},ensure_ascii=False),json.dumps(ack),now()))
        result={'id':item['id'],'company':item['company'],'published':True,'ack':ack,'subject':draft['subject'],'body':draft['body'],
            'source_unchanged':report['source_snapshot_unchanged'],'review_checks':payload['review_checks']}
        save(status_path,result);results.append(result)
        print(json.dumps({'reviewed_and_revised':item['domain'],'completed':sum(r.get('published') is True for r in results),'total':len(plan)}),flush=True)
    final={'copy_version':VERSION,'planned':len(plan),'revised':sum(r.get('published') is True for r in results),'skipped_recorded_attempts':len(attempted),
        'passed':len(results)==len(plan) and all(r.get('published') for r in results),'items':results,'smtp_calls':0}
    save(directory/'final.json',final)
    return final


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--directory',required=True)
    args=parser.parse_args()
    import fcntl
    folder=Path(args.directory);folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    with open(folder/'refresh.lock','a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        report=refresh(args.config,args.directory)
    print(json.dumps({key:report[key] for key in ('passed','planned','revised','smtp_calls')}))
    raise SystemExit(0 if report['passed'] else 1)
