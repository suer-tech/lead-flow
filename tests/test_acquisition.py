"""Transactional CRM behavior using a disposable database, no network mail."""
import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class AcquisitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.environment = patch.dict(os.environ, {
            "CRM_DB_PATH": str(Path(cls.temp.name) / "isolated.sqlite3"),
            "CRM_AGENT_TOKEN": "isolated-agent-token-" + "x" * 32,
            "CRM_PASSWORD": "owner-test-password",
            "CRM_SECRET_KEY": "isolated-owner-secret",
            "CRM_OUTREACH_ENABLED": "1", "CRM_SENDER_EMAIL": "sender@example.org",
            "CRM_MESSAGE_ID_DOMAIN": "example.org", "CRM_SEED_DEMO": "0",
            "CRM_AUTO_APPROVE": "0", "DATABASE_URL": "", "POSTGRES_URL": "", "VERCEL": "",
        })
        cls.environment.start()
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        cls.module = importlib.import_module("app")
        cls.client = TestClient(cls.module.app)
        cls.agent = {"Authorization": "Bearer " + os.environ["CRM_AGENT_TOKEN"]}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.environment.stop()
        cls.temp.cleanup()

    def setUp(self):
        with closing(self.module.connect()) as db, db:
            for table in ("lead_inbox", "outreach", "acquisition_keys", "lead_evidence", "lead_events", "leads", "lead_suppressions", "acquisition_runs"):
                db.execute("DELETE FROM " + table)
        self.client.cookies.clear()

    def login(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"password": "owner-test-password"}).status_code, 200)

    def candidate(self, key="candidate-001", domain="firm.example.org"):
        return {"idempotency_key": key, "company": "Example company", "company_domain": domain,
                "email": "office@" + domain, "contact_source_url": "https://" + domain + "/contacts",
                "need_summary": "Manual entry of court documents", "qualification_status": "qualified",
                "fit_score": 85, "intent_score": 75,
                "evidence": [{"source_url": "https://" + domain + "/request", "quote": "We need to automate manual court document entry",
                              "observed_at": datetime.now(timezone.utc).isoformat(), "signal": "explicit_request", "interpretation": "Buyer requested a matching workflow"}]}

    def ingest(self, payload=None):
        response = self.client.post("/api/agent/leads", json=payload or self.candidate(), headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def draft(self, lead_id, key="draft-001", basis="explicit_request"):
        response = self.client.post("/api/agent/drafts", headers=self.agent, json={"idempotency_key": key, "lead_id": lead_id, "subject": "Your court workflow", "body": "Can we discuss your requested court workflow?", "contact_basis": basis, "basis_evidence": "Public request inviting a response about this precise process"})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def ready(self, draft_id):
        self.login()
        response = self.client.post(f"/api/outreach/{draft_id}/review", json={"action": "approve"})
        self.assertEqual(response.status_code, 200, response.text)

    def claim(self):
        response = self.client.post("/api/agent/outreach/claim", headers=self.agent)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["message"]

    def accepted(self, message):
        payload = {"claim_token": message["claim_token"], "outcome": "accepted", "provider_message_id": message["message_id"], "receipt": "SMTP DATA 250 accepted"}
        response = self.client.post(f"/api/agent/outreach/{message['id']}/result", headers=self.agent, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["delivery_confirmed"])
        return payload

    def reply(self, message, classification="refused", from_email=None):
        body = "Please do not contact us again" if classification == "refused" else "Please tell us about a pilot"
        return {"message_id": "<reply-001@example.org>", "from_email": from_email or message["to"], "in_reply_to": message["message_id"], "body": body, "received_at": datetime.now(timezone.utc).isoformat(), "classification": classification, "classification_quote": body}

    def test_claims_are_paced_and_failed_attempts_consume_daily_company_budget(self):
        first=self.ingest();letter=self.draft(first['lead_id']);self.ready(letter['id'])
        second=self.ingest(self.candidate(key='second-company-key',domain='other.example.org'))
        letter2=self.draft(second['lead_id'],key='second-letter-key');self.ready(letter2['id'])
        with patch.dict(os.environ,{'CRM_DAILY_SEND_LIMIT':'100','CRM_SEND_INTERVAL_SECONDS':'300'}):
            message=self.claim()
            reply=self.client.post('/api/agent/outreach/claim',headers=self.agent).json()
            self.assertEqual('paced',reply['reason'])
        result={'claim_token':message['claim_token'],'outcome':'failed_before_send','receipt':'Connection failed before DATA'}
        self.assertEqual(200,self.client.post(f"/api/agent/outreach/{message['id']}/result",headers=self.agent,json=result).status_code)
        with patch.dict(os.environ,{'CRM_DAILY_SEND_LIMIT':'1','CRM_SEND_INTERVAL_SECONDS':'0'}):
            self.assertEqual('daily_limit',self.client.post('/api/agent/outreach/claim',headers=self.agent).json()['reason'])

    def test_fresh_database_is_empty_and_agent_auth_is_separate(self):
        self.module.init_database()
        self.assertEqual(self.client.get("/api/leads").status_code, 401)
        self.login()
        self.assertEqual(self.client.get("/api/leads").json(), [])
        self.assertEqual(self.client.post("/api/agent/leads", json=self.candidate()).status_code, 401)
        self.assertEqual(self.client.post("/api/agent/leads", json=self.candidate(), headers=self.agent).status_code, 200)

    def test_ingestion_idempotent_and_preserves_owner_notes(self):
        candidate = self.candidate()
        first = self.ingest(candidate)
        self.assertTrue(self.ingest(candidate)["replayed"])
        changed = dict(candidate, company="Changed")
        self.assertEqual(self.client.post("/api/agent/leads", json=changed, headers=self.agent).status_code, 409)
        self.login()
        self.client.patch(f"/api/leads/{first['lead_id']}", json={"notes": "Owner note", "status": "diagnostics"})
        second = self.ingest(self.candidate("candidate-002"))
        self.assertEqual(first["lead_id"], second["lead_id"])
        row = self.client.get("/api/leads").json()[0]
        self.assertEqual(row["notes"], "Owner note")
        self.assertEqual(row["status"], "diagnostics")

    def test_verified_new_research_promotes_review_card_and_fills_machine_contact(self):
        first = self.candidate('review-first')
        first.update(qualification_status='needs_review',email='',contact_source_url=None,fit_score=None,intent_score=None)
        first['evidence'][0]['observed_at'] = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        ack = self.ingest(first)
        self.assertFalse(ack['initial_draft_allowed'])
        self.login()
        self.client.patch(f"/api/leads/{ack['lead_id']}",json={'notes':'Owner context'})
        update = self.candidate('qualified-later')
        update.update(fit_score=88,intent_score=81,need_summary='Verified manual corrections after import')
        promoted = self.ingest(update)
        self.assertFalse(promoted['created'])
        self.assertFalse(promoted['original_created'])
        self.assertTrue(promoted['initial_draft_allowed'])
        self.assertEqual(promoted['qualification_status'],'qualified')
        row = self.client.get('/api/leads').json()[0]
        self.assertEqual((row['notes'],row['status']),('Owner context','new'))
        self.assertEqual((row['fit_score'],row['intent_score'],row['email']),(88,81,update['email']))
        self.draft(ack['lead_id'])

    def test_new_research_never_overwrites_manually_changed_contact(self):
        first = self.candidate('first-manual-contact')
        first['evidence'][0]['observed_at'] = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        ack = self.ingest(first)
        self.login()
        self.client.patch(f"/api/leads/{ack['lead_id']}",json={'email':'director@firm.example.org','contact_name':'Owner selected person','notes':'Keep this'})
        changed = self.candidate('new-machine-contact')
        changed.update(email='newlyfound@firm.example.org',contact_name='Different person')
        self.ingest(changed)
        row = self.client.get('/api/leads').json()[0]
        self.assertEqual(row['email'],'director@firm.example.org')
        self.assertEqual(row['contact_name'],'Owner selected person')
        self.assertEqual(row['notes'],'Keep this')

    def test_older_observations_enrich_evidence_without_reverting_new_qualification(self):
        latest = self.candidate('latest-proof')
        ack = self.ingest(latest)
        old = self.candidate('old-proof-arrived-late')
        old.update(qualification_status='needs_review',fit_score=20,intent_score=0,need_summary='Old incomplete research')
        old['evidence'][0]['observed_at'] = (datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
        result = self.ingest(old)
        self.assertEqual(result['qualification_status'],'qualified')
        self.login()
        data = self.client.get(f"/api/leads/{ack['lead_id']}/research").json()
        self.assertEqual(data['lead']['fit_score'],85)
        self.assertEqual(len(data['evidence']),2)

    def test_ingestion_replay_preserves_original_created_and_current_draft_eligibility(self):
        original = self.candidate('original-created')
        first = self.ingest(original)
        replay = self.ingest(original)
        self.assertTrue(first['created'])
        self.assertFalse(replay['created'])
        self.assertTrue(replay['original_created'])
        self.assertTrue(replay['initial_draft_allowed'])
        known = self.candidate('known-company-update')
        self.assertFalse(self.ingest(known)['original_created'])
        replay_known = self.ingest(known)
        self.assertTrue(replay_known['replayed'])
        self.assertFalse(replay_known['original_created'])
        message = self.draft(first['lead_id'])
        existing = self.ingest(known)
        self.assertFalse(existing['initial_draft_allowed'])
        self.assertEqual(existing['existing_initial_draft'],{'id':message['id'],'state':'draft'})

    def test_replaying_pre_archive_ingestion_reports_current_suppression(self):
        candidate = self.candidate('before-archive')
        first = self.ingest(candidate)
        self.login()
        self.client.delete(f"/api/leads/{first['lead_id']}")
        replay = self.ingest(candidate)
        self.assertTrue(replay['replayed'])
        self.assertTrue(replay['archived'])
        self.assertTrue(replay['suppressed'])
        self.assertFalse(replay['initial_draft_allowed'])

    def test_downgrade_demotes_unsent_ready_letter_and_prevents_reapproval(self):
        first = self.candidate('before-downgrade')
        first['evidence'][0]['observed_at'] = (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()
        lead_id = self.ingest(first)['lead_id']
        draft = self.draft(lead_id)
        self.ready(draft['id'])
        new = self.candidate('after-downgrade')
        new.update(qualification_status='not_fit',intent_score=0)
        self.ingest(new)
        self.assertIsNone(self.claim())
        detail = self.client.get(f'/api/leads/{lead_id}/research').json()
        self.assertEqual(detail['outreach'][0]['state'],'draft')
        self.assertEqual(self.client.post(f"/api/outreach/{draft['id']}/review",json={'action':'approve'}).status_code,409)

    def test_changed_owner_email_prevents_claiming_previously_approved_recipient(self):
        lead_id = self.ingest()['lead_id']
        draft = self.draft(lead_id)
        self.ready(draft['id'])
        self.client.patch(f'/api/leads/{lead_id}',json={'email':'different@firm.example.org'})
        self.assertIsNone(self.claim())
        detail = self.client.get(f'/api/leads/{lead_id}/research').json()
        self.assertEqual(detail['outreach'][0]['state'],'draft')

    def test_cancelled_first_letter_never_rearmed_by_rediscovery(self):
        lead_id = self.ingest()['lead_id']
        draft = self.draft(lead_id)
        self.login()
        self.client.post(f"/api/outreach/{draft['id']}/review",json={'action':'cancel'})
        replay = self.ingest(self.candidate('rediscovery-cancelled'))
        self.assertFalse(replay['initial_draft_allowed'])
        self.assertEqual(replay['existing_initial_draft']['state'],'cancelled')
        response = self.client.post('/api/agent/drafts',headers=self.agent,json={'idempotency_key':'different-draft-key','lead_id':lead_id,'subject':'New attempt','body':'Another attempt should not happen automatically','contact_basis':'owner_review','basis_evidence':'Owner cancellation must remain respected'})
        self.assertEqual(response.status_code,409)

    def test_owner_sales_stage_is_preserved_and_never_restarts_cold_outreach(self):
        candidate = self.candidate('owner-active-conversation')
        lead_id = self.ingest(candidate)['lead_id']
        self.login()
        self.client.patch(f'/api/leads/{lead_id}',json={'status':'diagnostics'})
        result = self.ingest(self.candidate('rediscovery-active-conversation'))
        self.assertFalse(result['initial_draft_allowed'])
        self.assertEqual(self.client.get('/api/leads').json()[0]['status'],'diagnostics')
        response = self.client.post('/api/agent/drafts',headers=self.agent,json={'idempotency_key':'cold-after-dialogue','lead_id':lead_id,'subject':'Initial email','body':'Do not restart a conversation already owned by a human','contact_basis':'owner_review','basis_evidence':'Research does not reset a manually advanced sales stage'})
        self.assertEqual(response.status_code,409)

    def test_legacy_idempotency_key_keeps_unknown_origin_without_inventing_creation(self):
        candidate = self.candidate('legacy-key')
        self.ingest(candidate)
        with closing(self.module.connect()) as db,db:
            db.execute('UPDATE acquisition_keys SET original_created=NULL')
        replay = self.ingest(candidate)
        self.assertIsNone(replay['original_created'])
        self.assertTrue(replay['initial_draft_allowed'])

    def test_profile_alone_is_not_qualified(self):
        candidate = self.candidate()
        candidate["evidence"][0]["signal"] = "company_profile"
        self.assertEqual(self.client.post("/api/agent/leads", json=candidate, headers=self.agent).status_code, 422)
        candidate["qualification_status"] = "unknown"
        self.assertEqual(self.client.post("/api/agent/leads", json=candidate, headers=self.agent).status_code, 200)

    def test_contact_requires_source_and_header_injection_rejected(self):
        candidate = self.candidate()
        candidate["contact_source_url"] = None
        self.assertEqual(self.client.post("/api/agent/leads", json=candidate, headers=self.agent).status_code, 422)
        candidate = self.candidate()
        candidate["email"] = "x@example.org\r\nBcc: victim@example.org"
        self.assertEqual(self.client.post("/api/agent/leads", json=candidate, headers=self.agent).status_code, 422)

    def test_single_initial_email_and_only_receipt_changes_status(self):
        lead_id = self.ingest()["lead_id"]
        draft = self.draft(lead_id)
        self.assertEqual(draft["state"], "draft")
        self.assertIsNone(self.claim())
        self.ready(draft["id"])
        message = self.claim()
        self.assertIsNone(self.claim())
        invalid = self.client.post(f"/api/agent/outreach/{message['id']}/result", headers=self.agent, json={"claim_token":message["claim_token"],"outcome":"accepted"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.client.get("/api/leads").json()[0]["status"], "new")
        payload = self.accepted(message)
        replay = self.client.post(f"/api/agent/outreach/{message['id']}/result", headers=self.agent, json=payload)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.client.get("/api/leads").json()[0]["status"], "proposal_sent")

    def test_concurrent_claims_get_one_message(self):
        draft = self.draft(self.ingest()["lead_id"])
        self.ready(draft["id"])
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(pool.map(lambda _: self.client.post("/api/agent/outreach/claim", headers=self.agent).json(), range(4)))
        self.assertEqual(sum(r["message"] is not None for r in responses), 1)

    def test_expired_claim_becomes_uncertain_never_requeued(self):
        draft = self.draft(self.ingest()["lead_id"])
        self.ready(draft["id"])
        message = self.claim()
        with closing(self.module.connect()) as db, db:
            db.execute("UPDATE outreach SET claimed_at=?", ((datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),))
        self.assertIsNone(self.claim())
        with closing(self.module.connect()) as db, db:
            self.assertEqual(db.execute("SELECT state FROM outreach").fetchone()["state"], "uncertain")
        response=self.client.post(f"/api/agent/outreach/{message['id']}/result",headers=self.agent,json={"claim_token":message["claim_token"],"outcome":"accepted","provider_message_id":message["message_id"],"receipt":"Late receipt"})
        self.assertEqual(response.status_code,409)

    def test_refusal_archives_suppresses_and_deduplicates(self):
        lead_id = self.ingest()["lead_id"]
        draft = self.draft(lead_id)
        self.ready(draft["id"])
        message = self.claim()
        self.accepted(message)
        reply = self.reply(message)
        result = self.client.post("/api/agent/replies",headers=self.agent,json=reply)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()["match_state"],"matched")
        self.assertTrue(self.client.post("/api/agent/replies",headers=self.agent,json=reply).json()["replayed"])
        self.assertEqual(self.client.get("/api/leads").json(),[])
        self.assertEqual(self.client.get("/api/leads?archived=true").json()[0]["archive_reason"],"refused")
        self.assertTrue(self.ingest(self.candidate("candidate-new"))["suppressed"])

    def test_unrelated_reply_does_not_change_lead(self):
        draft=self.draft(self.ingest()["lead_id"])
        self.ready(draft["id"]);message=self.claim();self.accepted(message)
        response=self.client.post("/api/agent/replies",headers=self.agent,json=self.reply(message,from_email="other@example.org"))
        self.assertEqual(response.json()["match_state"],"needs_review")
        self.assertEqual(len(self.client.get("/api/leads").json()),1)

    def test_auto_reply_not_interest_and_quote_required(self):
        draft=self.draft(self.ingest()["lead_id"])
        self.ready(draft["id"]);message=self.claim();self.accepted(message)
        reply=self.reply(message,"interested");reply["classification_quote"]="Not present in source"
        self.assertEqual(self.client.post("/api/agent/replies",headers=self.agent,json=reply).status_code,422)
        reply=self.reply(message,"auto_reply")
        self.assertEqual(self.client.post("/api/agent/replies",headers=self.agent,json=reply).status_code,200)
        row=self.client.get("/api/leads").json()[0]
        self.assertEqual(row["status"],"proposal_sent")
        self.assertIsNone(row["last_human_reply_at"])

    def test_owner_archive_preserves_evidence_and_blocks_new_mail(self):
        lead_id=self.ingest()["lead_id"]
        draft=self.draft(lead_id)
        self.login()
        self.assertEqual(self.client.delete(f"/api/leads/{lead_id}").status_code,204)
        detail=self.client.get(f"/api/leads/{lead_id}/research").json()
        self.assertEqual(len(detail["evidence"]),1)
        self.assertEqual(detail["outreach"][0]["state"],"cancelled")
        self.assertTrue(self.ingest(self.candidate("new-discovery"))["suppressed"])

    def test_no_reply_archive_disabled_until_configured(self):
        draft=self.draft(self.ingest()["lead_id"])
        self.ready(draft["id"]);message=self.claim();self.accepted(message)
        with closing(self.module.connect()) as db, db:
            db.execute("UPDATE outreach SET accepted_at=?",((datetime.now(timezone.utc)-timedelta(days=30)).isoformat(),))
        self.assertEqual(self.client.post("/api/agent/maintenance",headers=self.agent).json()["archived"],0)
        with patch.dict(os.environ,{"CRM_NO_REPLY_ARCHIVE_ENABLED":"1"}):
            self.assertEqual(self.client.post("/api/agent/maintenance",headers=self.agent).json()["archived"],1)
        self.assertTrue(self.ingest(self.candidate("new-discovery"))["archived"])

    def test_auto_ready_only_explicit_qualified_contact_when_commissioned(self):
        with patch.dict(os.environ,{"CRM_AUTO_APPROVE":"1"}):
            lead_id=self.ingest()["lead_id"]
            self.assertEqual(self.draft(lead_id)["state"],"ready")
            second=self.ingest(self.candidate("another-company","second.example.org"))["lead_id"]
            self.assertEqual(self.draft(second,"draft-002","unknown")["state"],"draft")


if __name__ == "__main__":
    unittest.main()
