from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tarfile

from maintenance import maintain
from worker.store import Store


def test_backup_and_compaction_keep_checkpoints_and_evidence(tmp_path):
    path = tmp_path / 'research.sqlite3'
    store = Store(path)
    for state in ('completed', 'running', 'failed'):
        store.db.execute("INSERT INTO runs(id,campaign,slot,config_hash,state,created,updated) VALUES(?,?,?,?,?,?,?)",
            (state, state, 'day', 'hash', state, '2026-01-01', '2026-01-01'))
        store.db.execute('INSERT INTO pages VALUES(?,?,?)', (state, 'https://example.org', json.dumps({'text': 'evidence body', 'content_hash': 'digest'})))
        store.db.execute('INSERT INTO candidates(run_id,domain,data,result) VALUES(?,?,?,?)', (state,'example.org','{}','{"quote":"evidence body"}'))
    store.db.commit(); store.close()
    result = maintain(path, [], tmp_path/'backups', clock=datetime(2026,10,26,tzinfo=timezone.utc), reserve=0)
    assert result['source_bodies_compacted'] == 1
    with sqlite3.connect(path) as db:
        assert json.loads(db.execute("SELECT data FROM pages WHERE run_id='completed'").fetchone()[0])['text'] == ''
        assert json.loads(db.execute("SELECT data FROM pages WHERE run_id='running'").fetchone()[0])['text'] == 'evidence body'
        assert json.loads(db.execute("SELECT data FROM pages WHERE run_id='failed'").fetchone()[0])['text'] == 'evidence body'
        assert db.execute('SELECT count(*) FROM candidates WHERE result LIKE ?', ('%evidence body%',)).fetchone()[0] == 3
    with tarfile.open(tmp_path/'backups'/result['backup']) as archive:
        member = archive.getmember('0-research.sqlite3')
        target = tmp_path/'restored.sqlite3'
        target.write_bytes(archive.extractfile(member).read())
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert json.loads(db.execute("SELECT data FROM pages WHERE run_id='completed'").fetchone()[0])['text'] == 'evidence body'


def test_missing_db_does_not_create_state(tmp_path):
    assert maintain(tmp_path/'absent.sqlite3', [], tmp_path/'backups')['state'] == 'no_research_database'
    assert not (tmp_path/'absent.sqlite3').exists()


def test_mail_journal_backed_up_without_research_database(tmp_path):
    path = tmp_path/'mail-dispatch.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE attempts(id TEXT PRIMARY KEY,state TEXT)')
        db.execute("INSERT INTO attempts VALUES('id','uncertain')")
    result = maintain(tmp_path/'missing.sqlite3', [path], tmp_path/'backups', reserve=0)
    assert result['databases'] == 1
    assert result['source_bodies_compacted'] == 0
    assert not (tmp_path/'missing.sqlite3').exists()
