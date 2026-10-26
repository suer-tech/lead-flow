"""Isolated PostgreSQL acceptance; run as OS postgres, never against a supplied DB.

Creates one uniquely named disposable database, seeds the legacy schema and
synthetic manual rows, then exercises the actual API via TestClient. No SMTP.
The database/report remain for review; no production database is modified.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import sys
import tempfile
import traceback

import psycopg
from psycopg import sql as pgsql
from psycopg.rows import dict_row


def main():
    if pwd.getpwuid(os.geteuid()).pw_name != "postgres":
        raise RuntimeError("Run with peer-authenticated OS postgres only")
    root = Path(__file__).resolve().parents[1]
    if root != Path("/opt/lead-flow"):
        raise RuntimeError("Expected isolated acceptance checkout /opt/lead-flow")
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", default=str(root))
    args = parser.parse_args()
    source_root = Path(args.source_dir).resolve()
    if source_root != root and not source_root.is_relative_to(root / "acceptance-probe"):
        raise RuntimeError("Only the installed checkout or acceptance fixture sources are allowed")
    started = datetime.now(timezone.utc)
    db_name = "lead_flow_acceptance_" + started.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(3)
    report_dir = Path(tempfile.mkdtemp(prefix="lead-flow-acceptance-"))
    os.chmod(report_dir, 0o700)
    report = {"database": db_name, "report_directory": str(report_dir), "started_at": started.isoformat(),
              "checks": [], "smtp_calls": 0, "real_email_sent": False,
              "test_scope": "actual PostgreSQL; synthetic transport receipts; FastAPI TestClient",
              "source_directory": str(source_root),
              "source_hashes": {name: hashlib.sha256((source_root/name).read_bytes()).hexdigest()
                                for name in ("app.py", "acquisition.py")}}
    admin_dsn = "dbname=postgres user=postgres host=/var/run/postgresql"
    dsn = "dbname=" + db_name + " user=postgres host=/var/run/postgresql"
    def check(name, condition):
        report["checks"].append({"name": name, "passed": bool(condition)})
        if not condition:
            raise AssertionError(name)
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            report["postgres_version"] = admin.execute("SELECT version()").fetchone()[0]
            before = [row[0] for row in admin.execute("SELECT datname FROM pg_database ORDER BY datname")]
            admin.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(db_name)))
        report["database_created"] = True
        with psycopg.connect(dsn, row_factory=dict_row) as db:
            db.execute("""CREATE TABLE leads (
                id BIGSERIAL PRIMARY KEY,company TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'new',
                source TEXT NOT NULL DEFAULT 'hh.ru',source_url TEXT,vacancy TEXT NOT NULL DEFAULT '',
                contact_name TEXT NOT NULL DEFAULT '',contact_role TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',email TEXT NOT NULL DEFAULT '',offer TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',next_action TEXT NOT NULL DEFAULT '',next_action_at TEXT,
                budget BIGINT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                CONSTRAINT leads_status_check CHECK (status IN ('new','proposal_sent','interested',
                   'diagnostics','proposal','negotiations','won','lost')))
            """)
            db.execute("""INSERT INTO leads(id,company,status,source,source_url,contact_name,email,notes,
                           next_action,budget,created_at,updated_at) VALUES
                          (101,'Synthetic manual company','negotiations','manual','https://legacy.invalid/one',
                           'Manual contact','manual@legacy.invalid','Ручные заметки владельца; сохранить',
                           'Согласовать условия',150000,'2026-01-01T10:00:00+00:00','2026-01-03T10:00:00+00:00'),
                          (202,'Synthetic new company','new','manual','https://legacy.invalid/two',
                           'Second contact','second@legacy.invalid','Second manual note',
                           'Call later',NULL,'2026-01-04T10:00:00+00:00','2026-01-04T10:00:00+00:00')""")
            db.execute("SELECT setval(pg_get_serial_sequence('leads','id'),202,true)")
            db.execute("CREATE TABLE acquisition_keys(key TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,lead_id BIGINT NOT NULL REFERENCES leads(id))")
            db.execute("INSERT INTO acquisition_keys VALUES('legacy-unclassified-key','historical-hash-with-unknown-origin',101)")
            legacy = [dict(row) for row in db.execute("SELECT * FROM leads ORDER BY id")]
        os.environ.update(DATABASE_URL=dsn, POSTGRES_URL="", CRM_PASSWORD=secrets.token_urlsafe(24),
            CRM_SECRET_KEY=secrets.token_urlsafe(32), CRM_AGENT_TOKEN=secrets.token_urlsafe(48),
            CRM_SEED_DEMO="0", CRM_OUTREACH_ENABLED="1", CRM_AUTO_APPROVE="0", CRM_NO_REPLY_ARCHIVE_ENABLED="0",
            CRM_DAILY_SEND_LIMIT="10", CRM_SENDER_EMAIL="sender@acceptance.invalid",
            CRM_MESSAGE_ID_DOMAIN="acceptance.invalid", VERCEL="1")
        # The installed research bundle intentionally has no frontend assets.
        # Run byte-identical API sources in a private fixture directory; no
        # application file on the research deployment is changed for this test.
        fixture_root = report_dir / "application"
        fixture_root.mkdir(mode=0o700)
        (fixture_root / "static").mkdir(mode=0o700)
        for name in ("app.py", "acquisition.py"):
            shutil.copyfile(source_root / name, fixture_root / name)
        report["static_ui_tested"] = False
        sys.path.insert(0, str(fixture_root))
        module = importlib.import_module("app")
        module = importlib.reload(module)  # Repeat the real startup/migrations.
        check("postgres_backend_selected", module.USE_POSTGRES)
        with closing(module.connect()) as db:
            actual = [dict(row) for row in db.execute("SELECT * FROM leads ORDER BY id")]
            check("legacy_rows_notes_and_stages_unchanged", len(actual) == 2 and all(
                all(after[key] == value for key, value in old.items()) for old, after in zip(legacy, actual)))
            check("additive_migration_columns", all(name in actual[0] for name in
                ("proposal_sent_at", "qualification_status", "company_domain", "archived_at", "research_updated_at", "research_contact_hash")))
            check("legacy_nullable_origin_migration_preserved", db.execute("SELECT original_created FROM acquisition_keys WHERE key='legacy-unclassified-key'").fetchone()["original_created"] is None
                  and actual[0]["research_updated_at"] is None and actual[0]["research_contact_hash"] is None)
            check("events_not_duplicated_on_second_start", db.execute("SELECT COUNT(*) AS n FROM lead_events").fetchone()["n"] == 4)
        from fastapi.testclient import TestClient
        with TestClient(module.app, base_url="https://acceptance.invalid") as client:
            token = {"Authorization": "Bearer " + os.environ["CRM_AGENT_TOKEN"]}
            def post(path, data=None, *, agent=True, expected=200):
                response = client.post(path, json=data, headers=token if agent else {})
                if response.status_code != expected:
                    raise AssertionError(f"{path}: HTTP {response.status_code}, expected {expected}; {response.text[:500]}")
                return response.json() if response.content else None
            post("/api/auth/login", {"password": os.environ["CRM_PASSWORD"]}, agent=False)
            candidate = {"idempotency_key": "acceptance-candidate-one", "company": "Synthetic OCR buyer",
                "company_domain": "buyer.acceptance.invalid", "email": "office@buyer.acceptance.invalid",
                "contact_source_url": "https://buyer.acceptance.invalid/contacts", "need_summary": "Synthetic manual court workflow",
                "qualification_status": "qualified", "fit_score": 85, "intent_score": 75,
                "evidence": [{"source_url": "https://buyer.acceptance.invalid/request",
                    "quote": "Our team manually reviews court rulings and needs assistance",
                    "observed_at": datetime.now(timezone.utc).isoformat(), "signal": "explicit_request",
                    "interpretation": "Synthetic isolated acceptance fixture"}]}
            post("/api/agent/leads", candidate, agent=False, expected=401)
            lead = post("/api/agent/leads", candidate)
            check("agent_ingest_idempotent", post("/api/agent/leads", candidate)["replayed"])
            lead_id = lead["lead_id"]
            patch = client.patch(f"/api/leads/{lead_id}", json={"notes": "Owner added note", "status": "diagnostics"})
            check("manual_update_supported", patch.status_code == 200)
            enrich = post("/api/agent/leads", dict(candidate, idempotency_key="acceptance-candidate-enriched"))
            rows = client.get("/api/leads").json()
            modified = next(row for row in rows if row["id"] == lead_id)
            check("rediscovery_preserves_manual_state", enrich["lead_id"] == lead_id
                  and modified["notes"] == "Owner added note" and modified["status"] == "diagnostics")
            client.patch(f"/api/leads/{lead_id}", json={"status": "new"})
            draft = post("/api/agent/drafts", {"idempotency_key": "acceptance-draft-one", "lead_id": lead_id,
                "subject": "Synthetic pilot question", "body": "This is an isolated API test. No email is sent.",
                "contact_basis": "owner_review", "basis_evidence": "Isolated acceptance approval; fictitious address"})
            check("unapproved_draft_not_claimed", draft["state"] == "draft" and post("/api/agent/outreach/claim")["message"] is None)
            post(f"/api/outreach/{draft['id']}/review", {"action": "approve"}, agent=False)
            with ThreadPoolExecutor(max_workers=4) as pool:
                claims = list(pool.map(lambda _: post("/api/agent/outreach/claim"), range(4)))
            messages = [item["message"] for item in claims if item["message"] is not None]
            check("postgres_concurrent_claim_exactly_once", len(messages) == 1)
            message = messages[0]
            path = f"/api/agent/outreach/{message['id']}/result"
            post(path, {"claim_token": message["claim_token"], "outcome": "accepted"}, expected=422)
            outcome = {"claim_token": message["claim_token"], "outcome": "accepted",
                "provider_message_id": message["message_id"], "receipt": "ISOLATED TEST receipt; SMTP never called"}
            accepted = post(path, outcome)
            check("receipt_is_idempotent_and_not_delivery", not accepted["delivery_confirmed"] and post(path, outcome)["replayed"])
            reply = {"message_id": "<reply-one@acceptance.invalid>", "from_email": message["to"],
                "in_reply_to": message["message_id"], "body": "Please tell us about your pilot",
                "received_at": datetime.now(timezone.utc).isoformat(), "classification": "interested",
                "classification_quote": "Please tell us about your pilot"}
            check("reply_matching_and_dedup", post("/api/agent/replies", reply)["match_state"] == "matched"
                  and post("/api/agent/replies", reply)["replayed"])
            interested = next(row for row in client.get("/api/leads").json() if row["id"] == lead_id)
            check("interested_reply_advances_stage", interested["status"] == "interested")
            refused = dict(reply, message_id="<reply-refused@acceptance.invalid>", body="Please do not contact us again",
                classification="refused", classification_quote="Please do not contact us again")
            post("/api/agent/replies", refused)
            check("refusal_archives_and_suppresses", lead_id not in [row["id"] for row in client.get("/api/leads").json()]
                and post("/api/agent/leads", dict(candidate, idempotency_key="acceptance-rescraped-after-refusal"))["suppressed"])
            detail = client.get(f"/api/leads/{lead_id}/research").json()
            check("archived_evidence_and_outreach_preserved", bool(detail["evidence"]) and len(detail["outreach"]) == 1)
            replay_after_refusal = post("/api/agent/leads", candidate)
            check("replayed_ingestion_reports_current_suppression", replay_after_refusal["replayed"]
                  and replay_after_refusal["suppressed"] and replay_after_refusal["archived"]
                  and not replay_after_refusal["initial_draft_allowed"] and replay_after_refusal["original_created"])

            def prospect(key, domain, *, age_days=0, **updates):
                value = json.loads(json.dumps(candidate))
                value.update(idempotency_key=key, company_domain=domain,
                    email="office@" + domain, contact_source_url="https://" + domain + "/contacts")
                value["evidence"][0]["source_url"] = "https://" + domain + "/request"
                value["evidence"][0]["observed_at"] = (datetime.now(timezone.utc)-timedelta(days=age_days)).isoformat()
                value.update(updates)
                return value

            def find_lead(ident):
                return next(row for row in client.get("/api/leads").json() if row["id"] == ident)

            def draft_for(ident, key):
                return {"idempotency_key": key, "lead_id": ident, "subject": "Synthetic second acceptance",
                    "body": "Synthetic API record only. No SMTP is invoked.", "contact_basis": "owner_review",
                    "basis_evidence": "Test fixture only; reserved .invalid destination"}

            first_review = prospect("needs-review-initial", "promoted.acceptance.invalid", age_days=1,
                qualification_status="needs_review", email="", contact_source_url=None, fit_score=None, intent_score=None)
            initial = post("/api/agent/leads", first_review)
            promoted_id = initial["lead_id"]
            client.patch(f"/api/leads/{promoted_id}", json={"notes": "Preserved owner context"})
            qualified = prospect("new-qualified-evidence", "promoted.acceptance.invalid", fit_score=88, intent_score=81,
                need_summary="Verified manual corrections after import")
            promotion = post("/api/agent/leads", qualified)
            promoted = find_lead(promoted_id)
            check("newer_research_promotes_and_fills_late_contact", not initial["initial_draft_allowed"]
                and not promotion["created"] and promotion["initial_draft_allowed"]
                and promoted["qualification_status"] == "qualified" and promoted["fit_score"] == 88
                and promoted["intent_score"] == 81 and promoted["email"] == qualified["email"]
                and promoted["notes"] == "Preserved owner context" and promoted["status"] == "new")
            initial_replay = post("/api/agent/leads", first_review)
            update_replay = post("/api/agent/leads", qualified)
            check("replay_preserves_original_created_and_current_eligibility", initial_replay["replayed"]
                  and initial_replay["created"] is False and initial_replay["original_created"] is True
                  and update_replay["replayed"] and update_replay["original_created"] is False
                  and initial_replay["initial_draft_allowed"] and update_replay["initial_draft_allowed"])
            older = prospect("older-evidence-arrives-late", "promoted.acceptance.invalid", age_days=3,
                qualification_status="needs_review", fit_score=20, intent_score=0, need_summary="Old incomplete observation")
            post("/api/agent/leads", older)
            promoted = find_lead(promoted_id)
            check("older_research_does_not_revert_fresh_qualification", promoted["qualification_status"] == "qualified"
                  and promoted["fit_score"] == 88 and promoted["intent_score"] == 81
                  and promoted["need_summary"] == qualified["need_summary"])
            client.patch(f"/api/leads/{promoted_id}", json={"email": "director@promoted.acceptance.invalid",
                "contact_name": "Owner chosen contact", "notes": "Keep manual contact"})
            after_manual = prospect("new-contact-after-owner-edit", "promoted.acceptance.invalid",
                email="newlyfound@promoted.acceptance.invalid", contact_name="Machine found contact")
            post("/api/agent/leads", after_manual)
            promoted = find_lead(promoted_id)
            check("fresh_research_preserves_manually_selected_contact", promoted["email"] == "director@promoted.acceptance.invalid"
                  and promoted["contact_name"] == "Owner chosen contact" and promoted["notes"] == "Keep manual contact")
            client.patch(f"/api/leads/{promoted_id}", json={"status": "won"})
            terminal = post("/api/agent/leads", prospect("terminal-rediscovery", "promoted.acceptance.invalid"))
            post("/api/agent/drafts", draft_for(promoted_id,"terminal-cold-outreach-blocked"),expected=409)
            check("terminal_stage_blocks_new_cold_outreach", not terminal["initial_draft_allowed"] and find_lead(promoted_id)["status"] == "won")
            with closing(module.connect()) as db:
                db.execute("UPDATE acquisition_keys SET original_created=NULL WHERE key='needs-review-initial'")
                db.commit()
            check("legacy_origin_replay_remains_unknown", post("/api/agent/leads", first_review)["original_created"] is None)

            downgrade_base = prospect("downgrade-start", "downgrade.acceptance.invalid", age_days=1)
            downgrade_id = post("/api/agent/leads", downgrade_base)["lead_id"]
            downgrade_draft = post("/api/agent/drafts", draft_for(downgrade_id,"downgrade-ready-draft"))
            post(f"/api/outreach/{downgrade_draft['id']}/review", {"action":"approve"}, agent=False)
            post("/api/agent/leads", prospect("downgrade-current", "downgrade.acceptance.invalid",
                qualification_status="not_fit", intent_score=0))
            no_claim = post("/api/agent/outreach/claim")["message"] is None
            post(f"/api/outreach/{downgrade_draft['id']}/review", {"action":"approve"}, agent=False,expected=409)
            detail = client.get(f"/api/leads/{downgrade_id}/research").json()
            check("downgrade_demotes_ready_letter_and_blocks_reapproval", no_claim and detail["outreach"][0]["state"] == "draft")

            contact_id = post("/api/agent/leads", prospect("changed-email-start", "changed.acceptance.invalid"))["lead_id"]
            contact_draft = post("/api/agent/drafts", draft_for(contact_id,"changed-recipient-draft"))
            post(f"/api/outreach/{contact_draft['id']}/review", {"action":"approve"}, agent=False)
            client.patch(f"/api/leads/{contact_id}", json={"email":"newowner@changed.acceptance.invalid"})
            check("owner_email_change_blocks_stale_approved_recipient", post("/api/agent/outreach/claim")["message"] is None
                and client.get(f"/api/leads/{contact_id}/research").json()["outreach"][0]["state"] == "draft")
            post("/api/agent/heartbeat", {"run_id": "isolated-pg-acceptance", "state": "completed", "summary": "Synthetic API checks; no SMTP"})
            automation = client.get("/api/automation").json()
            check("automation_summary_reports_worker", automation["worker_seen"])
            final_manual = [row for row in client.get("/api/leads").json() if row["id"] in (101,202)]
            check("legacy_manual_rows_still_preserved_after_api", len(final_manual) == 2 and all(
                all(after[key] == value for key,value in old.items()) for old,after in zip(legacy,sorted(final_manual,key=lambda x:x["id"])) ))
        with psycopg.connect(admin_dsn) as admin:
            after = [row[0] for row in admin.execute("SELECT datname FROM pg_database ORDER BY datname")]
            check("only_named_test_database_created", set(after) - set(before) == {db_name} and set(before) <= set(after))
        report["ok"] = True
    except Exception as exc:
        report["ok"] = False
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:1500]
        report["traceback"] = traceback.format_exc()[-5000:]
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    target = report_dir / "report.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(target, 0o600)
    print(json.dumps({"ok": report["ok"], "report": str(target), "database": db_name,
                      "passed": sum(bool(item["passed"]) for item in report["checks"]),
                      "checks": len(report["checks"]), "smtp_calls": 0}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
