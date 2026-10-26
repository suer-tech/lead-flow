from __future__ import annotations

import hashlib
import imaplib
import os
import re
import sqlite3
import ssl
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from .config import MailConfig


class MailState:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise ValueError("state_path_must_not_be_symlink")
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS cursors(account TEXT, mailbox TEXT,
              uidvalidity TEXT NOT NULL, last_uid INTEGER NOT NULL,
              PRIMARY KEY(account, mailbox));
            CREATE TABLE IF NOT EXISTS seen(account TEXT, digest TEXT,
              message_id TEXT NOT NULL, received_at TEXT NOT NULL,
              PRIMARY KEY(account,digest));
            CREATE TABLE IF NOT EXISTS observed(account TEXT,digest TEXT,
              received_at TEXT NOT NULL,PRIMARY KEY(account,digest));
            """)
        if os.name != "nt":
            self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def cursor(self, config):
        with self.connect() as db:
            return db.execute("SELECT uidvalidity,last_uid FROM cursors WHERE account=? AND mailbox=?",
                              (config.sender.lower(), config.mailbox)).fetchone()

    def set_cursor(self, config, uidvalidity, uid):
        with self.connect() as db:
            db.execute("INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(account,mailbox) DO UPDATE SET uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid",
                       (config.sender.lower(), config.mailbox, uidvalidity, uid))

    def seen(self, config, digest):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM seen WHERE account=? AND digest=?",
                              (config.sender.lower(), digest)).fetchone() is not None

    def first_observed(self, config, digest):
        # Missing/broken Date headers must not change payload_hash after a lost
        # CRM acknowledgement. Persist the fallback before invoking the callback.
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO observed VALUES(?,?,?)",
                       (config.sender.lower(), digest, datetime.now(timezone.utc).isoformat()))
            return db.execute("SELECT received_at FROM observed WHERE account=? AND digest=?",
                              (config.sender.lower(), digest)).fetchone()[0]

    def acknowledge(self, config, uidvalidity, uid, digest, payload):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO seen VALUES(?,?,?,?)",
                       (config.sender.lower(), digest, payload["message_id"], payload["received_at"]))
            db.execute("INSERT INTO cursors VALUES(?,?,?,?) ON CONFLICT(account,mailbox) DO UPDATE SET uidvalidity=excluded.uidvalidity,last_uid=excluded.last_uid",
                       (config.sender.lower(), config.mailbox, uidvalidity, uid))

    @contextmanager
    def lock(self):
        """Cross-process lock held over one polling transaction, not a SQLite write."""
        handle = open(str(self.path) + ".lock", "a+b")
        try:
            if __import__("os").name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            handle.close()


def _ids(value):
    return [item for item in re.findall(r"<[^<>\s]+@[^<>\s]+>", str(value or "")) if len(item) <= 500][:40]


def parse_reply(raw: bytes, *, digest: str, observed_at: str | None = None):
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    sender = getaddresses(msg.get_all("From", []))
    from_email = sender[0][1].lower() if len(sender) == 1 else ""
    # Preserve an unidentifiable message for review without guessing a company
    # address. This reserved domain can never match a claimed business recipient.
    if len(from_email) > 180 or not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", from_email):
        from_email = "unidentified@lead-flow.invalid"
    refs = _ids(" ".join(str(x) for x in msg.get_all("References", [])))
    in_reply_to = _ids(msg.get("In-Reply-To"))
    ids = _ids(msg.get("Message-ID"))
    # Headerless messages remain idempotent, but can never claim a sender identity.
    message_id = ids[0] if len(ids) == 1 else f"<missing-{digest}@lead-flow.invalid>"
    chunks = []
    for part in msg.walk():
        if part.get_content_type() == "text/plain" and part.get_content_disposition() != "attachment":
            try:
                chunks.append(part.get_content()[:20_000])
            except (LookupError, UnicodeError):
                pass
    body = "\n".join(chunks)[:20_000]
    classification = "needs_review"
    if msg.get_content_type() == "multipart/report" and msg.get_param("report-type") == "delivery-status":
        classification = "bounce"
    elif str(msg.get("Auto-Submitted", "no")).lower() != "no" or msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        classification = "auto_reply"
    fallback = datetime.fromisoformat(observed_at) if observed_at else datetime.now(timezone.utc)
    try:
        timestamp = parsedate_to_datetime(str(msg.get("Date", "")))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if timestamp > fallback:
            timestamp = fallback
    except (TypeError, ValueError, OverflowError):
        timestamp = fallback
    return {"message_id": message_id, "from_email": from_email,
            "in_reply_to": in_reply_to[0] if in_reply_to else "", "references": refs,
            "subject": str(msg.get("Subject", ""))[:250], "body": body,
            "received_at": timestamp.isoformat(), "classification": classification,
            "classification_quote": ""}


def poll_replies(config: MailConfig, on_reply, *, imap_factory=imaplib.IMAP4_SSL, deadline=None):
    """Callback must durably acknowledge CRM ingestion; never changes mailbox flags."""
    if not config.enabled:
        return {"status": "disabled", "processed": 0}
    state, client = MailState(config.state_path), None
    processed = 0
    try:
        with state.lock():
            password = config.password()
            client = imap_factory(config.imap_host, config.imap_port,
                                  ssl_context=ssl.create_default_context(), timeout=config.timeout)
            client.login(config.sender, password)
            code, _ = client.select(config.mailbox, readonly=True)
            if code != "OK":
                return {"status": "error", "error": "imap_select_failed", "processed": 0}
            _, validity_rows = client.response("UIDVALIDITY")
            validity = validity_rows[0].decode("ascii") if validity_rows and validity_rows[0] else ""
            if not validity.isdigit():
                return {"status": "error", "error": "imap_uidvalidity_missing", "processed": 0}
            cursor = state.cursor(config)
            changed = bool(cursor and cursor[0] != validity)
            lower = 1 if not cursor or changed else cursor[1] + 1
            code, rows = client.uid("search", None, "UID", f"{lower}:*")
            if code != "OK":
                return {"status": "error", "error": "imap_search_failed", "processed": 0}
            # RFC range '*' can return the previous highest UID; filter locally.
            uids = sorted({int(x) for x in (rows[0] or b"").split() if x.isdigit() and int(x) >= lower})
            if cursor is None:
                state.set_cursor(config, validity, max(uids, default=0))
                return {"status": "baselined", "processed": 0, "existing_skipped": len(uids)}
            if changed:
                state.set_cursor(config, validity, 0)
            for index, uid in enumerate(uids[:config.batch_size]):
                if deadline is not None and time.monotonic() >= deadline:
                    return {"status": "ok", "processed": processed, "uidvalidity_changed": changed,
                            "remaining": len(uids) - index, "time_budget_reached": True}
                code, data = client.uid("fetch", str(uid), f"(UID RFC822.SIZE BODY.PEEK[]<0.{config.max_message_bytes + 1}>)")
                if code != "OK":
                    return {"status": "error", "error": "imap_fetch_failed", "processed": processed}
                parts = [x for x in data if isinstance(x, tuple) and len(x) > 1 and isinstance(x[1], bytes)]
                if len(parts) != 1:
                    return {"status": "error", "error": "imap_message_missing", "processed": processed}
                metadata, raw = parts[0][0], parts[0][1]
                size_match = re.search(br"RFC822.SIZE\s+(\d+)", metadata)
                if len(raw) > config.max_message_bytes or (size_match and int(size_match[1]) > config.max_message_bytes):
                    # Stop with an actionable limitation; do not silently skip a possible reply.
                    return {"status": "error", "error": "imap_message_too_large", "uid": uid, "processed": processed}
                digest = hashlib.sha256(raw).hexdigest()
                payload = parse_reply(raw, digest=digest, observed_at=state.first_observed(config, digest))
                if not state.seen(config, digest):
                    on_reply(payload)  # Failure leaves this UID unacknowledged.
                    processed += 1
                state.acknowledge(config, validity, uid, digest, payload)
            return {"status": "ok", "processed": processed, "uidvalidity_changed": changed,
                    "remaining": max(0, len(uids) - config.batch_size)}
    except Exception:
        # Transport/provider bodies may contain addresses or credentials; never log them.
        return {"status": "error", "error": "imap_or_ingestion_failed", "processed": processed}
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass
