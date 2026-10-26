"""Write-ahead record for SMTP attempts. Never retries a recorded attempt."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DispatchStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("state_path_must_not_be_symlink")
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts(
              outreach_id TEXT PRIMARY KEY, message_id TEXT UNIQUE NOT NULL,
              claim_token TEXT NOT NULL, state TEXT NOT NULL, result_json TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY,state TEXT NOT NULL,summary_json TEXT NOT NULL,
              started_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def lock(self):
        handle = open(str(self.path) + ".lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0); handle.write(b"0"); handle.flush(); handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                os.fchmod(handle.fileno(), 0o600)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            handle.close()

    def bind(self, config):
        identity = hashlib.sha256(json.dumps([config.crm_url.rstrip("/"), config.mail.sender.lower(),
            config.mail.mailbox, str(Path(config.mail.state_path).resolve())]).encode()).hexdigest()
        with self.connect() as db:
            old = db.execute("SELECT value FROM settings WHERE key='identity'").fetchone()
            if old and old[0] != identity:
                raise ValueError("mail_state_identity_changed")
            db.execute("INSERT OR IGNORE INTO settings VALUES('identity',?)", (identity,))

    def register(self, message):
        # Commit BEFORE calling SMTP. A crash after here is uncertain, even if
        # network I/O in fact never happened: absence of a receipt cannot prove it.
        with self.connect() as db:
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?)", (message["id"], message["message_id"],
                message["claim_token"], "started", None, now(), now()))

    def result(self, outreach_id, payload):
        with self.connect() as db:
            db.execute("UPDATE attempts SET state='pending_ack',result_json=?,updated_at=? WHERE outreach_id=?",
                       (json.dumps(payload, sort_keys=True), now(), outreach_id))

    def mark(self, outreach_id, state):
        if state not in {"acknowledged", "reconciliation_required"}:
            raise ValueError("invalid_attempt_state")
        with self.connect() as db:
            db.execute("UPDATE attempts SET state=?,updated_at=? WHERE outreach_id=?", (state, now(), outreach_id))

    def pending(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM attempts WHERE state!='acknowledged' ORDER BY created_at,outreach_id")]

    def has_attempts(self):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM attempts LIMIT 1").fetchone() is not None

    def run(self, run_id, state, summary):
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state,summary_json=excluded.summary_json,updated_at=excluded.updated_at",
                (run_id, state, json.dumps(summary, ensure_ascii=False), now(), now()))
            # Bounded diagnostics; immutable attempt dedup is retained.
            db.execute("DELETE FROM runs WHERE id NOT IN (SELECT id FROM runs ORDER BY started_at DESC LIMIT 100)")

    def status(self):
        with self.connect() as db:
            last = db.execute("SELECT id,state,summary_json,started_at,updated_at FROM runs ORDER BY started_at DESC,rowid DESC LIMIT 1").fetchone()
            states = {row["state"]: row["n"] for row in db.execute("SELECT state,COUNT(*) AS n FROM attempts GROUP BY state")}
        result = dict(last) if last else None
        if result:
            result["summary"] = json.loads(result.pop("summary_json"))
        return {"attempt_states": states, "last_run": result}
