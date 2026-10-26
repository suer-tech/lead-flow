from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError

from worker.contracts import exact_quote, public_url, validate_assessment, LETTER, LETTER_REVIEW_FLAGS
from worker.engine import Engine
from worker.runtime import NoRedirect
from worker.store import Store


TIME = datetime(2026, 10, 26, 10, tzinfo=timezone.utc)
COMPANY = {"company_name": "Право Тест", "company_domain": "pravo.example", "segment": "bankruptcy_legal",
    "source_urls": ["https://pravo.example/about", "https://pravo.example/need", "https://pravo.example/contact"]}
TEXTS = ["Право Тест — юридическая компания по банкротству.",
    "Ищем подрядчика для автоматизации обработки судебных определений. Бюджет обсуждается после пилота.",
    "Наш официальный контакт: office@pravo.example для деловых вопросов."]
ASSESSMENT = {"company_name": "Право Тест", "identity_url": COMPANY["source_urls"][0], "identity_quote": TEXTS[0],
    "signal_url": COMPANY["source_urls"][1], "signal_quote": TEXTS[1], "signal_kind": "explicit_need",
    "signal_date": "2026-10-20", "date_quote": "", "current_unmet_need": True,
    "use_case": "извлечение полей из судебных определений", "why_fit": "Есть явный запрос на автоматизацию судебных документов",
    "existing_solution": "неизвестно", "remaining_manual_step": "обработка судебных определений вручную", "budget_evidence": "неизвестно",
    "contact_email": "office@pravo.example", "contact_url": COMPANY["source_urls"][2],
    "contact_quote": TEXTS[2], "unknowns": ["Объем документов неизвестен"]}
REVIEW = {"company_attribution_valid": True, "need_quote_supports_claim": True, "need_is_unmet": True,
    "contact_official": True, "fit_valid": True, "process_quote_supports_workflow": True, "reason": "Явный запрос компании", "unknowns": []}


class FakeRuntime:
    def __init__(self):
        self.calls = []
        self.assessment = deepcopy(ASSESSMENT)
        self.review = deepcopy(REVIEW)
        self.fail_assess = False
        self.busy = False
        self.released = 0
        self.companies = [deepcopy(COMPANY)]
        self.texts = list(TEXTS)
        self.letter_reviews = []
        self.letter_feedback = []
        self.proposal_patch = {}
        self.fail_letter_review = False
        self.search_exclusions = []
        self.followup_urls = []
        self.extra_sources = {}
        self.followup_fail = False
        self.after_followup_assessment = None
        self.quote_patch = {}
        self.fail_quote_repair = False

    @contextmanager
    def lease(self, run_id):
        try:
            if self.busy:
                raise RuntimeError("research_resources_busy")
            yield
        finally:
            self.released += 1

    def search(self, query, excluded_domains=(), limit=4):
        self.calls.append("search")
        self.search_exclusions.append(list(excluded_domains))
        return {"companies": self.companies}

    def fetch(self, url):
        self.calls.append(url)
        if url in self.extra_sources:
            return deepcopy(self.extra_sources[url])
        return {"ok": True, "url": url, "text": self.texts[COMPANY["source_urls"].index(url)], "title": "Company",
            "fetched_at": "2026-10-26T10:00:00+00:00", "published_at": "2026-10-20T10:00:00+00:00"}

    def assess(self, candidate, sources):
        self.calls.append("assess")
        if self.fail_assess:
            self.fail_assess = False
            raise RuntimeError("secret must not enter logs")
        return deepcopy(self.assessment)

    def followup(self, candidate, missing, sources):
        self.calls.append("followup")
        if self.followup_fail:
            raise RuntimeError("private source details")
        if self.after_followup_assessment:
            self.assessment = deepcopy(self.after_followup_assessment)
        return {"source_urls": list(self.followup_urls)}

    def repair_quotes(self, candidate, assessment, sources, targets):
        self.calls.append("repair_quotes")
        if self.fail_quote_repair:
            raise RuntimeError("private citation request")
        return {key: assessment.get(key, "") for key in ("identity_quote", "signal_quote", "contact_quote")} | self.quote_patch

    def critique(self, candidate, assessment, sources):
        self.calls.append("critique")
        return deepcopy(self.review)

    def compose(self, candidate, assessment, sources, feedback=None):
        self.calls.append("compose")
        self.letter_feedback.append(deepcopy(feedback))
        return {"subject": "Вопрос об обработке судебных определений",
            "observation": "Увидел на вашем сайте описание задачи: " + assessment["use_case"] + ".",
            "question": "Актуальна ли сейчас задача извлечения данных из судебных определений?",
            "pilot_focus": ("Предлагаю совместный пилот для одного клиента: проверить извлечение сумм и реквизитов на его образцах."
                if candidate["segment"] == "crm_integrator" else "Предлагаю проверить извлечение сумм и реквизитов на небольшой выборке ваших документов."),
            "offer_kind": "partner_integration" if candidate["segment"] == "crm_integrator" else "direct_pilot", **self.proposal_patch}

    def review_letter(self, candidate, assessment, sources, proposal):
        self.calls.append("review_letter")
        if self.fail_letter_review:
            self.fail_letter_review = False
            raise RuntimeError("private model details")
        if self.letter_reviews:
            return deepcopy(self.letter_reviews.pop(0))
        return {**{name: True for name in LETTER_REVIEW_FLAGS}, "approved": True,
            "reason": "Source grounded and bounded offer", "revision_feedback": ""}


