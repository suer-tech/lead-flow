"""Actual HTTP contract against an isolated app, never the Vercel database."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import uuid

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None

from worker.engine import Engine
from worker.store import Store
from worker.tests.test_research import ASSESSMENT, COMPANY, FakeRuntime, REVIEW, TEXTS


class Bridge:
    def __init__(self, client, token):
        self.client, self.token = client, token

    def post(self, path, payload):
        result = self.client.post(path, json=payload, headers={"Authorization": "Bearer " + self.token})
        if result.status_code != 200:
            raise HTTPError("https://isolated.example" + path, result.status_code, "isolated HTTP failure", {}, io.BytesIO(result.content))
        return result.json()

    def ingest(self, payload):
        return self.post("/api/agent/leads", payload)

    def draft(self, payload):
        return self.post("/api/agent/drafts", payload)


@unittest.skipIf(TestClient is None, "Optional CRM HTTP dependencies unavailable")
class CrmContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.token = "isolated-worker-contract-" + "x" * 32
        self.environment = patch.dict(os.environ, {
            "CRM_DB_PATH": str(Path(self.temp.name) / "crm.sqlite"), "CRM_AGENT_TOKEN": self.token,
            "CRM_PASSWORD": "test-only-owner", "CRM_SECRET_KEY": "test-only-secret", "CRM_SEED_DEMO": "0",
            "CRM_OUTREACH_ENABLED": "0", "CRM_AUTO_APPROVE": "0", "DATABASE_URL": "", "POSTGRES_URL": "", "VERCEL": "",
        })
        self.environment.start()
        self.module_name = "_worker_crm_contract_" + uuid.uuid4().hex
        spec = importlib.util.spec_from_file_location(self.module_name, Path(__file__).resolve().parents[2] / "app.py")
        self.module = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.module
        spec.loader.exec_module(self.module)
        self.client = TestClient(self.module.app)
        self.store = Store(Path(self.temp.name) / "worker.sqlite")
        self.config = {"campaign": "isolated", "queries": ["Проверить конкретный документный процесс"],
            "max_candidates": 2, "max_pages": 12, "max_model_calls": 12, "max_signal_age_days": 180,
            "timezone": "UTC", "enabled": False, "mode": "ingest"}
        self.runtime = FakeRuntime()
        fetch = self.runtime.fetch
        self.observed_at = datetime.now(timezone.utc) - timedelta(hours=2)
        def observed(url):
            result = fetch(url)
            result["fetched_at"] = self.observed_at.isoformat()
            return result
        self.runtime.fetch = observed
        self.engine = Engine(self.config, self.store, self.runtime, Bridge(self.client, self.token),
            clock=lambda: datetime.now(timezone.utc))

    def tearDown(self):
        self.store.close()
        self.client.close()
        sys.modules.pop(self.module_name, None)
        self.environment.stop()
        self.temp.cleanup()

    def rows(self, table):
        db = self.module.connect()
        try:
            return [dict(row) for row in db.execute("SELECT * FROM " + table)]
        finally:
            db.close()

    def test_qualified_research_creates_one_draft_and_new_cycle_does_not_repeat(self):
        self.assertTrue(self.engine.run(slot="first")["ok"])
        self.assertEqual(1, len(self.rows("leads")))
        self.assertEqual(1, len(self.rows("outreach")))
        self.assertEqual("draft", self.rows("outreach")[0]["state"])
        with self.store.db:
            self.store.db.execute("UPDATE runs SET created='2020-01-01T00:00:00+00:00'")
        self.observed_at += timedelta(minutes=1)
        self.assertTrue(self.engine.run(slot="after-cooldown")["ok"])
        self.assertEqual(1, len(self.rows("outreach")))
        self.assertEqual("known_company", self.store.rows("SELECT stage FROM candidates ORDER BY rowid DESC")[0]["stage"])

    def test_reviewable_candidate_is_saved_then_can_qualify_on_new_evidence(self):
        self.runtime.assessment["signal_date"] = ""
        self.assertTrue(self.engine.run(slot="first")["ok"])
        self.assertEqual("needs_review", self.rows("leads")[0]["qualification_status"])
        self.assertEqual([], self.rows("outreach"))
        with self.store.db:
            self.store.db.execute("UPDATE runs SET created='2020-01-01T00:00:00+00:00'")
        self.observed_at += timedelta(minutes=1)
        self.runtime.assessment = deepcopy(ASSESSMENT)
        self.assertTrue(self.engine.run(slot="verified-later")["ok"])
        self.assertEqual("qualified", self.rows("leads")[0]["qualification_status"])
        self.assertEqual(1, len(self.rows("outreach")))

    def test_verified_process_without_official_contact_is_only_a_card(self):
        self.runtime.assessment.update(contact_email="", contact_url="", contact_quote="")
        self.assertTrue(self.engine.run(slot="missing-contact")["ok"])
        lead = self.rows("leads")[0]
        self.assertEqual("needs_review", lead["qualification_status"])
        self.assertEqual("", lead["email"])
        self.assertIsNone(lead["contact_source_url"])
        self.assertEqual([], self.rows("outreach"))
        self.assertIsNone(json.loads(self.store.rows("SELECT result FROM candidates")[0]["result"])["draft"])

    def test_unknown_need_can_make_one_discovery_letter_without_promoting_intent(self):
        self.config['discovery_outreach'] = True
        self.runtime.assessment.update(signal_kind='profile_process', current_unmet_need=False,
            signal_date='', remaining_manual_step='')
        self.runtime.review.update(need_quote_supports_claim=False, need_is_unmet=False, process_quote_supports_workflow=False)
        with patch.dict(os.environ, {'CRM_AUTO_APPROVE':'1','CRM_DISCOVERY_OUTREACH_ENABLED':'1'}):
            result = self.engine.run(slot='discovery-first')
        self.assertTrue(result['ok'])
        lead = self.rows('leads')[0]
        self.assertEqual('contact_ready', lead['qualification_status'])
        self.assertEqual(0, lead['intent_score'])
        self.assertNotIn('followup', self.runtime.calls)
        letter = self.rows('outreach')[0]
        self.assertEqual('discovery', letter['purpose'])
        self.assertEqual('ready', letter['state'])
        # A prepared queue does not enable SMTP/claims.
        self.assertEqual(409, self.client.post('/api/agent/outreach/claim', headers={'Authorization':'Bearer '+self.token}).status_code)
        self.assertTrue(self.engine.run(slot='next-search')['ok'])
        self.assertEqual(1, len(self.rows('outreach')))

    def test_discovery_still_rejects_unverified_contact_and_unfit_process(self):
        self.config['discovery_outreach'] = True
        self.runtime.assessment.update(contact_email='', contact_url='', contact_quote='')
        self.runtime.review.update(process_quote_supports_workflow=False,contact_official=False)
        self.engine.run(slot='no-contact')
        self.assertEqual('needs_review', self.rows('leads')[0]['qualification_status'])
        self.assertEqual([], self.rows('outreach'))

    def test_discovery_does_not_admit_unfit_company_or_hr_mailbox(self):
        self.config['discovery_outreach'] = True
        self.runtime.review['fit_valid']=False
        self.engine.run(slot='unfit')
        self.assertEqual([], self.rows('leads'))
        with self.store.db:
            self.store.db.execute("UPDATE runs SET created='2020-01-01T00:00:00+00:00'")
        self.runtime.review['fit_valid']=True
        self.runtime.texts[2]='Для резюме используйте hr@pravo.example; отдел кадров.'
        self.runtime.assessment.update(contact_email='hr@pravo.example',contact_quote=self.runtime.texts[2])
        self.engine.run(slot='hr-is-not-a-sales-contact')
        self.assertEqual('needs_review', self.rows('leads')[0]['qualification_status'])
        self.assertEqual('', self.rows('leads')[0]['email'])
        self.assertEqual([], self.rows('outreach'))

    def test_contact_ready_cannot_make_sales_letter_or_forge_contact_provenance(self):
        self.config['discovery_outreach'] = True
        self.runtime.assessment.update(signal_date='')
        self.engine.run(slot='discovery-card')
        row = self.rows('leads')[0]
        # Remove the first draft only in this disposable fixture, testing the
        # purpose gate separately from first-letter deduplication.
        with closing(self.module.connect()) as db:
            db.execute('DELETE FROM outreach');db.commit()
        payload={'idempotency_key':'wrong-sales-purpose','lead_id':row['id'],'subject':'Process question',
            'body':'Is this process relevant for your team?', 'contact_basis':'owner_review','basis_evidence':'Owner authorized a discovery question'}
        self.assertEqual(409,self.client.post('/api/agent/drafts',json=payload,headers={'Authorization':'Bearer '+self.token}).status_code)
        saved=json.loads(self.store.rows('SELECT result FROM candidates')[0]['result'])['lead']
        saved.update(idempotency_key='forged-contact-proof',company_domain='different.example')
        self.assertEqual(422,self.client.post('/api/agent/leads',json=saved,headers={'Authorization':'Bearer '+self.token}).status_code)


if __name__ == "__main__":
    unittest.main()
