"""Evidence, outreach ledger and agent API. This module never sends email.

The sender is a separately authenticated worker. Transport acceptance is distinct
from delivery; an abandoned claim is uncertain and is never automatically retried.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from contextlib import contextmanager, closing
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


QUALIFICATIONS = Literal["unknown", "researching", "qualified", "contact_ready", "not_fit", "needs_review"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(StrictModel):
    source_url: HttpUrl
    quote: str = Field(min_length=15, max_length=600)
    observed_at: datetime
    published_at: datetime | None = None
    signal: Literal["explicit_request", "procurement", "manual_work", "hiring", "complaint", "company_profile", "counterevidence"]
    interpretation: str = Field(min_length=1, max_length=1200)


class Candidate(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    company: str = Field(min_length=1, max_length=180)
    company_domain: str = Field(min_length=3, max_length=253)
    segment: str = Field(default="", max_length=180)
    document_types: list[str] = Field(default_factory=list, max_length=20)
    use_case: str = Field(default="", max_length=1500)
    need_summary: str = Field(default="", max_length=2000)
    qualification_status: QUALIFICATIONS = "unknown"
    fit_score: int | None = Field(default=None, ge=0, le=100)
    intent_score: int | None = Field(default=None, ge=0, le=100)
    email: str = Field(default="", max_length=180)
    contact_name: str = Field(default="", max_length=160)
    contact_role: str = Field(default="", max_length=160)
    contact_source_url: HttpUrl | None = None
    evidence: list[Evidence] = Field(default_factory=list, max_length=20)
    limitations: str = Field(default="", max_length=3000)

    @field_validator("company_domain")
    @classmethod
    def domain_valid(cls, value: str) -> str:
        value = value.strip().lower().removeprefix("www.").rstrip(".")
        if ":" in value or "/" in value or "@" in value or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value) or "." not in value or ".." in value:
            raise ValueError("Укажите домен компании без пути и протокола")
        return value

    @field_validator("email")
    @classmethod
    def email_valid(cls, value: str) -> str:
        value = value.strip().lower()
        if value and not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", value):
            raise ValueError("Некорректный email")
        return value


class Draft(StrictModel):
    idempotency_key: str = Field(min_length=8, max_length=160)
    lead_id: int = Field(gt=0)
    subject: str = Field(min_length=1, max_length=250)
    body: str = Field(min_length=10, max_length=8000)
    purpose: Literal["sales", "discovery"] = "sales"
    contact_basis: Literal["unknown", "explicit_request", "consent", "existing_relationship", "owner_review"]
    basis_evidence: str = Field(min_length=10, max_length=2000)

    @field_validator("subject")
    @classmethod
    def subject_valid(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Недопустимый заголовок")
        return value


class Approval(StrictModel):
    action: Literal["approve", "cancel"]


class DraftRevision(StrictModel):
    idempotency_key: str = Field(min_length=8,max_length=160)
    outreach_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    expected_content_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    subject: str = Field(min_length=1,max_length=250)
    body: str = Field(min_length=10,max_length=8000)
    review_checks: dict[str,bool]

    @field_validator('subject')
    @classmethod
    def subject_valid(cls,value):
        return Draft.subject_valid(value)

    @field_validator('review_checks')
    @classmethod
    def approved_review(cls,value):
        from worker.contracts import LETTER_REVIEW_FLAGS
        if set(value)!={'approved',*LETTER_REVIEW_FLAGS} or not all(value.values()):
            raise ValueError('Independent letter review must approve every check')
        return value


class TransportResult(StrictModel):
    claim_token: str = Field(min_length=20, max_length=100)
    outcome: Literal["accepted", "failed_before_send", "rejected", "uncertain"]
    provider_message_id: str = Field(default="", max_length=500)
    receipt: str = Field(default="", max_length=2000)


class Reply(StrictModel):
    message_id: str = Field(min_length=3, max_length=500)
    from_email: str = Field(min_length=3, max_length=180)
    in_reply_to: str = Field(default="", max_length=500)
    references: list[str] = Field(default_factory=list, max_length=40)
    subject: str = Field(default="", max_length=250)
    body: str = Field(max_length=20000)
    received_at: datetime
    classification: Literal["interested", "refused", "human_reply", "auto_reply", "bounce", "needs_review"] = "needs_review"
    classification_quote: str = Field(default="", max_length=1000)

    @field_validator("message_id", "in_reply_to")
    @classmethod
    def header_valid(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Недопустимый Message-ID")
        return value.strip()

    @field_validator("from_email")
    @classmethod
    def email_valid(cls, value: str) -> str:
        return Candidate.email_valid(value)


class Heartbeat(StrictModel):
    run_id: str = Field(min_length=1, max_length=100)
    state: Literal["running", "completed", "failed", "paused"]
    summary: str = Field(max_length=2000)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def email_key(email: str) -> str:
    return "email:" + hashlib.sha256(email.strip().lower().encode()).hexdigest()


def contact_digest(row):
    return digest({key:row.get(key) or '' for key in ('email','contact_source_url','contact_name','contact_role')})


def qualification_allows_letter(row, purpose="sales"):
    return row["qualification_status"] == "qualified" or (
        purpose == "discovery" and row["qualification_status"] == "contact_ready")


def official_host(url, domain):
    host = (urlsplit(str(url)).hostname or "").lower().removeprefix("www.")
    return host == domain or host.endswith("." + domain)


def install(app, connect, sql, serialize, require_owner, use_postgres: bool) -> None:
    """Register additive tables and routes without importing application secrets."""
    with closing(connect()) as db:
        columns = {
            "company_domain": "TEXT", "segment": "TEXT NOT NULL DEFAULT ''",
            "document_types": "TEXT NOT NULL DEFAULT '[]'", "use_case": "TEXT NOT NULL DEFAULT ''",
            "need_summary": "TEXT NOT NULL DEFAULT ''", "qualification_status": "TEXT NOT NULL DEFAULT 'unknown'",
            "fit_score": "INTEGER", "intent_score": "INTEGER", "contact_source_url": "TEXT",
            "limitations": "TEXT NOT NULL DEFAULT ''", "archived_at": "TEXT", "archive_reason": "TEXT",
            "last_human_reply_at": "TEXT", "research_updated_at": "TEXT", "research_contact_hash": "TEXT",
        }
        existing = set() if use_postgres else {r["name"] for r in db.execute("PRAGMA table_info(leads)")}
        for name, definition in columns.items():
            if use_postgres or name not in existing:
                db.execute(f"ALTER TABLE leads ADD COLUMN {'IF NOT EXISTS ' if use_postgres else ''}{name} {definition}")
        db.execute("CREATE UNIQUE INDEX IF NOT EXISTS leads_company_domain_unique ON leads(company_domain)")
        identity = "BIGSERIAL PRIMARY KEY" if use_postgres else "INTEGER PRIMARY KEY AUTOINCREMENT"
        tables = [
            "CREATE TABLE IF NOT EXISTS acquisition_keys (key TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, lead_id BIGINT NOT NULL REFERENCES leads(id), original_created INTEGER)",
            f"CREATE TABLE IF NOT EXISTS lead_evidence (id {identity}, lead_id BIGINT NOT NULL REFERENCES leads(id), fingerprint TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(lead_id, fingerprint))",
            "CREATE TABLE IF NOT EXISTS lead_suppressions (identity TEXT PRIMARY KEY, reason TEXT NOT NULL, created_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS outreach (id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, payload_hash TEXT NOT NULL, lead_id BIGINT NOT NULL REFERENCES leads(id), recipient TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, contact_basis TEXT NOT NULL, basis_evidence TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, approved_at TEXT, message_id TEXT UNIQUE NOT NULL, claim_token TEXT, claimed_at TEXT, provider_message_id TEXT, receipt TEXT, accepted_at TEXT)",
            "CREATE TABLE IF NOT EXISTS lead_inbox (message_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, lead_id BIGINT REFERENCES leads(id), outreach_id TEXT REFERENCES outreach(id), from_email TEXT NOT NULL, subject TEXT NOT NULL, body TEXT NOT NULL, received_at TEXT NOT NULL, classification TEXT NOT NULL, classification_quote TEXT NOT NULL, match_state TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS acquisition_runs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL, summary TEXT NOT NULL, updated_at TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS outreach_text_revisions (key TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,outreach_id TEXT NOT NULL REFERENCES outreach(id),old_subject TEXT NOT NULL,old_body TEXT NOT NULL,new_hash TEXT NOT NULL,review_checks TEXT NOT NULL,created_at TEXT NOT NULL)",
        ]
        for statement in tables:
            db.execute(statement)
        # Older keys did not store the original outcome. Preserve null instead
        # of inventing whether their first request created or enriched a card.
        if use_postgres:
            db.execute('ALTER TABLE acquisition_keys ADD COLUMN IF NOT EXISTS original_created INTEGER')
        elif 'original_created' not in {row['name'] for row in db.execute('PRAGMA table_info(acquisition_keys)')}:
            db.execute('ALTER TABLE acquisition_keys ADD COLUMN original_created INTEGER')
        if use_postgres:
            db.execute("ALTER TABLE outreach ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'sales'")
        elif 'purpose' not in {row['name'] for row in db.execute('PRAGMA table_info(outreach)')}:
            db.execute("ALTER TABLE outreach ADD COLUMN purpose TEXT NOT NULL DEFAULT 'sales'")
        db.execute("CREATE INDEX IF NOT EXISTS outreach_state_idx ON outreach(state, created_at)")
        db.commit()

    @contextmanager
    def transaction():
        with closing(connect()) as db:
            # One ingestion/claim/reply transition at a time across web workers.
            # pg advisory transaction lock is release-on-commit/crash; SQLite uses its writer lock.
            if use_postgres:
                db.execute("SELECT pg_advisory_xact_lock(1294731021)")
            else:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    def agent_auth(request: Request):
        expected = os.getenv("CRM_AGENT_TOKEN", "")
        supplied = request.headers.get("Authorization", "")
        if len(expected) < 32:
            raise HTTPException(503, "Интеграция агента не настроена")
        if not hmac.compare_digest(supplied, "Bearer " + expected):
            raise HTTPException(401, "Нужен отдельный токен агента")

    agent = APIRouter(prefix="/api/agent", dependencies=[Depends(agent_auth)])
    owner = APIRouter(prefix="/api", dependencies=[Depends(require_owner)])

    def lead(db, lead_id):
        row = db.execute(sql("SELECT * FROM leads WHERE id = ?"), (lead_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Лид не найден")
        return dict(row)

    def event(db, lead_id, event_type, old=None, new=None):
        db.execute(sql("INSERT INTO lead_events(lead_id,event_type,from_status,to_status,occurred_at) VALUES(?,?,?,?,?)"), (lead_id, event_type, old, new, now()))

    def is_suppressed(db, domain, email):
        identities = ["domain:" + domain] if domain else []
        if email:
            identities.append(email_key(email))
        return any(db.execute(sql("SELECT identity FROM lead_suppressions WHERE identity=?"), (i,)).fetchone() for i in identities)

    def archive(db, row, reason, suppress=True):
        if row["archived_at"]:
            return
        timestamp = now()
        db.execute(sql("UPDATE leads SET archived_at=?,archive_reason=?,updated_at=? WHERE id=?"), (timestamp, reason, timestamp, row["id"]))
        db.execute(sql("UPDATE outreach SET state='cancelled',updated_at=? WHERE lead_id=? AND state IN ('draft','ready')"), (timestamp, row["id"]))
        if suppress:
            identities = ["domain:" + row["company_domain"]] if row.get("company_domain") else []
            if row.get("email"):
                identities.append(email_key(row["email"]))
            for identity in identities:
                db.execute(sql("INSERT INTO lead_suppressions(identity,reason,created_at) VALUES(?,?,?) ON CONFLICT(identity) DO NOTHING"), (identity, reason, timestamp))
        event(db, row["id"], "archived", row["status"], row["status"])

    def ingestion_result(db, row, *, created, replayed, original_created):
        """Current eligibility is separate from a durable ingestion outcome."""
        row = dict(row)
        suppressed = is_suppressed(db, row['company_domain'], row['email'])
        existing = db.execute(sql('SELECT id,state FROM outreach WHERE lead_id=? ORDER BY created_at LIMIT 1'), (row['id'],)).fetchone()
        # Shared recipient also prevents two company cards contacting one inbox.
        shared = bool(row['email'] and db.execute(sql('SELECT id FROM outreach WHERE LOWER(recipient)=? LIMIT 1'), (row['email'].lower(),)).fetchone())
        allowed = bool(not row['archived_at'] and not suppressed and not existing and not shared
            and row['status'] == 'new' and qualification_allows_letter(row, 'discovery') and row['email'] and row['contact_source_url'])
        return {'lead_id':row['id'], 'created':created, 'replayed':replayed,
            'original_created':original_created, 'qualification_status':row['qualification_status'],
            'research_updated_at':row['research_updated_at'],
            'archived':bool(row['archived_at']), 'suppressed':bool(suppressed),
            'initial_draft_allowed':allowed,
            'existing_initial_draft':{'id':existing['id'],'state':existing['state']} if existing else None}

    @agent.post("/leads")
    def ingest(payload: Candidate):
        data = payload.model_dump(mode="json")
        fingerprint = digest(data)
        if payload.qualification_status == "qualified":
            if not payload.evidence or not payload.need_summary or payload.fit_score is None or payload.intent_score is None:
                raise HTTPException(422, "Квалификация требует свидетельств, потребности и отдельных оценок")
            if not any(e.signal in {"explicit_request", "procurement", "manual_work", "complaint"} for e in payload.evidence):
                raise HTTPException(422, "Профиль компании или вакансия сами по себе не подтверждают потребность")
        if payload.qualification_status == "contact_ready":
            contact_url = str(payload.contact_source_url or "")
            if (not payload.email or not payload.use_case or (payload.fit_score or 0) < 50
                    or payload.intent_score is None or payload.intent_score > 25 or len(payload.evidence) < 2
                    or not official_host(contact_url, payload.company_domain)
                    or not any(str(e.source_url) == contact_url and payload.email.lower() in e.quote.lower() for e in payload.evidence)
                    or not any(official_host(e.source_url, payload.company_domain) and e.signal == 'company_profile' for e in payload.evidence)):
                raise HTTPException(422, "Для уточнения потребности нужны проверенные профиль, процесс и официальный контакт; интерес к покупке остаётся неизвестным")
        if payload.email and not payload.contact_source_url:
            raise HTTPException(422, "Для контакта нужен источник")
        for item in payload.evidence:
            if item.observed_at.tzinfo is None or item.observed_at > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise HTTPException(422, "Некорректная дата проверки источника")
        observed = max((item.observed_at.astimezone(timezone.utc) for item in payload.evidence), default=None)
        observed_at = observed.isoformat() if observed else None
        with transaction() as db:
            previous = db.execute(sql("SELECT * FROM acquisition_keys WHERE key=?"), (payload.idempotency_key,)).fetchone()
            if previous:
                if previous["payload_hash"] != fingerprint:
                    raise HTTPException(409, "Ключ уже использован с другим содержимым")
                stored_created = dict(previous).get('original_created')
                return ingestion_result(db,lead(db,previous['lead_id']),created=False,replayed=True,
                    original_created=bool(stored_created) if stored_created is not None else None)
            if is_suppressed(db, payload.company_domain, payload.email):
                return {"lead_id": None, "created": False, "original_created":False,
                    "suppressed": True, "initial_draft_allowed":False}
            row = db.execute(sql("SELECT * FROM leads WHERE company_domain=?"), (payload.company_domain,)).fetchone()
            if row:
                lead_id = row["id"]
                if row["archived_at"]:
                    return ingestion_result(db,row,created=False,replayed=False,original_created=False)
                # New verified research can change machine-owned qualification,
                # but never overwrites an owner's contact, notes or sales stage.
                prior_time = datetime.fromisoformat(row['research_updated_at']) if row['research_updated_at'] else None
                if observed and (prior_time is None or observed > prior_time):
                    fields = {key:data[key] for key in ('segment','use_case','need_summary','qualification_status','fit_score','intent_score','limitations')}
                    fields.update(document_types=json.dumps(data['document_types'],ensure_ascii=False),
                        research_updated_at=observed_at,updated_at=now())
                    # Fill a later discovered public contact only while these
                    # fields still equal the last machine-owned values. Human
                    # edits (including clearing an address) break this equality.
                    if payload.email and row['research_contact_hash'] == contact_digest(dict(row)):
                        fields.update({key:data[key] for key in ('email','contact_source_url','contact_name','contact_role')})
                        fields['research_contact_hash'] = contact_digest(data)
                    db.execute(sql('UPDATE leads SET '+','.join(key+'=?' for key in fields)+' WHERE id=?'), [*fields.values(),lead_id])
                    if payload.qualification_status != 'qualified':
                        db.execute(sql("UPDATE outreach SET state='draft',approved_at=NULL,updated_at=? WHERE lead_id=? AND state='ready' AND (purpose<>'discovery' OR ?<>'contact_ready')"), (now(),lead_id,payload.qualification_status))
                    event(db,lead_id,'research_refreshed',row['status'],row['status'])
            else:
                timestamp = now()
                fields = {k: data[k] for k in ("company", "company_domain", "segment", "use_case", "need_summary", "qualification_status", "fit_score", "intent_score", "email", "contact_name", "contact_role", "contact_source_url", "limitations")}
                fields.update(document_types=json.dumps(data["document_types"], ensure_ascii=False), source="OSINT OCR", created_at=timestamp, updated_at=timestamp,research_updated_at=observed_at)
                fields['research_contact_hash'] = contact_digest(data)
                query = f"INSERT INTO leads({','.join(fields)}) VALUES({','.join('?' for _ in fields)})" + (" RETURNING id" if use_postgres else "")
                cursor = db.execute(sql(query), list(fields.values()))
                lead_id = cursor.fetchone()["id"] if use_postgres else cursor.lastrowid
                event(db, lead_id, "created", None, "new")
            for item in data["evidence"]:
                db.execute(sql("INSERT INTO lead_evidence(lead_id,fingerprint,data) VALUES(?,?,?) ON CONFLICT(lead_id,fingerprint) DO NOTHING"), (lead_id, digest(item), json.dumps(item, ensure_ascii=False)))
            db.execute(sql("INSERT INTO acquisition_keys(key,payload_hash,lead_id,original_created) VALUES(?,?,?,?)"), (payload.idempotency_key, fingerprint, lead_id,int(row is None)))
            return ingestion_result(db,lead(db,lead_id),created=row is None,replayed=False,original_created=row is None)

    @owner.get("/leads/{lead_id}/research")
    def detail(lead_id: int):
        with closing(connect()) as db:
            row = lead(db, lead_id)
            evidence = [json.loads(r["data"]) for r in db.execute(sql("SELECT data FROM lead_evidence WHERE lead_id=? ORDER BY id"), (lead_id,))]
            messages = [dict(r) for r in db.execute(sql("SELECT id,recipient,subject,body,state,created_at,accepted_at,receipt,contact_basis,basis_evidence FROM outreach WHERE lead_id=? ORDER BY created_at"), (lead_id,))]
            replies = [dict(r) for r in db.execute(sql("SELECT message_id,from_email,subject,body,received_at,classification,match_state FROM lead_inbox WHERE lead_id=? ORDER BY received_at"), (lead_id,))]
            return {"lead": serialize(row), "evidence": evidence, "outreach": messages, "replies": replies}

    @agent.post("/drafts")
    def make_draft(payload: Draft):
        fingerprint = digest(payload.model_dump())
        with transaction() as db:
            old = db.execute(sql("SELECT id,payload_hash,state FROM outreach WHERE idempotency_key=?"), (payload.idempotency_key,)).fetchone()
            if old:
                if old["payload_hash"] != fingerprint:
                    raise HTTPException(409, "Ключ письма уже использован")
                return {"id": old["id"], "state": old["state"], "replayed": True}
            row = lead(db, payload.lead_id)
            if row["archived_at"] or is_suppressed(db, row["company_domain"], row["email"]):
                raise HTTPException(409, "Контакт исключён из работы")
            if not row["email"] or not row["contact_source_url"]:
                raise HTTPException(422, "Нет подтверждённого контакта")
            if not qualification_allows_letter(row, payload.purpose):
                raise HTTPException(409, "Для этого письма недостаточно проверенных сведений о компании")
            if row['status'] != 'new':
                raise HTTPException(409, 'Первичное письмо не создаётся для уже ведущейся или закрытой сделки')
            # This first iteration sends only a single initial message per company.
            if db.execute(sql("SELECT id FROM outreach WHERE (lead_id=? OR LOWER(recipient)=?)"), (row["id"],row["email"].lower())).fetchone():
                raise HTTPException(409, "Первичное письмо этой компании уже существует")
            key = secrets.token_hex(16)
            stamp = now()
            message_id = f"<leadflow-{key}@{os.getenv('CRM_MESSAGE_ID_DOMAIN', 'lead-flow.invalid')}>"
            # Owner can commission automatic sending for narrowly qualified contacts.
            # A public address alone or 'owner_review' never unlocks this policy.
            auto_ready = (os.getenv("CRM_AUTO_APPROVE") == "1"
                          and os.getenv("CRM_OUTREACH_ENABLED") == "1"
                          and payload.contact_basis in {"explicit_request", "consent"}
                          and (row["fit_score"] or 0) >= int(os.getenv("CRM_MIN_FIT_SCORE", "70"))
                          and (row["intent_score"] or 0) >= int(os.getenv("CRM_MIN_INTENT_SCORE", "60")))
            discovery_ready = (os.getenv("CRM_AUTO_APPROVE") == "1"
                and os.getenv("CRM_DISCOVERY_OUTREACH_ENABLED") == "1"
                and payload.purpose == "discovery" and payload.contact_basis == "owner_review"
                and qualification_allows_letter(row, 'discovery') and (row["fit_score"] or 0) >= 50)
            auto_ready = auto_ready or discovery_ready
            state = "ready" if auto_ready else "draft"
            db.execute(sql("INSERT INTO outreach(id,idempotency_key,payload_hash,lead_id,recipient,subject,body,contact_basis,basis_evidence,state,created_at,updated_at,message_id,approved_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"), (key,payload.idempotency_key,fingerprint,row["id"],row["email"],payload.subject,payload.body,payload.contact_basis,payload.basis_evidence,state,stamp,stamp,message_id,stamp if auto_ready else None))
            db.execute(sql("UPDATE outreach SET purpose=? WHERE id=?"), (payload.purpose,key))
            event(db, row["id"], "draft_created")
            if auto_ready:
                event(db,row["id"],"campaign_policy_approved")
            return {"id": key, "state": state, "replayed": False}

    @agent.post('/drafts/revise')
    def revise_draft(payload: DraftRevision):
        fingerprint=digest(payload.model_dump())
        with transaction() as db:
            previous=db.execute(sql('SELECT payload_hash,outreach_id FROM outreach_text_revisions WHERE key=?'),(payload.idempotency_key,)).fetchone()
            if previous:
                if previous['payload_hash']!=fingerprint:raise HTTPException(409,'Revision key already used')
                current=db.execute(sql('SELECT id,state,message_id FROM outreach WHERE id=?'),(previous['outreach_id'],)).fetchone()
                return {'id':current['id'],'state':current['state'],'message_id':current['message_id'],'replayed':True,'revised':True}
            row=db.execute(sql('SELECT * FROM outreach WHERE id=?'),(payload.outreach_id,)).fetchone()
            if not row:raise HTTPException(404,'Letter not found')
            if row['state'] not in ('draft','ready') or row['claimed_at'] is not None:
                raise HTTPException(409,'Only never-claimed drafts may be revised')
            if digest({'subject':row['subject'],'body':row['body']})!=payload.expected_content_hash:
                raise HTTPException(409,'Letter changed since review')
            parent=lead(db,row['lead_id'])
            if (parent['archived_at'] or parent['status']!='new' or is_suppressed(db,parent['company_domain'],row['recipient'])
                    or not qualification_allows_letter(parent,row['purpose']) or parent['email'].lower()!=row['recipient'].lower()):
                raise HTTPException(409,'Contact no longer eligible')
            stamp=now();new_hash=digest({'subject':payload.subject,'body':payload.body})
            db.execute(sql('INSERT INTO outreach_text_revisions VALUES(?,?,?,?,?,?,?,?)'),
                (payload.idempotency_key,fingerprint,row['id'],row['subject'],row['body'],new_hash,json.dumps(payload.review_checks),stamp))
            db.execute(sql('UPDATE outreach SET subject=?,body=?,updated_at=? WHERE id=?'),(payload.subject,payload.body,stamp,row['id']))
            event(db,row['lead_id'],'draft_text_revised')
            return {'id':row['id'],'state':row['state'],'message_id':row['message_id'],'content_hash':new_hash,'replayed':False,'revised':True}

    @owner.post("/outreach/{outreach_id}/review")
    def review(outreach_id: str, payload: Approval):
        with transaction() as db:
            row = db.execute(sql("SELECT * FROM outreach WHERE id=?"), (outreach_id,)).fetchone()
            if not row:
                raise HTTPException(404, "Черновик не найден")
            if row["state"] not in {"draft", "ready"}:
                raise HTTPException(409, "Письмо уже обрабатывается или закрыто")
            parent = lead(db, row["lead_id"])
            if payload.action == "approve" and (parent["archived_at"] or is_suppressed(db, parent["company_domain"], row["recipient"])):
                raise HTTPException(409, "Контакт исключён")
            if payload.action == 'approve' and (parent['status'] != 'new' or not qualification_allows_letter(parent, row['purpose'])
                    or not parent['email'] or parent['email'].lower() != row['recipient'].lower() or not parent['contact_source_url']):
                raise HTTPException(409, 'Нужна актуальная квалификация и подтверждённый контакт письма')
            state = "ready" if payload.action == "approve" else "cancelled"
            db.execute(sql("UPDATE outreach SET state=?,approved_at=?,updated_at=? WHERE id=?"), (state, now() if state == "ready" else None, now(), outreach_id))
            event(db, row["lead_id"], "draft_" + payload.action)
            return {"id": outreach_id, "state": state}

    def expire_claims(db):
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
        db.execute(sql("UPDATE outreach SET state='uncertain',receipt='Worker acknowledgement missing; no automatic resend',updated_at=? WHERE state='inflight' AND claimed_at < ?"), (now(), cutoff))

    @agent.post("/outreach/claim")
    def claim():
        if os.getenv("CRM_OUTREACH_ENABLED") != "1" or not os.getenv("CRM_SENDER_EMAIL"):
            raise HTTPException(409, "Отправка не подключена: письма остаются черновиками")
        with transaction() as db:
            expire_claims(db)
            limit = max(1, min(100, int(os.getenv("CRM_DAILY_SEND_LIMIT", "10"))))
            moment = datetime.now(timezone.utc)
            local = moment.astimezone(ZoneInfo(os.getenv('CRM_OUTREACH_TIMEZONE', 'Asia/Yekaterinburg')))
            if not int(os.getenv('CRM_SEND_START_HOUR', '0')) <= local.hour < int(os.getenv('CRM_SEND_END_HOUR', '24')):
                return {"message": None, "reason": "outside_window"}
            today = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
            count = db.execute(sql("SELECT COUNT(*) AS n FROM outreach WHERE claimed_at>=?"), (today,)).fetchone()["n"]
            if count >= limit:
                return {"message": None, "reason": "daily_limit"}
            spacing = max(0, min(3600, int(os.getenv('CRM_SEND_INTERVAL_SECONDS', '0'))))
            latest = db.execute("SELECT MAX(claimed_at) AS stamp FROM outreach").fetchone()['stamp']
            if latest and moment < datetime.fromisoformat(latest) + timedelta(seconds=spacing):
                return {"message": None, "reason": "paced"}
            rows = db.execute("SELECT o.* FROM outreach o JOIN leads l ON l.id=o.lead_id WHERE o.state='ready' AND l.archived_at IS NULL ORDER BY o.created_at").fetchall()
            for item in rows:
                parent = lead(db, item["lead_id"])
                if is_suppressed(db, parent["company_domain"], item["recipient"]):
                    db.execute(sql("UPDATE outreach SET state='cancelled',updated_at=? WHERE id=?"), (now(), item["id"]))
                    continue
                if (parent['status'] != 'new' or not qualification_allows_letter(parent, item['purpose']) or not parent['email']
                        or parent['email'].lower() != item['recipient'].lower() or not parent['contact_source_url']):
                    db.execute(sql("UPDATE outreach SET state='draft',approved_at=NULL,updated_at=? WHERE id=?"),(now(),item['id']))
                    continue
                token = secrets.token_urlsafe(32)
                db.execute(sql("UPDATE outreach SET state='inflight',claim_token=?,claimed_at=?,updated_at=? WHERE id=? AND state='ready'"), (token,now(),now(),item["id"]))
                return {"message": {"id": item["id"], "lead_id": item["lead_id"], "to": item["recipient"], "from": os.getenv("CRM_SENDER_EMAIL"), "subject": item["subject"], "body": item["body"], "message_id": item["message_id"], "claim_token": token}}
            return {"message": None, "reason": "empty"}

    @agent.post("/outreach/{outreach_id}/result")
    def acknowledge(outreach_id: str, payload: TransportResult):
        if payload.outcome == "accepted" and (not payload.provider_message_id.strip() or not payload.receipt.strip()):
            raise HTTPException(422, "Нужны идентификатор и подтверждение почтового транспорта")
        with transaction() as db:
            row = db.execute(sql("SELECT * FROM outreach WHERE id=?"), (outreach_id,)).fetchone()
            if not row or not row["claim_token"] or not hmac.compare_digest(row["claim_token"],payload.claim_token):
                raise HTTPException(409, "Неизвестная попытка отправки")
            if row["state"] != "inflight":
                if row["state"] == payload.outcome and row["provider_message_id"] == payload.provider_message_id and row["receipt"] == payload.receipt:
                    return {"state": row["state"], "replayed": True}
                raise HTTPException(409, "Попытка закрыта; требуется сверка, повторная отправка запрещена")
            stamp = now()
            db.execute(sql("UPDATE outreach SET state=?,provider_message_id=?,receipt=?,accepted_at=?,updated_at=? WHERE id=?"), (payload.outcome,payload.provider_message_id,payload.receipt,stamp if payload.outcome=="accepted" else None,stamp,outreach_id))
            if payload.outcome == "accepted":
                parent = lead(db,row["lead_id"])
                if not parent["archived_at"] and parent["status"] == "new":
                    db.execute(sql("UPDATE leads SET status='proposal_sent',proposal_sent_at=?,updated_at=? WHERE id=?"), (stamp,stamp,parent["id"]))
                    event(db,parent["id"],"transport_accepted","new","proposal_sent")
            return {"state": payload.outcome, "replayed": False, "delivery_confirmed": False}

    @agent.post("/replies")
    def receive(payload: Reply):
        data = payload.model_dump(mode="json")
        fingerprint = digest(data)
        if payload.received_at.tzinfo is None or payload.received_at > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise HTTPException(422, "Некорректная дата письма")
        if payload.classification in {"interested", "refused"} and (len(payload.classification_quote.strip()) < 3 or payload.classification_quote not in payload.body):
            raise HTTPException(422, "Классификация требует дословного фрагмента ответа")
        with transaction() as db:
            old = db.execute(sql("SELECT payload_hash,lead_id,match_state FROM lead_inbox WHERE message_id=?"), (payload.message_id,)).fetchone()
            if old:
                if old["payload_hash"] != fingerprint:
                    raise HTTPException(409, "Message-ID уже зарегистрирован с другим содержимым")
                return {"lead_id": old["lead_id"], "match_state": old["match_state"], "replayed": True}
            refs = list(dict.fromkeys([payload.in_reply_to, *reversed(payload.references)]))
            matches = []
            for ref in refs:
                if not ref:
                    continue
                row = db.execute(sql("SELECT * FROM outreach WHERE message_id=? AND recipient=? AND state IN ('accepted','uncertain','inflight')"), (ref,payload.from_email)).fetchone()
                if row:
                    matches.append(dict(row))
            matched = matches[0] if matches and len({r["lead_id"] for r in matches}) == 1 else None
            state = "matched" if matched else "needs_review"
            db.execute(sql("INSERT INTO lead_inbox(message_id,payload_hash,lead_id,outreach_id,from_email,subject,body,received_at,classification,classification_quote,match_state) VALUES(?,?,?,?,?,?,?,?,?,?,?)"), (payload.message_id,fingerprint,matched["lead_id"] if matched else None,matched["id"] if matched else None,payload.from_email,payload.subject,payload.body,payload.received_at.astimezone(timezone.utc).isoformat(),payload.classification,payload.classification_quote,state))
            if matched:
                parent = lead(db,matched["lead_id"])
                if payload.classification in {"interested","refused","human_reply"}:
                    db.execute(sql("UPDATE leads SET last_human_reply_at=?,updated_at=? WHERE id=?"), (now(),now(),parent["id"]))
                if payload.classification == "refused":
                    archive(db,parent,"refused",True)
                elif payload.classification == "interested" and not parent["archived_at"] and parent["status"] in {"new","proposal_sent","follow_up"}:
                    db.execute(sql("UPDATE leads SET status='interested',updated_at=? WHERE id=?"), (now(),parent["id"]))
                    event(db,parent["id"],"status_changed",parent["status"],"interested")
                event(db,parent["id"],"reply_" + payload.classification)
            return {"lead_id": matched["lead_id"] if matched else None, "match_state": state, "replayed": False}

    @agent.post("/heartbeat")
    def heartbeat(payload: Heartbeat):
        with transaction() as db:
            db.execute(sql("INSERT INTO acquisition_runs(run_id,state,summary,updated_at) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET state=excluded.state,summary=excluded.summary,updated_at=excluded.updated_at"), (payload.run_id,payload.state,payload.summary,now()))
        return {"recorded": True}

    @agent.post("/maintenance")
    def maintenance():
        days = max(1,min(365,int(os.getenv("CRM_NO_REPLY_DAYS","14"))))
        with transaction() as db:
            expire_claims(db)
            archived = 0
            if os.getenv("CRM_NO_REPLY_ARCHIVE_ENABLED") == "1":
                cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat(timespec="seconds")
                rows=db.execute(sql("SELECT l.* FROM leads l WHERE l.archived_at IS NULL AND l.status IN ('proposal_sent','follow_up') AND l.last_human_reply_at IS NULL AND EXISTS(SELECT 1 FROM outreach o WHERE o.lead_id=l.id AND o.state='accepted' AND o.accepted_at < ?) AND NOT EXISTS(SELECT 1 FROM outreach o WHERE o.lead_id=l.id AND o.state IN ('inflight','uncertain','ready','draft')) AND NOT EXISTS(SELECT 1 FROM lead_inbox i WHERE (i.lead_id=l.id OR (i.from_email=LOWER(l.email) AND i.match_state='needs_review')) AND i.classification IN ('needs_review','human_reply','interested','refused'))"),(cutoff,)).fetchall()
                for row in rows:
                    archive(db,dict(row),"no_reply",False)
                    archived+=1
        return {"archived":archived,"no_reply_days":days,"archive_enabled":os.getenv("CRM_NO_REPLY_ARCHIVE_ENABLED")=="1"}

    @owner.get("/automation")
    def automation():
        with closing(connect()) as db:
            runs=[dict(r) for r in db.execute("SELECT * FROM acquisition_runs ORDER BY updated_at DESC LIMIT 10")]
            counts={r["state"]:r["n"] for r in db.execute("SELECT state,COUNT(*) AS n FROM outreach GROUP BY state")}
            review=db.execute("SELECT COUNT(*) AS n FROM lead_inbox WHERE match_state='needs_review'").fetchone()["n"]
            archived=db.execute("SELECT COUNT(*) AS n FROM leads WHERE archived_at IS NOT NULL").fetchone()["n"]
        return {"agent_configured":len(os.getenv("CRM_AGENT_TOKEN",""))>=32,"outreach_enabled":os.getenv("CRM_OUTREACH_ENABLED")=="1","sender_configured":bool(os.getenv("CRM_SENDER_EMAIL")),"worker_seen":bool(runs),"schedule_connected":False,"schedule_note":"Наличие запусков не доказывает подключение расписания; расписание ведёт серверный worker.","runs":runs,"outreach":counts,"unmatched_replies":review,"archived_leads":archived,"auto_approve_enabled":os.getenv("CRM_AUTO_APPROVE")=="1","no_reply_archive_enabled":os.getenv("CRM_NO_REPLY_ARCHIVE_ENABLED")=="1","no_reply_days":max(1,min(365,int(os.getenv("CRM_NO_REPLY_DAYS","14"))))}

    @owner.post("/leads/{lead_id}/archive")
    def owner_archive(lead_id:int):
        with transaction() as db:
            archive(db,lead(db,lead_id),"owner_archived",True)
        return {"archived":True}

    # Keep this callable available to existing DELETE semantics without a second transaction.
    app.state.acquisition_archive=owner_archive
    app.include_router(agent)
    app.include_router(owner)
