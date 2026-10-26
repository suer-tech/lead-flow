"""Private resumable research state, separate from Iva's finance/schedule DB."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sqlite3
import uuid


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,campaign TEXT NOT NULL,slot TEXT NOT NULL,
          config_hash TEXT NOT NULL,state TEXT NOT NULL,model_calls INTEGER NOT NULL DEFAULT 0,
          created TEXT NOT NULL,updated TEXT NOT NULL,error TEXT,UNIQUE(campaign,slot));
        CREATE TABLE IF NOT EXISTS queries(run_id TEXT NOT NULL,position INTEGER NOT NULL,query TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(run_id,position));
        CREATE TABLE IF NOT EXISTS candidates(run_id TEXT NOT NULL,domain TEXT NOT NULL,data TEXT NOT NULL,
          stage TEXT NOT NULL DEFAULT 'fetch',assessment TEXT,review TEXT,validation TEXT,result TEXT,
          lead_ack TEXT,draft_ack TEXT,error TEXT,PRIMARY KEY(run_id,domain));
        CREATE TABLE IF NOT EXISTS pages(run_id TEXT NOT NULL,url TEXT NOT NULL,data TEXT NOT NULL,
          PRIMARY KEY(run_id,url));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,at TEXT NOT NULL,
          stage TEXT NOT NULL,code TEXT NOT NULL);
        """)
        # Additive migration: saved lead/CRM acknowledgements remain intact.
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(candidates)")}
        for name, definition in (("letter", "TEXT"), ("letter_review", "TEXT"), ("letter_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("followup", "TEXT"), ("followup_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("citation_repair", "TEXT"), ("citation_attempts", "INTEGER NOT NULL DEFAULT 0")):
            if name not in columns:
                self.db.execute("ALTER TABLE candidates ADD COLUMN " + name + " " + definition)
        run_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(runs)")}
        for name, definition in (("excluded_domains", "TEXT NOT NULL DEFAULT '[]'"), ("research_state", "TEXT"), ("research_error", "TEXT")):
            if name not in run_columns:
                self.db.execute("ALTER TABLE runs ADD COLUMN " + name + " " + definition)
        query_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(queries)")}
        for name, definition in (("candidate_quota", "INTEGER"), ("search_exclusions", "TEXT")):
            if name not in query_columns:
                self.db.execute("ALTER TABLE queries ADD COLUMN " + name + " " + definition)
        self.db.commit()

    def close(self):
        self.db.close()

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def run(self, run_id):
        return self.rows("SELECT * FROM runs WHERE id=?", (run_id,))[0]

    def start(self, config, slot, fingerprint):
        pending = self.rows("SELECT * FROM runs WHERE campaign=? AND state IN ('running','failed','waiting_resources','pending_publish') ORDER BY created LIMIT 1", (config["campaign"],))
        existing = pending or self.rows("SELECT * FROM runs WHERE campaign=? AND slot=?", (config["campaign"], slot))
        if existing:
            if existing[0]["config_hash"] != fingerprint:
                if existing[0]["state"] in ("completed", "completed_with_gaps"):
                    return {**existing[0], "saved_result_only": True}
                raise ValueError("config_changed_during_saved_run")
            return existing[0]
        ident = uuid.uuid4().hex
        cutoff = (datetime.now(timezone.utc) - timedelta(days=config.get("company_recheck_days", 30))).isoformat(timespec="seconds")
        # Freeze the bounded history for this run; resumption cannot change its
        # search assignment because another cycle was added later.
        recent = self.rows("""SELECT c.domain,MAX(r.created) AS seen FROM candidates c JOIN runs r ON r.id=c.run_id
            WHERE r.campaign=? AND r.created>=? AND r.state IN ('completed','completed_with_gaps')
            AND c.stage IN ('preview','published','suppressed','known_company','review_needed','letter_review_needed','crm_review_needed','rejected','draft_rejected')
            GROUP BY c.domain ORDER BY seen DESC,c.domain LIMIT 4096""", (config["campaign"], cutoff))
        excluded = [row["domain"] for row in recent]
        with self.db:
            self.db.execute("INSERT INTO runs(id,campaign,slot,config_hash,state,created,updated,excluded_domains) VALUES(?,?,?,?,?,?,?,?)",
                (ident, config["campaign"], slot, fingerprint, "running", now(), now(), dumps(excluded)))
            self.db.executemany("INSERT INTO queries(run_id,position,query) VALUES(?,?,?)",
                [(ident, index, query) for index, query in enumerate(config["queries"])])
        return self.run(ident)

    def state(self, ident, state, error=None):
        with self.db:
            self.db.execute("UPDATE runs SET state=?,error=?,updated=? WHERE id=?", (state, error, now(), ident))

    def finish_research(self, ident, outcome, error=None, publish_pending=False):
        if outcome not in ("completed", "completed_with_gaps"):
            raise ValueError("invalid_research_outcome")
        with self.db:
            self.db.execute("UPDATE runs SET state=?,research_state=?,research_error=?,error=?,updated=? WHERE id=?",
                ("pending_publish" if publish_pending else outcome, outcome, error, error, now(), ident))

    def event(self, ident, stage, code):
        with self.db:
            self.db.execute("INSERT INTO events(run_id,at,stage,code) VALUES(?,?,?,?)", (ident, now(), stage, code))
            self.db.execute("UPDATE runs SET updated=? WHERE id=?", (now(), ident))

    def reserve_model(self, ident, limit):
        with self.db:
            updated = self.db.execute("UPDATE runs SET model_calls=model_calls+1,updated=? WHERE id=? AND model_calls<?", (now(), ident, limit))
            if updated.rowcount != 1:
                raise RuntimeError("model_budget_exhausted")

    def update_candidate(self, ident, host, **fields):
        allowed = {"stage", "data", "assessment", "review", "validation", "result", "lead_ack", "draft_ack", "error", "letter", "letter_review", "letter_attempts", "followup", "followup_attempts", "citation_repair", "citation_attempts"}
        if set(fields) - allowed:
            raise ValueError("unknown_candidate_field")
        values = [dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values()]
        with self.db:
            self.db.execute("UPDATE candidates SET " + ",".join(key + "=?" for key in fields) + " WHERE run_id=? AND domain=?", [*values, ident, host])

    def summary(self, ident=None):
        runs = self.rows("SELECT * FROM runs " + ("WHERE id=? " if ident else "") + "ORDER BY created DESC LIMIT 10", (ident,) if ident else ())
        for run in runs:
            run["excluded_company_count"] = len(json.loads(run.pop("excluded_domains", "[]")))
            events = self.rows("SELECT at,stage,code FROM events WHERE run_id=? ORDER BY id DESC LIMIT 1", (run["id"],))
            run["last_event"] = events[0] if events else None
            run["candidates"] = self.rows("SELECT stage,COUNT(*) AS count FROM candidates WHERE run_id=? GROUP BY stage", (run["id"],))
            run["pages"] = self.db.execute("SELECT COUNT(*) FROM pages WHERE run_id=?", (run["id"],)).fetchone()[0]
            run["published_leads"] = self.db.execute("SELECT COUNT(*) FROM candidates WHERE run_id=? AND lead_ack IS NOT NULL", (run["id"],)).fetchone()[0]
            run["published_drafts"] = self.db.execute("SELECT COUNT(*) FROM candidates WHERE run_id=? AND draft_ack IS NOT NULL", (run["id"],)).fetchone()[0]
        return {"runs": runs, "email_sending": False}

    @contextmanager
    def exclusive(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "a+b") as stream:
            os.chmod(lock_path, 0o600)
            if os.name == "nt":
                import msvcrt
                if stream.tell() == 0:
                    stream.write(b"0"); stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise RuntimeError("worker_already_running") from None
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("worker_already_running") from None
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)