class FakeCrm:
    def __init__(self):
        self.leads = {}; self.drafts = {}; self.suppressed = False; self.crash_draft = False

    def ingest(self, payload):
        if self.suppressed:
            return {"suppressed": True}
        self.leads.setdefault(payload["idempotency_key"], payload)
        return {"lead_id": 10, "created": True, "original_created": True,
            "qualification_status": payload["qualification_status"], "initial_draft_allowed": payload["qualification_status"] == "qualified"}

    def draft(self, payload):
        self.drafts.setdefault(payload["idempotency_key"], payload)
        if self.crash_draft:
            self.crash_draft = False
            raise OSError("connection lost after CRM accepted")
        return {"id": 20, "state": "draft"}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = {"campaign": "test", "database": str(Path(self.tmp.name) / "db.sqlite"),
            "queries": ["Поиск потребности OCR документов"], "max_candidates": 4, "max_pages": 12,
            "max_model_calls": 10, "max_signal_age_days": 180, "timezone": "UTC", "enabled": False,
            "mode": "preview"}
        self.store = Store(self.config["database"])
        self.runtime = FakeRuntime(); self.crm = FakeCrm()
        self.engine = Engine(self.config, self.store, self.runtime, self.crm, clock=lambda: TIME)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def candidate(self):
        return self.store.rows("SELECT * FROM candidates")[0]

    def test_preview_real_pipeline_records_evidence_without_outreach(self):
        result = self.engine.run()
        self.assertTrue(result["ok"])
        self.assertEqual(1, self.runtime.released)
        row = self.candidate(); payload = json.loads(row["result"])
        self.assertEqual("preview", row["stage"])
        self.assertEqual("qualified", payload["lead"]["qualification_status"])
        self.assertEqual("owner_review", payload["draft"]["contact_basis"])
        self.assertEqual(3, len(payload["lead"]["evidence"]))
        self.assertFalse(self.crm.leads)
        self.assertTrue(payload["draft"]["body"].endswith("AI Studio"))
        self.assertNotIn("Ильшат", payload["draft"]["body"])
        self.assertEqual(1, self.runtime.calls.count("compose"))
        self.assertTrue(json.loads(row["letter_review"])["approved"])

    def test_same_day_complete_run_does_not_repeat_models(self):
        self.engine.run(); calls = list(self.runtime.calls)
        self.engine.run()
        self.assertEqual(calls, self.runtime.calls)

    def test_disabled_scheduler_does_no_work(self):
        self.assertEqual("disabled", self.engine.run(scheduled=True)["state"])
        self.assertFalse(self.runtime.calls)

    def test_resume_after_model_failure_skips_completed_fetch_and_search(self):
        self.runtime.fail_assess = True
        self.assertFalse(self.engine.run()["ok"])
        run_id = self.store.summary()["runs"][0]["id"]
        self.assertTrue(self.engine.run(slot="2026-10-27")["ok"])
        self.assertEqual(run_id, self.store.summary()["runs"][0]["id"])
        self.assertEqual(1, self.runtime.calls.count("search"))
        self.assertEqual(1, self.runtime.calls.count(COMPANY["source_urls"][0]))
        self.assertNotIn("secret", json.dumps(self.store.rows("SELECT * FROM events")))

    def test_quote_fabrication_rejected_before_critic(self):
        self.runtime.assessment["signal_quote"] = "Несуществующий запрос на распознавание документов"
        self.engine.run()
        self.assertEqual("rejected", self.candidate()["stage"])
        self.assertNotIn("critique", self.runtime.calls)

    def test_guessed_email_rejected(self):
        self.runtime.assessment["contact_email"] = "ceo@pravo.example"
        self.engine.run()
        payload = json.loads(self.candidate()["result"])
        self.assertEqual("needs_review", payload["lead"]["qualification_status"])
        self.assertEqual("", payload["lead"]["email"])
        self.assertIsNone(payload["draft"])
        self.assertIn("email_not_in_official_quote", payload["lead"]["limitations"])

    def test_existing_solution_not_buying_signal(self):
        self.runtime.assessment["signal_kind"] = "existing_solution"
        self.engine.run()
        payload = json.loads(self.candidate()["result"])
        self.assertEqual("needs_review", payload["lead"]["qualification_status"])
        self.assertEqual(0, payload["lead"]["intent_score"])

    def test_existing_pau_with_verified_manual_corrections_can_qualify(self):
        quote = "Используем ПАУ, но после импорта определений вручную исправляем даты и суммы: автоматизация регулярно ошибается."
        self.runtime.texts[1] = quote
        self.runtime.assessment.update(signal_quote=quote, signal_kind="manual_work",
            existing_solution="ПАУ с импортом PDF", current_unmet_need=True,
            remaining_manual_step="Исправление дат и сумм после ошибочного импорта в ПАУ",
            why_fit="Проверить извлечение конкретных полей и время ручных исправлений на пилоте")
        self.engine.run()
        payload = json.loads(self.candidate()["result"])
        self.assertEqual("qualified", payload["lead"]["qualification_status"])
        self.assertEqual("manual_work", payload["lead"]["evidence"][0]["signal"])
        self.assertIn("ПАУ", payload["lead"]["limitations"])

    def test_vendor_feature_claim_alone_is_not_customer_need_or_satisfied_customer_proof(self):
        quote = "Наш продукт автоматически извлекает все нужные поля из судебных документов без ручного труда."
        self.runtime.texts[1] = quote
        self.runtime.assessment.update(signal_quote=quote, signal_kind="existing_solution",
            existing_solution="Заявленная реклама ПАУ", current_unmet_need=False, remaining_manual_step="")
        self.runtime.review["process_quote_supports_workflow"] = False
        self.engine.run()
        self.assertEqual("rejected", self.candidate()["stage"])
        self.assertIsNone(self.candidate()["result"])

    def test_static_prompt_requires_review_before_vendor_comparison(self):
        from worker.runtime import PRODUCT
        self.assertIn("НЕ квалифицирует и НЕ дисквалифицирует", PRODUCT)
        self.assertIn("До сравнительного вывода ищи отзывы от первого лица", PRODUCT)
        self.assertIn("Это заявления о функциях", PRODUCT)

    def test_missing_residual_manual_step_is_not_qualified(self):
        self.runtime.assessment["remaining_manual_step"] = ""
        self.engine.run()
        payload = json.loads(self.candidate()["result"])
        self.assertEqual("needs_review", payload["lead"]["qualification_status"])
        self.assertIn("remaining_workflow_gap_unknown", payload["lead"]["limitations"])

    def test_curated_product_context_loaded_from_fixed_repository_files(self):
        from worker.runtime import product_context
        context = product_context()
        self.assertIn("DaData", context)
        self.assertIn("Bankro", context)
        self.assertIn("do_not_claim", context)

    def test_independent_critic_rejects_agency_or_wrong_company(self):
        self.runtime.review["company_attribution_valid"] = False
        self.engine.run()
        self.assertEqual("independent_review_rejected", self.candidate()["error"])

    def test_unknown_date_needs_review_never_invented_today(self):
        self.runtime.assessment["signal_date"] = ""
        self.engine.run()
        result = json.loads(self.candidate()["result"])
        self.assertEqual("needs_review", result["lead"]["qualification_status"])
        self.assertIn("signal_date_unverified", result["lead"]["limitations"])

    def test_old_or_future_signal_rejected(self):
        for date in ("2020-01-01", "2027-01-01"):
            assessment = {**ASSESSMENT, "signal_date": date}
            sources = [self.runtime.fetch(url) for url in COMPANY["source_urls"]]
            result = validate_assessment(COMPANY, assessment, sources, TIME.date())
            self.assertFalse(result["eligible"])

    def test_hiring_not_high_intent_or_qualified(self):
        self.runtime.assessment["signal_kind"] = "hiring"
        self.engine.run()
        result = json.loads(self.candidate()["result"])
        self.assertEqual("needs_review", result["lead"]["qualification_status"])
        self.assertLess(result["lead"]["intent_score"], 50)

    def test_deduplicates_companies_across_queries(self):
        self.config["queries"].append("Второй поиск тех же компаний")
        self.engine.run()
        self.assertEqual(1, len(self.store.rows("SELECT * FROM candidates")))
        self.assertEqual(1, self.runtime.calls.count("assess"))

    def test_resource_lease_failure_no_models(self):
        self.runtime.busy = True
        result = self.engine.run()
        self.assertEqual("waiting_resources", result["runs"][0]["state"])
        self.assertFalse(self.runtime.calls)
        self.assertEqual(1, self.runtime.released)

    def test_total_model_budget_survives_crash(self):
        self.config["max_model_calls"] = 1
        result = self.engine.run()
        self.assertEqual("completed_with_gaps", result["runs"][0]["state"])
        self.assertEqual(1, result["runs"][0]["model_calls"])
        self.assertEqual(1, self.runtime.calls.count("search"))
        self.engine.run()
        self.assertNotIn("assess", self.runtime.calls)

    def test_page_budget_is_enforced(self):
        self.config["max_pages"] = 1
        self.engine.run()
        self.assertEqual(1, len(self.store.rows("SELECT * FROM pages")))
        self.assertEqual("rejected", self.candidate()["stage"])

    def test_publish_separate_idempotent_lead_and_draft(self):
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        self.engine.publish(ident); self.engine.publish(ident)
        self.assertEqual(1, len(self.crm.leads)); self.assertEqual(1, len(self.crm.drafts))

    def test_publish_crash_after_draft_acceptance_uses_same_key(self):
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        self.crm.crash_draft = True
        with self.assertRaises(OSError):
            self.engine.publish(ident)
        self.assertEqual("pending_publish", self.store.run(ident)["state"])
        self.assertIsNotNone(self.candidate()["lead_ack"])
        self.engine.publish(ident)
        self.assertEqual(1, len(self.crm.leads)); self.assertEqual(1, len(self.crm.drafts))

    def test_automatic_publish_failure_resumes_next_day_without_models_or_lease(self):
        self.config["mode"] = "ingest"
        self.crm.crash_draft = True
        first = self.engine.run(slot="first-day")
        ident = first["runs"][0]["id"]
        self.assertFalse(first["ok"])
        self.assertEqual("pending_publish", first["runs"][0]["state"])
        self.assertEqual("completed", first["runs"][0]["research_state"])
        calls = list(self.runtime.calls)
        self.runtime.busy = True
        second = self.engine.run(slot="next-day")
        self.assertTrue(second["ok"])
        self.assertEqual(ident, second["runs"][0]["id"])
        self.assertEqual("completed", second["runs"][0]["state"])
        self.assertEqual(calls, self.runtime.calls)
        self.assertEqual(1, self.runtime.released)
        self.assertEqual(1, len(self.store.rows("SELECT * FROM runs")))
        self.assertEqual(1, len(self.crm.drafts))

    def test_partial_research_outcome_survives_publish_failure_and_recovery(self):
        self.engine.run()
        ident = self.store.summary()["runs"][0]["id"]
        self.store.finish_research(ident, "completed_with_gaps", "model_budget_exhausted", publish_pending=True)
        calls = list(self.runtime.calls)
        self.crm.crash_draft = True
        self.assertFalse(self.engine.run()["ok"])
        result = self.engine.run()
        self.assertEqual("completed_with_gaps", result["runs"][0]["state"])
        self.assertEqual("model_budget_exhausted", result["runs"][0]["error"])
        self.assertEqual(calls, self.runtime.calls)

    def test_suppressed_company_does_not_create_draft(self):
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        self.crm.suppressed = True; self.engine.publish(ident)
        self.assertFalse(self.crm.drafts)
        self.assertEqual("suppressed", self.candidate()["stage"])

    def test_publish_needs_review_ingests_card_but_keeps_letter_local(self):
        self.runtime.assessment["signal_date"] = ""
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        self.engine.publish(ident); self.engine.publish(ident)
        row = self.candidate()
        self.assertEqual("review_needed", row["stage"])
        self.assertEqual(1, len(self.crm.leads))
        self.assertFalse(self.crm.drafts)
        self.assertIsNotNone(row["lead_ack"])
        self.assertIsNone(row["draft_ack"])
        self.assertTrue(json.loads(row["result"])["draft"]["body"])

    def test_publish_reviewable_card_does_not_block_next_qualified_company(self):
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        payload = json.loads(self.candidate()["result"])
        payload["lead"].update(company_domain="aaa.example", idempotency_key="reviewable-card-key", qualification_status="needs_review")
        payload["draft"]["idempotency_key"] = "reviewable-letter-key"
        with self.store.db:
            self.store.db.execute("INSERT INTO candidates(run_id,domain,data,stage,result) VALUES(?,?,?,?,?)",
                (ident, "aaa.example", "{}", "preview", json.dumps(payload)))
        self.engine.publish(ident)
        self.assertEqual(2, len(self.crm.leads))
        self.assertEqual(1, len(self.crm.drafts))
        self.assertNotIn("reviewable-letter-key", self.crm.drafts)
        rows = {row["domain"]: row for row in self.store.rows("SELECT * FROM candidates")}
        self.assertEqual("review_needed", rows["aaa.example"]["stage"])
        self.assertEqual("published", rows[COMPANY["company_domain"]]["stage"])

    def test_changed_config_cannot_mix_in_pending_cycle(self):
        self.runtime.fail_assess = True; self.engine.run()
        self.config["queries"] = ["Несвязанное направление исследования"]
        with self.assertRaisesRegex(ValueError, "config_changed"):
            self.engine.run()

    def test_private_and_signed_urls_rejected(self):
        for url in ("http://localhost/a", "http://127.0.0.1/a", "https://example.com/?token=private", "https://x:y@example.com/a"):
            with self.assertRaises(ValueError):
                public_url(url)

    def test_quotes_are_literal_and_bounded(self):
        self.assertTrue(exact_quote("Компания   переносит данные вручную", "Компания переносит данные вручную"))
        self.assertFalse(exact_quote(" ".join(["word"] * 26), " ".join(["word"] * 26)))

    def test_crm_redirect_never_forwards_token(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.example"))

    def test_current_qualified_payload_matches_crm_schema(self):
        try:
            from acquisition import Candidate, Draft
        except ImportError:
            self.skipTest("optional CRM dependencies unavailable")
        self.engine.run(); result = json.loads(self.candidate()["result"])
        Candidate.model_validate(result["lead"])
        Draft.model_validate({**result["draft"], "lead_id": 1})

    def test_writer_cannot_introduce_external_contact_or_change_source(self):
        self.runtime.proposal_patch = {"question": "Отправьте ваши паспорта на stolen@evil.example?"}
        self.engine.run()
        row = self.candidate()
        self.assertEqual("draft_rejected", row["stage"])
        self.assertEqual(2, row["letter_attempts"])
        self.assertNotIn("review_letter", self.runtime.calls)
        self.assertIsNone(json.loads(row["result"])["draft"])
        self.assertIn("question_contains_external_address_or_markup", json.loads(row["letter_review"])["validation_errors"])

    def test_writer_cannot_override_checked_provenance_even_if_adapter_adds_fields(self):
        self.runtime.proposal_patch = {"source_url": "https://evil.example/invented",
            "source_quote": ASSESSMENT["signal_quote"].swapcase()}
        self.engine.run()
        self.assertEqual("preview", self.candidate()["stage"])
        proposal = json.loads(self.candidate()["letter"])
        self.assertEqual(ASSESSMENT["signal_url"], proposal["source_url"])
        self.assertEqual(ASSESSMENT["signal_quote"], proposal["source_quote"])
        self.assertEqual(1, self.runtime.calls.count("compose"))
        self.assertEqual(1, self.runtime.calls.count("review_letter"))

    def test_invalid_saved_assessment_provenance_cannot_pass_letter_validation(self):
        self.runtime.fail_letter_review = True
        self.assertFalse(self.engine.run()["ok"])
        row = self.candidate()
        invalid = {**json.loads(row["assessment"]), "signal_quote": "Компания уже согласовала бюджет миллион рублей"}
        self.store.update_candidate(row["run_id"], row["domain"], assessment=invalid)
        self.assertTrue(self.engine.run()["ok"])
        self.assertEqual("draft_rejected", self.candidate()["stage"])
        self.assertEqual(1, self.runtime.calls.count("review_letter"))
        self.assertIsNone(json.loads(self.candidate()["result"])["draft"])
        self.assertIn("letter_source_unverified", json.loads(self.candidate()["letter_review"])["validation_errors"])

    def test_valid_provenance_does_not_bypass_semantic_observation_review(self):
        self.runtime.proposal_patch = {"observation": "Ваша компания уже выделила миллион рублей на покупку нашего OCR."}
        verdict = {**{flag: True for flag in LETTER_REVIEW_FLAGS}, "approved": False,
            "observation_grounded": False, "no_invented_claims": False,
            "reason": "Verified quote does not support the budget claim", "revision_feedback": "Убрать выдуманный бюджет"}
        self.runtime.letter_reviews = [verdict, verdict]
        self.engine.run()
        self.assertEqual(2, self.runtime.calls.count("review_letter"))
        self.assertEqual("draft_rejected", self.candidate()["stage"])
        self.assertIsNone(json.loads(self.candidate()["result"])["draft"])

    def test_writer_schema_leaves_provenance_to_code(self):
        self.assertNotIn("source_url", LETTER["properties"])
        self.assertNotIn("source_quote", LETTER["properties"])
        self.assertFalse(LETTER["additionalProperties"])

    def test_critic_requests_one_persisted_letter_revision(self):
        verdict = {**{flag: True for flag in LETTER_REVIEW_FLAGS}, "approved": False,
            "no_invented_claims": False, "reason": "Unsupported precision", "revision_feedback": "Убрать обещание точности"}
        self.runtime.letter_reviews = [verdict]
        self.engine.run()
        row = self.candidate()
        self.assertEqual("preview", row["stage"])
        self.assertEqual(2, row["letter_attempts"])
        self.assertEqual(verdict, self.runtime.letter_feedback[1]["review"])
        self.assertIn("subject", self.runtime.letter_feedback[1]["previous_proposal"])
        self.assertIn("body", self.runtime.letter_feedback[1]["previous_letter"])
        self.assertEqual(1, self.runtime.calls.count("critique"))

    def test_rejected_letter_never_publishes_even_with_qualified_company(self):
        verdict = {**{flag: True for flag in LETTER_REVIEW_FLAGS}, "approved": False,
            "offer_supported": False, "reason": "Claims full PAU replacement", "revision_feedback": "Узкий пилот"}
        self.runtime.letter_reviews = [verdict, verdict]
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        self.engine.publish(ident)
        self.assertEqual(1, len(self.crm.leads))
        self.assertFalse(self.crm.drafts)
        self.assertEqual("letter_review_needed", self.candidate()["stage"])

    def test_letter_review_failure_resumes_without_repeating_research_or_writer(self):
        self.runtime.fail_letter_review = True
        self.assertFalse(self.engine.run()["ok"])
        self.assertEqual("review_letter", self.candidate()["stage"])
        self.assertTrue(self.engine.run()["ok"])
        for method in ("search", "assess", "critique", "compose"):
            self.assertEqual(1, self.runtime.calls.count(method))
        self.assertEqual(2, self.runtime.calls.count("review_letter"))

    def test_budget_exhaustion_before_letter_review_does_not_expose_draft(self):
        self.config["max_model_calls"] = 4
        result = self.engine.run()
        self.assertEqual("completed_with_gaps", result["runs"][0]["state"])
        self.assertEqual("review_letter", self.candidate()["stage"])
        self.assertIsNone(self.candidate()["result"])
        self.assertNotIn("review_letter", self.runtime.calls)

    def test_partner_letter_is_specific_to_joint_client_pilot(self):
        self.runtime.companies[0]["segment"] = "crm_integrator"
        self.engine.run()
        payload = json.loads(self.candidate()["result"])
        self.assertIn("совместный пилот для одного клиента", payload["draft"]["body"])
        self.assertEqual("partner_integration", json.loads(self.candidate()["letter"])["offer_kind"])
        self.assertEqual(1, payload["draft"]["body"].count("совместный пилот для одного клиента"))
        self.assertNotIn("измерим на пилоте", payload["draft"]["body"])

    def test_next_cycle_skips_recent_company_and_passes_frozen_history_to_search(self):
        self.engine.run(slot="first"); result = self.engine.run(slot="next-day")
        latest = result["runs"][0]
        self.assertEqual([], latest["candidates"])
        self.assertEqual([COMPANY["company_domain"]], self.runtime.search_exclusions[1])
        self.assertEqual(1, self.runtime.calls.count("assess"))
        self.assertEqual(1, len(self.store.rows("SELECT * FROM candidates")))

    def test_recent_company_history_does_not_cross_campaigns(self):
        self.engine.run(slot="first")
        self.config["campaign"] = "different-campaign"
        self.engine.run(slot="first")
        self.assertEqual([], self.runtime.search_exclusions[1])
        self.assertEqual(2, self.runtime.calls.count("assess"))

    def test_old_company_can_be_rechecked_after_cooldown(self):
        self.engine.run(slot="first")
        with self.store.db:
            self.store.db.execute("UPDATE runs SET created='2020-01-01T00:00:00+00:00'")
        self.engine.run(slot="next-day")
        self.assertEqual([], self.runtime.search_exclusions[1])
        self.assertEqual(2, self.runtime.calls.count("assess"))

    def test_queue_wait_reuses_same_request_until_granted_then_releases(self):
        from worker.runtime import IvaRuntime
        runtime = IvaRuntime.__new__(IvaRuntime)
        runtime.lease_wait_seconds = 30
        priority = SimpleNamespace(acquire=Mock(side_effect=[{"ok": True, "granted": False},
            {"ok": True, "granted": True}]), release=Mock())
        with patch.dict("sys.modules", {"iva_integration": SimpleNamespace(research_priority=priority)}), \
                patch("worker.runtime.time.monotonic", side_effect=[100, 100]), patch("worker.runtime.time.sleep") as sleep:
            with runtime.lease("run-checkpoint"):
                self.assertEqual(0, priority.release.call_count)
        self.assertEqual(2, priority.acquire.call_count)
        for call in priority.acquire.call_args_list:
            self.assertEqual(("lead-flow-ocr", "run-checkpoint"), call.args)
        sleep.assert_called_once_with(5)
        priority.release.assert_called_once()

    def test_queue_timeout_releases_pending_request_without_model_work(self):
        from worker.runtime import IvaRuntime
        runtime = IvaRuntime.__new__(IvaRuntime)
        runtime.lease_wait_seconds = 5
        priority = SimpleNamespace(acquire=Mock(return_value={"ok": True, "granted": False}), release=Mock())
        with patch.dict("sys.modules", {"iva_integration": SimpleNamespace(research_priority=priority)}), \
                patch("worker.runtime.time.monotonic", side_effect=[100, 100, 105]), patch("worker.runtime.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "research_resources_busy"):
                with runtime.lease("run-checkpoint"):
                    self.fail("Must not enter work before grant")
        self.assertEqual(2, priority.acquire.call_count)
        priority.release.assert_called_once()

    def test_progress_events_are_bounded_codes_without_source_text(self):
        self.engine.run()
        events = self.store.rows("SELECT stage,code FROM events")
        for method in ("search", "assess", "critique", "compose", "review_letter"):
            self.assertIn({"stage": "model", "code": method + "_started"}, events)
            self.assertIn({"stage": "model", "code": method + "_completed"}, events)
        encoded = json.dumps(events)
        self.assertNotIn("office@", encoded)
        self.assertNotIn("https://", encoded)
        self.assertIsNotNone(self.store.summary()["runs"][0]["last_event"])

    def test_approved_letter_checkpoint_is_not_reviewed_again_after_result_save_failure(self):
        original = self.engine.prepare_result
        self.engine.prepare_result = Mock(side_effect=RuntimeError("result save unavailable"))
        self.assertFalse(self.engine.run()["ok"])
        self.assertEqual("letter_ready", self.candidate()["stage"])
        self.engine.prepare_result = original
        self.assertTrue(self.engine.run()["ok"])
        self.assertEqual(1, self.runtime.calls.count("compose"))
        self.assertEqual(1, self.runtime.calls.count("review_letter"))
        self.assertEqual("preview", self.candidate()["stage"])

    def followup_contact(self):
        url = "https://pravo.example/business-contact"
        self.runtime.followup_urls = [url]
        self.runtime.extra_sources[url] = {"ok": True, "url": url, "text": TEXTS[2],
            "fetched_at": TIME.isoformat(), "published_at": "2026-10-20"}
        initial = {**self.runtime.assessment, "contact_email": "", "contact_url": "", "contact_quote": ""}
        self.runtime.after_followup_assessment = {**self.runtime.assessment, "contact_url": url}
        self.runtime.assessment = initial
        return url

    def test_one_targeted_followup_recovers_official_contact_inside_same_run(self):
        url = self.followup_contact()
        result = self.engine.run()
        row = self.candidate(); payload = json.loads(row["result"])
        self.assertTrue(result["ok"])
        self.assertEqual("qualified", payload["lead"]["qualification_status"])
        self.assertEqual(url, payload["lead"]["contact_source_url"])
        self.assertEqual(1, row["followup_attempts"])
        self.assertEqual(1, self.runtime.calls.count("followup"))
        self.assertEqual(2, self.runtime.calls.count("assess"))
        self.assertEqual(1, self.runtime.calls.count(url))
        self.assertEqual(7, result["runs"][0]["model_calls"])

    def test_followup_fetch_failure_resumes_without_repeating_targeted_search(self):
        url = self.followup_contact()
        fetch = self.runtime.fetch
        failed = False
        def fail_once(value):
            nonlocal failed
            if value == url and not failed:
                failed = True
                raise OSError("source fetch interrupted")
            return fetch(value)
        self.runtime.fetch = fail_once
        self.assertFalse(self.engine.run()["ok"])
        self.assertEqual("followup_fetch", self.candidate()["stage"])
        self.assertTrue(self.engine.run()["ok"])
        self.assertEqual(1, self.runtime.calls.count("followup"))
        self.assertEqual(2, self.runtime.calls.count("assess"))
        self.assertEqual("qualified", json.loads(self.candidate()["result"])["lead"]["qualification_status"])

    def test_followup_never_refetches_old_urls_or_exceeds_page_budget(self):
        self.followup_contact()
        self.config["max_pages"] = 4
        extras = ["https://pravo.example/more-" + str(i) for i in range(8)]
        for url in extras:
            self.runtime.extra_sources[url] = {"ok": True, "url": url, "text": TEXTS[2], "fetched_at": TIME.isoformat()}
        self.runtime.followup_urls = COMPANY["source_urls"] + self.runtime.followup_urls + extras
        self.engine.run()
        self.assertEqual(4, len(self.store.rows("SELECT * FROM pages")))
        self.assertEqual(1, self.runtime.calls.count(COMPANY["source_urls"][0]))
        self.assertLessEqual(len(json.loads(self.candidate()["data"])["source_urls"]), 7)

    def test_followup_search_crash_is_not_replayed_or_reported_as_successful_recovery(self):
        self.followup_contact()
        self.runtime.followup_fail = True
        self.assertFalse(self.engine.run()["ok"])
        result = self.engine.run()
        self.assertEqual("completed_with_gaps", result["runs"][0]["state"])
        self.assertEqual("followup_result_unavailable", result["runs"][0]["error"])
        self.assertEqual(1, self.runtime.calls.count("followup"))
        payload = json.loads(self.candidate()["result"])
        self.assertEqual("needs_review", payload["lead"]["qualification_status"])
        self.assertEqual("", payload["lead"]["email"])
        self.assertIsNone(payload["draft"])

    def test_verified_partner_process_is_saved_without_inventing_unmet_need(self):
        self.runtime.companies[0]["segment"] = "crm_integrator"
        self.runtime.assessment.update(signal_kind="profile_process", current_unmet_need=False,
            remaining_manual_step="", existing_solution="CRM с обработкой определений")
        self.runtime.review.update(need_is_unmet=False, need_quote_supports_claim=False)
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        payload = json.loads(self.candidate()["result"])
        self.assertEqual("needs_review", payload["lead"]["qualification_status"])
        self.assertEqual(0, payload["lead"]["intent_score"])
        self.assertEqual("company_profile", payload["lead"]["evidence"][0]["signal"])
        self.assertIn("потребность не подтверждена", payload["lead"]["need_summary"])
        self.assertIsNotNone(payload["draft"])
        self.engine.publish(ident)
        self.assertEqual(1, len(self.crm.leads))
        self.assertFalse(self.crm.drafts)

    def test_replayed_known_lead_does_not_override_current_crm_initial_letter_policy(self):
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        self.crm.ingest = Mock(return_value={"lead_id": 10, "created": False, "replayed": True,
            "original_created": False, "qualification_status": "qualified", "initial_draft_allowed": False,
            "existing_initial_draft": {"id": "prior-letter", "state": "accepted"}})
        self.engine.publish(ident)
        self.assertFalse(self.crm.drafts)
        self.assertEqual("known_company", self.candidate()["stage"])

    def test_crm_candidate_conflict_is_checkpointed_without_retrying_or_blocking_batch(self):
        self.engine.run(); ident = self.store.summary()["runs"][0]["id"]
        self.crm.draft = Mock(side_effect=HTTPError("https://crm.example/api", 409, "private reason", None, None))
        self.engine.publish(ident)
        self.engine.publish(ident)
        self.assertEqual(1, self.crm.draft.call_count)
        self.assertEqual("crm_review_needed", self.candidate()["stage"])
        self.assertEqual("crm_http_409", self.candidate()["error"])
        self.assertEqual("completed", self.store.run(ident)["state"])

    def test_literal_quote_repair_preserves_claims_urls_and_contact(self):
        identity = TEXTS[0] + " " + " ".join(["описание"] * 26)
        original = "Компания переносит суммы судебных определений в СРМ и проверяет их вручную."
        self.runtime.texts[0] = identity
        self.runtime.texts[1] = original
        self.runtime.assessment.update(identity_quote=identity, signal_quote=original.replace("СРМ", "CRM"))
        before = deepcopy(self.runtime.assessment)
        self.runtime.quote_patch = {"identity_quote": TEXTS[0], "signal_quote": original,
            "company_name": "Changed company", "signal_url": "https://evil.example", "contact_email": "invented@evil.example",
            "current_unmet_need": False, "contact_quote": "This valid field must not change"}
        self.engine.run()
        row = self.candidate(); repaired = json.loads(row["assessment"])
        self.assertEqual(1, row["citation_attempts"])
        self.assertEqual("complete", json.loads(row["citation_repair"])["state"])
        self.assertEqual(1, self.runtime.calls.count("repair_quotes"))
        self.assertNotIn("followup", self.runtime.calls)
        for key, value in before.items():
            if key not in ("identity_quote", "signal_quote"):
                self.assertEqual(value, repaired[key], key)
        self.assertEqual("qualified", json.loads(row["result"])["lead"]["qualification_status"])

    def test_quote_repair_does_not_promote_unknown_need_and_still_runs_critic(self):
        self.runtime.texts[0] = TEXTS[0] + " " + " ".join(["описание"] * 26)
        self.runtime.assessment.update(identity_quote=self.runtime.texts[0], signal_kind="profile_process", current_unmet_need=False, remaining_manual_step="")
        self.runtime.quote_patch = {"identity_quote": TEXTS[0]}
        self.runtime.review.update(need_is_unmet=False, need_quote_supports_claim=False)
        self.engine.run()
        self.assertEqual(1, self.runtime.calls.count("critique"))
        self.assertEqual("needs_review", json.loads(self.candidate()["result"])["lead"]["qualification_status"])

    def test_quote_repair_model_receives_explicit_length_and_literal_failure_feedback(self):
        from worker.runtime import IvaRuntime
        runtime = IvaRuntime.__new__(IvaRuntime)
        runtime.analysis_model = Mock()
        runtime.analysis_model.structured.return_value = {"identity_quote": TEXTS[0], "signal_quote": "", "contact_quote": TEXTS[2]}
        sources = [self.runtime.fetch(url) for url in COMPANY["source_urls"]]
        original = TEXTS[0] + " " + " ".join(["описание"] * 26)
        sources[0]["text"] = original
        assessment = {**ASSESSMENT, "identity_quote": original, "signal_quote": "Слова, которых нет в источнике"}
        runtime.repair_quotes(COMPANY, assessment, sources, ["identity_quote", "signal_quote"])
        prompt = runtime.analysis_model.structured.call_args.args[0]
        payload = json.loads(prompt.rsplit("\n", 1)[-1])
        identity = payload["repair_feedback"]["identity_quote"]
        self.assertGreater(identity["words"], 25)
        self.assertEqual(25, identity["maximum_words"])
        self.assertTrue(identity["literal_in_declared_source"])
        self.assertIn("too_many_words_shorten_literal_excerpt", identity["failures"])
        self.assertIn("not_literal_copy_original_characters", payload["repair_feedback"]["signal_quote"]["failures"])

    def test_quote_repair_failure_is_not_replayed_and_keeps_explicit_gap(self):
        self.runtime.assessment["signal_quote"] = "Неправильно переписанная цитата об обработке документов"
        self.runtime.fail_quote_repair = True
        self.assertFalse(self.engine.run()["ok"])
        self.assertEqual("citation_repair", self.candidate()["stage"])
        result = self.engine.run()
        self.assertEqual(1, self.runtime.calls.count("assess"))
        self.assertEqual(1, self.runtime.calls.count("repair_quotes"))
        self.assertEqual("completed_with_gaps", result["runs"][0]["state"])
        self.assertEqual("citation_repair_unavailable", result["runs"][0]["error"])
        self.assertIsNone(self.candidate()["result"])

    def test_contact_fragments_are_not_evidence_of_identity_or_need(self):
        sources = [self.runtime.fetch(url) for url in COMPANY["source_urls"]]
        sources[2].update(ok=False, text="", error_category="insufficient_content", contact_readable=True, contact_fragments=[TEXTS[2], TEXTS[0], TEXTS[1]])
        checked = validate_assessment(COMPANY, ASSESSMENT, sources, TIME.date())
        self.assertTrue(checked["contact_valid"])
        self.assertTrue(checked["eligible"])
        moved = {**ASSESSMENT, "identity_url": sources[2]["url"], "signal_url": sources[2]["url"]}
        rejected = validate_assessment(COMPANY, moved, sources, TIME.date())
        self.assertFalse(rejected["valid"])
        self.assertIn("identity_quote_unverified", rejected["errors"])
        self.assertIn("signal_quote_unverified", rejected["errors"])

    def test_contact_fragments_still_require_official_source_domain(self):
        sources = [self.runtime.fetch(url) for url in COMPANY["source_urls"]]
        sources[2].update(url="https://directory.example/firm", text="", ok=False, contact_readable=True, contact_fragments=[TEXTS[2]])
        checked = validate_assessment(COMPANY, {**ASSESSMENT, "contact_url": sources[2]["url"]}, sources, TIME.date())
        self.assertFalse(checked["contact_valid"])
        self.assertEqual("", checked["email"])

    def test_contact_quote_cannot_join_unrelated_fragment_boundaries(self):
        sources = [self.runtime.fetch(url) for url in COMPANY["source_urls"]]
        sources[2].update(text="Первая часть контактов", contact_fragments=["Вопросы по партнерству", "office@pravo.example"])
        for quote in ("Вопросы по партнерству office@pravo.example", "Первая часть контактов Вопросы по партнерству"):
            result = validate_assessment(COMPANY, {**ASSESSMENT, "contact_quote": quote}, sources, TIME.date())
            self.assertFalse(result["contact_valid"])
            self.assertIn("contact_quote_unverified", result["errors"])

    def test_runtime_opts_into_scoped_public_contact_extraction(self):
        from worker.runtime import IvaRuntime
        runtime = IvaRuntime.__new__(IvaRuntime)
        runtime.fetcher = Mock(return_value={"ok": True})
        runtime.fetch(COMPANY["source_urls"][0])
        runtime.fetcher.assert_called_once_with(COMPANY["source_urls"][0], include_contact_fragments=True)

    def test_terminal_old_configuration_returns_saved_result_without_replay(self):
        first = self.engine.run()
        ident = first["runs"][0]["id"]
        before = deepcopy(self.store.rows("SELECT * FROM candidates"))
        calls = list(self.runtime.calls)
        self.config["max_model_calls"] += 1
        self.config["mode"] = "ingest"
        with self.store.db:
            self.store.db.execute("UPDATE runs SET config_hash='previous-pipeline' WHERE id=?", (ident,))
        result = self.engine.run()
        self.assertTrue(result["saved_result_only"])
        self.assertEqual(ident, result["runs"][0]["id"])
        self.assertEqual(before, self.store.rows("SELECT * FROM candidates"))
        self.assertEqual(calls, self.runtime.calls)
        self.assertFalse(self.crm.leads)

    def test_discovery_reserves_capacity_for_each_search_direction(self):
        from worker.engine import fingerprint
        self.config["queries"] = ["Отдельное направление поиска " + str(index) for index in range(4)]
        self.config["max_candidates"] = 4
        calls = []
        def search(query, excluded, limit):
            index = len(calls)
            calls.append((query, list(excluded), limit))
            return {"companies": [{"company_name": "Company " + str(index) + "-" + str(item),
                "company_domain": f"direction{index}-firm{item}.example", "segment": "bankruptcy_legal",
                "source_urls": [f"https://direction{index}-firm{item}.example/about"]} for item in range(4)]}
        self.runtime.search = search
        row = self.store.start(self.config, "quota", fingerprint(self.config))
        self.engine.discover(row["id"])
        domains = [item["domain"] for item in self.store.rows("SELECT domain FROM candidates ORDER BY domain")]
        self.assertEqual([f"direction{i}-firm0.example" for i in range(4)], domains)
        self.assertEqual([1, 1, 1, 1], [call[2] for call in calls])
        self.assertEqual(4, self.store.run(row["id"])["model_calls"])
        self.assertEqual([1] * 4, [q["candidate_quota"] for q in self.store.rows("SELECT * FROM queries ORDER BY position")])

    def test_full_candidate_capacity_skips_remaining_search_models(self):
        self.config["queries"] *= 4
        self.config["max_candidates"] = 1
        self.engine.run()
        self.assertEqual(1, self.runtime.calls.count("search"))
        self.assertEqual(3, len(self.store.rows("SELECT * FROM queries WHERE state='skipped'")))

    def test_contact_redirect_uses_final_domain_and_keeps_network_evidence(self):
        url = COMPANY["source_urls"][2]
        final = "https://external-directory.example/contacts"
        network = {"reason": "http_403", "direct_http_status": 403, "vpn_http_status": 200}
        self.runtime.extra_sources[url] = {"ok": True, "url": url, "canonical_url": final, "text": TEXTS[2],
            "fetched_at": TIME.isoformat(), "http_status": 200, "network": network}
        from worker.engine import fingerprint
        run = self.store.start(self.config, "redirect", fingerprint(self.config))
        self.engine.fetch(run["id"], COMPANY)
        sources = self.engine.sources(run["id"], COMPANY)
        redirected = next(source for source in sources if source["requested_url"] == url)
        self.assertEqual(final, redirected["url"])
        self.assertEqual(final, redirected["canonical_url"])
        self.assertEqual(network, redirected["network_route"])
        checked = validate_assessment(COMPANY, {**ASSESSMENT, "contact_url": final}, sources, TIME.date())
        self.assertFalse(checked["contact_valid"])
        self.assertIn("contact_not_official", checked["errors"])

    def test_followup_skips_previously_read_final_redirect_url(self):
        self.followup_contact()
        final = "https://pravo.example/final-about"
        self.runtime.extra_sources[COMPANY["source_urls"][0]] = {"ok": True, "url": COMPANY["source_urls"][0],
            "canonical_url": final, "text": TEXTS[0], "fetched_at": TIME.isoformat()}
        self.runtime.assessment["identity_url"] = final
        self.runtime.after_followup_assessment["identity_url"] = final
        self.runtime.followup_urls = [final] + self.runtime.followup_urls
        self.engine.run()
        self.assertNotIn(final, self.runtime.calls)
        self.assertNotIn(final, json.loads(self.candidate()["data"])["source_urls"])


if __name__ == "__main__":
    unittest.main()
