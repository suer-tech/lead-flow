"""Bounded discovery -> fetched evidence -> independent qualification -> drafts."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from .contracts import accepted_review, accepted_research_review, accepted_letter_review, compact, domain, make_draft, public_url, source_readable, stable_key, validate_assessment, validate_letter
from .store import dumps, now


def load_config(path):
    value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    required = {"campaign", "database", "iva_root", "queries"}
    if not required.issubset(value):
        raise ValueError("incomplete_config")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,70}", value["campaign"]):
        raise ValueError("invalid_campaign")
    for key, default, maximum in (("max_candidates", 12, 30), ("max_pages", 48, 72),
        ("max_model_calls", 32, 70), ("max_signal_age_days", 180, 365), ("company_recheck_days", 30, 180)):
        value.setdefault(key, default)
        if type(value[key]) is not int or not 1 <= value[key] <= maximum:
            raise ValueError("invalid_" + key)
    queries = value["queries"]
    if not isinstance(queries, list) or not 1 <= len(queries) <= 8 or any(not isinstance(q, str) or not 10 <= len(q) <= 500 for q in queries):
        raise ValueError("invalid_queries")
    value.setdefault("enabled", False)
    value.setdefault("mode", "preview")
    value.setdefault("timezone", "Asia/Yekaterinburg")
    value.setdefault("lease_wait_seconds", 300)
    if type(value["lease_wait_seconds"]) is not int or not 0 <= value["lease_wait_seconds"] <= 600:
        raise ValueError("invalid_lease_wait_seconds")
    if type(value["enabled"]) is not bool or value["mode"] not in ("preview", "ingest"):
        raise ValueError("invalid_execution_mode")
    ZoneInfo(value["timezone"])
    if value.get("crm_url") and not public_url(value["crm_url"]).startswith("https://"):
        raise ValueError("crm_requires_https")
    from .crm_transport import validate_config
    validate_config(value)
    return value


def fingerprint(config):
    keys = ("campaign", "queries", "max_candidates", "max_pages", "max_model_calls", "max_signal_age_days")
    value = {key: config[key] for key in keys}
    value["pipeline_contract"] = 4
    value["company_recheck_days"] = config.get("company_recheck_days", 30)
    root = Path(__file__).resolve().parents[1] / "research"
    value["brief_versions"] = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ("product-brief.json", "competitor-baseline.json", "research-policy.json", "customer-review-evidence.json") if (root / name).is_file()}
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class Engine:
    def __init__(self, config, store, runtime, crm=None, clock=None):
        self.config, self.store, self.runtime, self.crm = config, store, runtime, crm
        self.clock = clock or (lambda: datetime.now(ZoneInfo(config["timezone"])))

    def model(self, run_id, method, *args):
        # Charge before starting: a crash during inference cannot reset the budget.
        self.store.reserve_model(run_id, self.config["max_model_calls"])
        self.store.event(run_id, "model", method + "_started")
        try:
            result = getattr(self.runtime, method)(*args)
        except Exception:
            self.store.event(run_id, "model", method + "_failed")
            raise
        self.store.event(run_id, "model", method + "_completed")
        return result

    def discover(self, run_id):
        history = json.loads(self.store.run(run_id)["excluded_domains"])
        for query in self.store.rows("SELECT * FROM queries WHERE run_id=? AND state NOT IN ('done','skipped') ORDER BY position", (run_id,)):
            known = [row["domain"] for row in self.store.rows("SELECT domain FROM candidates WHERE run_id=? ORDER BY domain", (run_id,))]
            capacity = self.config["max_candidates"] - len(known)
            if capacity <= 0:
                with self.store.db:
                    self.store.db.execute("UPDATE queries SET state='skipped',candidate_quota=0 WHERE run_id=? AND position=?", (run_id, query["position"]))
                self.store.event(run_id, "discovery", "query_skipped_capacity")
                continue
            if query["candidate_quota"] is None:
                remaining = self.store.db.execute("SELECT COUNT(*) FROM queries WHERE run_id=? AND state NOT IN ('done','skipped')", (run_id,)).fetchone()[0]
                quota = min(4, max(1, (capacity + remaining - 1) // remaining))
                excluded = list(dict.fromkeys([*known, *history]))[:100]
                with self.store.db:
                    self.store.db.execute("UPDATE queries SET candidate_quota=?,search_exclusions=? WHERE run_id=? AND position=?",
                        (quota, dumps(excluded), run_id, query["position"]))
            else:
                quota = query["candidate_quota"]
                excluded = json.loads(query["search_exclusions"] or "[]")
            if query["attempts"] >= 3:
                with self.store.db:
                    self.store.db.execute("UPDATE queries SET state='done' WHERE run_id=? AND position=?", (run_id, query["position"]))
                self.store.event(run_id, "discovery", "query_attempts_exhausted")
                continue
            with self.store.db:
                self.store.db.execute("UPDATE queries SET attempts=attempts+1 WHERE run_id=? AND position=?", (run_id, query["position"]))
            result = self.model(run_id, "search", query["query"], excluded, quota)
            accepted = []
            for raw in result.get("companies", [])[:4]:
                try:
                    host = domain(raw["company_domain"])
                    if any(host == item or host.endswith("." + item) for item in excluded):
                        self.store.event(run_id, "discovery", "recent_company_skipped")
                        continue
                    name = compact(raw["company_name"])
                    if not 2 <= len(name) <= 180 or raw["segment"] not in ("bankruptcy_legal", "crm_integrator"):
                        continue
                    urls = list(dict.fromkeys(public_url(u) for u in raw["source_urls"][:6]))
                    if not urls:
                        continue
                except (ValueError, TypeError, KeyError):
                    self.store.event(run_id, "discovery", "invalid_candidate_rejected")
                    continue
                candidate = {"company_name": name, "company_domain": host, "segment": raw["segment"], "source_urls": urls}
                if host in known or any(item["company_domain"] == host for item in accepted):
                    continue
                accepted.append(candidate)
                if len(accepted) >= min(quota, capacity):
                    break
            with self.store.db:
                self.store.db.executemany("INSERT OR IGNORE INTO candidates(run_id,domain,data) VALUES(?,?,?)",
                    [(run_id, item["company_domain"], dumps(item)) for item in accepted])
                self.store.db.execute("UPDATE queries SET state='done' WHERE run_id=? AND position=?", (run_id, query["position"]))

    def sources(self, run_id, candidate):
        rows = self.store.rows("SELECT data FROM pages WHERE run_id=? AND url IN (" + ",".join("?" for _ in candidate["source_urls"]) + ")",
            [run_id, *candidate["source_urls"]])
        return [json.loads(row["data"]) for row in rows]

    def fetch(self, run_id, candidate):
        for url in candidate["source_urls"]:
            if self.store.rows("SELECT url FROM pages WHERE run_id=? AND url=?", (run_id, url)):
                continue
            count = self.store.db.execute("SELECT COUNT(*) FROM pages WHERE run_id=?", (run_id,)).fetchone()[0]
            if count >= self.config["max_pages"]:
                self.store.event(run_id, "fetch", "page_budget_exhausted")
                break
            self.store.event(run_id, "fetch", "source_started")
            raw = self.runtime.fetch(url)
            source = {key: raw.get(key) for key in ("ok", "url", "title", "text", "published_at", "fetched_at", "error_category", "http_status", "network_route")}
            source["requested_url"] = url
            source["network_route"] = raw.get("network") or raw.get("network_route")
            source["text"] = str(source.get("text") or "")[:16000]
            fragments = raw.get("contact_fragments", [])
            source["contact_fragments"] = [item for item in fragments[:4] if isinstance(item, str) and len(item) <= 400] if isinstance(fragments, list) else []
            source["contact_readable"] = raw.get("contact_readable") is True
            source["contact_fragments_hash"] = raw.get("contact_fragments_hash")
            try:
                source["url"] = public_url(raw.get("canonical_url") or raw.get("url") or url)
                source["canonical_url"] = source["url"]
            except ValueError:
                source.update(url=url, ok=False, text="", contact_readable=False, contact_fragments=[], error_category="invalid_final_url")
            source["fetched_at"] = source.get("fetched_at") or now()
            source["content_hash"] = hashlib.sha256(dumps({"text": source["text"], "contact_fragments": source["contact_fragments"]}).encode()).hexdigest()
            with self.store.db:
                self.store.db.execute("INSERT OR IGNORE INTO pages(run_id,url,data) VALUES(?,?,?)", (run_id, url, dumps(source)))
            self.store.event(run_id, "fetch", "source_completed" if source.get("ok") else "source_unavailable")

    def qualify(self, run_id, row):
        candidate = json.loads(row["data"])
        host = row["domain"]
        if row["stage"] == "fetch":
            self.fetch(run_id, candidate)
            self.store.update_candidate(run_id, host, stage="assess")
        sources = self.sources(run_id, candidate)
        readable = [s for s in sources if source_readable(s, allow_contact=True)]
        current = self.store.rows("SELECT * FROM candidates WHERE run_id=? AND domain=?", (run_id, host))[0]
        if current["stage"] in ("assess", "assessment_checked", "citation_repair"):
            if current["stage"] == "assess":
                assessment = self.model(run_id, "assess", candidate, readable) if readable else {}
                for prefix in ("identity", "signal", "contact"):
                    try:
                        assessment[prefix + "_url"] = public_url(assessment.get(prefix + "_url", ""))
                    except (ValueError, TypeError):
                        pass  # The independent validator records missing/invalid URLs.
                checked = validate_assessment(candidate, assessment, readable, self.clock().date(), self.config["max_signal_age_days"]) if readable else {
                    "valid": False, "eligible": False, "errors": ["no_readable_sources"], "email": "", "contact_valid": False}
                self.store.update_candidate(run_id, host, assessment=assessment, validation=checked, stage="assessment_checked")
            else:
                assessment = json.loads(current["assessment"])
                checked = json.loads(current["validation"])
            assessment, checked = self.repair_citations(run_id, candidate, assessment, checked, readable)
            repairable = any(error.startswith(("identity_", "contact_", "email_", "signal_url_", "signal_quote_"))
                or error in ("no_readable_sources", "remaining_workflow_gap_unknown", "no_current_unmet_need") for error in checked["errors"])
            followup = repairable and current["followup_attempts"] == 0 and self.store.db.execute(
                "SELECT COUNT(*) FROM pages WHERE run_id=?", (run_id,)).fetchone()[0] < self.config["max_pages"]
            stage = "followup_search" if followup else "review" if checked["valid"] else "rejected"
            self.store.update_candidate(run_id, host, assessment=assessment, validation=checked,
                stage=stage, error=None if checked["valid"] else ",".join(checked["errors"]))
            if stage == "rejected":
                return
        else:
            assessment = json.loads(current["assessment"])
            checked = json.loads(current["validation"])
        current = self.store.rows("SELECT * FROM candidates WHERE run_id=? AND domain=?", (run_id, host))[0]
        if current["stage"] in ("followup_search", "followup_fetch"):
            self.followup(run_id, current)
            current = self.store.rows("SELECT * FROM candidates WHERE run_id=? AND domain=?", (run_id, host))[0]
            if current["stage"] == "rejected":
                return
            return self.qualify(run_id, current)
        if current["stage"] == "review":
            review = self.model(run_id, "critique", candidate, assessment, readable)
            if not accepted_research_review(review):
                self.store.update_candidate(run_id, host, stage="rejected", review=review, error="independent_review_rejected")
                return
            self.store.update_candidate(run_id, host, stage="write_letter", review=review)
        else:
            review = json.loads(current["review"])
        if not checked.get("contact_valid") or review.get("contact_official") is not True:
            checked = {**checked, "eligible": False, "email": "", "contact_valid": False}
            self.prepare_result(run_id, candidate, assessment, checked, review, readable, None)
            return
        proposal = self.prepare_letter(run_id, candidate, assessment, readable)
        if proposal is None:
            self.prepare_result(run_id, candidate, assessment, checked, review, readable, None, stage="draft_rejected")
            return
        self.prepare_result(run_id, candidate, assessment, checked, review, readable, proposal)

    def repair_citations(self, run_id, candidate, assessment, checked, sources):
        by_url = {source["url"]: source for source in sources if source_readable(source, allow_contact=True)}
        targets = [prefix + "_quote" for prefix in ("identity", "signal", "contact")
            if prefix + "_quote_unverified" in checked["errors"] and assessment.get(prefix + "_quote")
            and assessment.get(prefix + "_url") in by_url
            and source_readable(by_url[assessment[prefix + "_url"]], allow_contact=prefix == "contact")]
        if not targets:
            return assessment, checked
        host = candidate["company_domain"]
        row = self.store.rows("SELECT * FROM candidates WHERE run_id=? AND domain=?", (run_id, host))[0]
        if row["citation_attempts"]:
            record = json.loads(row["citation_repair"] or "{}")
            if record.get("state") == "pending":
                self.store.update_candidate(run_id, host, citation_repair={**record, "state": "unavailable"})
                self.store.event(run_id, "citation", "repair_result_unavailable")
            return assessment, checked
        selected_urls = {assessment[key.replace("_quote", "_url")] for key in targets}
        self.store.update_candidate(run_id, host, stage="citation_repair", citation_attempts=1,
            citation_repair={"state": "pending", "fields": targets})
        patch = self.model(run_id, "repair_quotes", candidate, assessment,
            [by_url[url] for url in sorted(selected_urls)], targets)
        # Apply only previously identified quotation fields. Even a malformed
        # model result cannot change the URL, identity, email or need verdict.
        repaired = {**assessment, **{key: patch[key] for key in targets if isinstance(patch.get(key), str)}}
        validation = validate_assessment(candidate, repaired, sources, self.clock().date(), self.config["max_signal_age_days"])
        remaining = [key for key in targets if key + "_unverified" in validation["errors"]]
        self.store.update_candidate(run_id, host, assessment=repaired, validation=validation, stage="assessment_checked",
            citation_repair={"state": "rejected" if remaining else "complete", "fields": targets, "remaining": remaining})
        self.store.event(run_id, "citation", "repair_rejected" if remaining else "repair_completed")
        return repaired, validation

    def followup(self, run_id, row):
        candidate = json.loads(row["data"])
        checked = json.loads(row["validation"])
        host = row["domain"]
        if row["stage"] == "followup_search":
            if row["followup_attempts"]:
                # A process died before recording the search result. Do not
                # replay a consumed branch in pursuit of a positive verdict.
                self.store.update_candidate(run_id, host, followup={"state": "unavailable", "errors": checked["errors"]},
                    stage="review" if checked["valid"] else "rejected", error="followup_result_unavailable")
                self.store.event(run_id, "followup", "search_result_unavailable")
                return
            self.store.update_candidate(run_id, host, followup_attempts=1,
                followup={"state": "searching", "errors": checked["errors"], "excluded_urls": candidate["source_urls"]})
            result = self.model(run_id, "followup", candidate, checked["errors"], self.sources(run_id, candidate))
            added = []
            already_attempted = {*candidate["source_urls"], *(source["url"] for source in self.sources(run_id, candidate))}
            for raw in result.get("source_urls", [])[:8]:
                try:
                    url = public_url(raw)
                except (ValueError, TypeError):
                    continue
                if url not in already_attempted and url not in added:
                    added.append(url)
                if len(added) >= 4 or len(candidate["source_urls"]) + len(added) >= 10:
                    break
            candidate = {**candidate, "source_urls": [*candidate["source_urls"], *added]}
            self.store.update_candidate(run_id, host, data=candidate, stage="followup_fetch" if added else "review" if checked["valid"] else "rejected",
                followup={"state": "found" if added else "no_new_sources", "errors": checked["errors"], "added_urls": added})
            self.store.event(run_id, "followup", "new_sources_found" if added else "no_new_sources")
            if not added:
                return
        self.fetch(run_id, candidate)
        self.store.update_candidate(run_id, host, stage="assess", followup={
            "state": "complete", "errors": checked["errors"], "added_urls": added if row["stage"] == "followup_search" else json.loads(row["followup"])["added_urls"]})

    def prepare_letter(self, run_id, candidate, assessment, sources):
        host = candidate["company_domain"]
        while True:
            row = self.store.rows("SELECT * FROM candidates WHERE run_id=? AND domain=?", (run_id, host))[0]
            if row["stage"] == "letter_ready":
                return json.loads(row["letter"])
            if row["stage"] == "write_letter":
                feedback = None
                if row["letter_review"]:
                    previous = json.loads(row["letter"]) if row["letter"] else None
                    rendered = make_draft(candidate, previous) if isinstance(previous, dict) and all(
                        isinstance(previous.get(key), str) for key in ("subject", "observation", "pilot_focus", "question")) else None
                    feedback = {"review": json.loads(row["letter_review"]), "previous_proposal": previous, "previous_letter": rendered}
                proposal = self.model(run_id, "compose", candidate, assessment, sources, feedback)
                attempts = row["letter_attempts"] + 1
            else:
                proposal = json.loads(row["letter"])
                attempts = row["letter_attempts"]
            # The author only writes prose. Keep the already reviewed evidence
            # verbatim, even if a legacy adapter supplies different provenance.
            proposal = {**proposal, "source_url": assessment.get("signal_url"), "source_quote": assessment.get("signal_quote")}
            errors = validate_letter(candidate, assessment, proposal, sources)
            stage = "review_letter" if not errors else "write_letter" if attempts < 2 else "draft_rejected"
            self.store.update_candidate(run_id, host, letter=proposal, letter_attempts=attempts,
                stage=stage, letter_review={"validation_errors": errors} if errors else None,
                error="letter_validation_rejected" if errors else None)
            if errors:
                self.store.event(run_id, "letter", "letter_validation_rejected")
                if stage == "draft_rejected":
                    return None
                continue
            # Both model calls are separate persisted checkpoints. A critic
            # failure resumes here without repeating discovery or the writer.
            verdict = self.model(run_id, "review_letter", candidate, assessment, sources, proposal)
            if accepted_letter_review(verdict):
                self.store.update_candidate(run_id, host, stage="letter_ready", letter_review=verdict, error=None)
                return proposal
            attempts = self.store.rows("SELECT letter_attempts FROM candidates WHERE run_id=? AND domain=?", (run_id, host))[0]["letter_attempts"]
            stage = "write_letter" if attempts < 2 else "draft_rejected"
            self.store.update_candidate(run_id, host, stage=stage, letter_review=verdict, error="letter_review_rejected")
            self.store.event(run_id, "letter", "letter_review_rejected")
            if stage == "draft_rejected":
                return None

    def prepare_result(self, run_id, candidate, assessment, checked, review, readable, proposal, stage="preview"):
        host = candidate["company_domain"]
        kind = assessment["signal_kind"]
        qualified = checked["eligible"] and accepted_review(review) and kind in ("explicit_need", "manual_work")
        signal_map = {"explicit_need": "explicit_request", "manual_work": "manual_work", "hiring": "hiring", "integration_project": "company_profile"}
        scores = {"explicit_need": 85, "manual_work": 50, "integration_project": 40, "hiring": 25}
        by_url = {s["url"]: s for s in readable}
        evidence = []
        for prefix, signal in (("signal", signal_map.get(kind, "company_profile")), ("identity", "company_profile"), ("contact", "company_profile")):
            if prefix == "contact" and not checked.get("contact_valid"):
                continue
            source = by_url[assessment[prefix + "_url"]]
            if prefix == "signal" and not qualified:
                signal = "hiring" if kind == "hiring" else "company_profile"
            evidence.append({"source_url": source["url"], "quote": compact(assessment[prefix + "_quote"]),
                "observed_at": source["fetched_at"], "published_at": source.get("published_at"), "signal": signal,
                "interpretation": (assessment["why_fit"] if qualified else "Наблюдаемый документный процесс; текущая потребность требует проверки") if prefix == "signal" else ("Официальная компания" if prefix == "identity" else "Опубликованный контакт")})
        unknowns = list(dict.fromkeys(assessment.get("unknowns", []) + review.get("unknowns", []) + checked["errors"] +
            ["Текущие инструменты: " + assessment.get("existing_solution", "неизвестно"),
             "Свидетельства бюджета: " + assessment.get("budget_evidence", "неизвестно"),
             "Готовность платить требует отдельной проверки", "Точность и экономия требуют пилота на документах клиента"]))
        key = stable_key(self.config["campaign"], host)
        # New research evidence may change tomorrow. Ingestion has a per-run key;
        # the CRM independently deduplicates company_domain and keeps owner edits.
        lead = {"idempotency_key": key + ":" + run_id, "company": candidate["company_name"], "company_domain": host,
            "segment": candidate["segment"], "document_types": [], "use_case": assessment["use_case"][:1500],
            "need_summary": assessment["why_fit"][:2000] if qualified else ("Текущая потребность не подтверждена. Наблюдаемый процесс: " + assessment["use_case"])[:2000],
            "qualification_status": "qualified" if qualified else "needs_review",
            "fit_score": 75 if qualified else 50, "intent_score": scores.get(kind, 0) if qualified else min(25, scores.get(kind, 0)), "email": checked["email"],
            "contact_name": "", "contact_role": "", "contact_source_url": assessment["contact_url"] if checked.get("contact_valid") else None,
            "evidence": evidence, "limitations": "; ".join(unknowns)[:3000]}
        draft = {"idempotency_key": key + ":initial", **make_draft(candidate, proposal),
            "contact_basis": "owner_review", "basis_evidence": "Черновик: публичный контакт и подтвержденный сигнал не равны разрешению на рассылку; требуется проверка основания обращения."} if proposal is not None else None
        self.store.update_candidate(run_id, host, stage=stage, review=review, result={"lead": lead, "draft": draft})

    def run(self, slot=None, scheduled=False):
        if scheduled and not self.config["enabled"]:
            return {"state": "disabled", "email_sending": False}
        with self.store.exclusive():
            row = self.store.start(self.config, slot or self.clock().date().isoformat(), fingerprint(self.config))
            run_id = row["id"]
            if row.get("saved_result_only"):
                return {"ok": True, "saved_result_only": True, "warning": "completed_run_uses_saved_configuration", **self.store.summary(run_id)}
            if row["state"] == "pending_publish":
                return self.publish_result(run_id)
            if row["state"] in ("completed", "completed_with_gaps"):
                if self.config["mode"] == "ingest":
                    return self.publish_result(run_id)
                return self.store.summary(run_id)
            try:
                self.store.state(run_id, "waiting_resources")
                self.store.event(run_id, "lease", "waiting_for_coordinator")
                with self.runtime.lease(run_id):
                    self.store.state(run_id, "running")
                    self.store.event(run_id, "lease", "coordinator_granted")
                    self.discover(run_id)
                    for candidate in self.store.rows("SELECT * FROM candidates WHERE run_id=? AND stage IN ('fetch','assess','assessment_checked','citation_repair','followup_search','followup_fetch','review','write_letter','review_letter','letter_ready') ORDER BY domain", (run_id,)):
                        self.qualify(run_id, candidate)
                    unresolved_followup = any(json.loads(row["followup"]).get("state") == "unavailable" for row in
                        self.store.rows("SELECT followup FROM candidates WHERE run_id=? AND followup IS NOT NULL", (run_id,)))
                    unresolved_citation = any(json.loads(row["citation_repair"]).get("state") == "unavailable" for row in
                        self.store.rows("SELECT citation_repair FROM candidates WHERE run_id=? AND citation_repair IS NOT NULL", (run_id,)))
                    self.store.finish_research(run_id, "completed_with_gaps" if unresolved_followup or unresolved_citation else "completed",
                        "followup_result_unavailable" if unresolved_followup else "citation_repair_unavailable" if unresolved_citation else None,
                        publish_pending=self.config["mode"] == "ingest")
            except Exception as exc:
                reason = str(exc) if str(exc) in ("research_resources_busy", "model_budget_exhausted") else type(exc).__name__
                state = "waiting_resources" if reason == "research_resources_busy" else "completed_with_gaps" if reason == "model_budget_exhausted" else "failed"
                if state == "completed_with_gaps":
                    self.store.finish_research(run_id, state, reason, publish_pending=self.config["mode"] == "ingest")
                else:
                    self.store.state(run_id, state, reason)
                self.store.event(run_id, "run", reason)
                if state == "completed_with_gaps" and self.config["mode"] == "ingest":
                    return self.publish_result(run_id)
                return {"ok": False, **self.store.summary(run_id)}
            if self.config["mode"] == "ingest":
                return self.publish_result(run_id)
            return {"ok": True, **self.store.summary(run_id)}

    def publish_result(self, run_id):
        try:
            self.publish(run_id)
        except Exception:
            return {"ok": False, **self.store.summary(run_id)}
        return {"ok": True, **self.store.summary(run_id)}

    def publish(self, run_id):
        if self.crm is None:
            raise ValueError("crm_agent_not_configured")
        run = self.store.run(run_id)
        outcome = run["research_state"] or run["state"]
        if outcome not in ("completed", "completed_with_gaps"):
            raise ValueError("research_not_complete")
        research_error = run["research_error"] if run["research_state"] else run["error"]
        self.store.finish_research(run_id, outcome, research_error, publish_pending=True)
        self.store.event(run_id, "publish", "crm_publish_started")
        try:
            self.publish_candidates(run_id)
        except Exception as exc:
            self.store.state(run_id, "pending_publish", type(exc).__name__)
            self.store.event(run_id, "publish", "crm_publish_failed")
            raise
        self.store.state(run_id, outcome, research_error)
        self.store.event(run_id, "publish", "crm_publish_completed")
        return self.store.summary(run_id)

    def publish_candidates(self, run_id):
        for row in self.store.rows("SELECT * FROM candidates WHERE run_id=? AND result IS NOT NULL ORDER BY domain", (run_id,)):
            if row["draft_ack"] or row["stage"] == "crm_review_needed":
                continue
            try:
                self.publish_candidate(run_id, row)
            except HTTPError as exc:
                if exc.code not in (409, 422):
                    raise
                self.store.update_candidate(run_id, row["domain"], stage="crm_review_needed", error="crm_http_" + str(exc.code))
                self.store.event(run_id, "publish", "candidate_http_" + str(exc.code))
                exc.close()

    def publish_candidate(self, run_id, row):
        payload = json.loads(row["result"])
        ack = json.loads(row["lead_ack"]) if row["lead_ack"] else self.crm.ingest(payload["lead"])
        if not isinstance(ack, dict):
            raise ValueError("invalid_crm_ack")
        if ack.get("suppressed") or ack.get("archived"):
            self.store.update_candidate(run_id, row["domain"], lead_ack=ack, stage="suppressed")
            return
        if type(ack.get("lead_id")) is not int or ack["lead_id"] < 1:
            raise ValueError("crm_lead_ack_missing")
        self.store.update_candidate(run_id, row["domain"], lead_ack=ack)
        if payload["lead"]["qualification_status"] != "qualified":
            self.store.update_candidate(run_id, row["domain"], stage="review_needed")
            return
        if payload.get("draft") is None:
            self.store.update_candidate(run_id, row["domain"], stage="letter_review_needed")
            return
        if type(ack.get("initial_draft_allowed")) is not bool:
            raise ValueError("crm_contract_missing_initial_draft_permission")
        if ack.get("qualification_status") != "qualified" or not ack["initial_draft_allowed"]:
            # An original/replayed ingestion is not permission to contact again.
            self.store.update_candidate(run_id, row["domain"], stage="known_company")
            return
        draft_ack = self.crm.draft({**payload["draft"], "lead_id": ack["lead_id"]})
        if not isinstance(draft_ack, dict) or not draft_ack.get("id"):
            raise ValueError("crm_draft_ack_missing")
        self.store.update_candidate(run_id, row["domain"], draft_ack=draft_ack, stage="published")
