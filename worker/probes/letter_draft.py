"""An explicit cached replay of writing/review only; no discovery or qualification."""
import argparse
import json
import os
from pathlib import Path

from ..contracts import accepted_research_review, accepted_review, domain
from ..engine import Engine, fingerprint, load_config
from ..store import Store, dumps
from .quote_repair import CachedAnalysisRuntime, digest, snapshot


class CachedLetterRuntime(CachedAnalysisRuntime):
    def assess(self, *args):
        self.forbidden_calls.append("assess")
        raise RuntimeError("letter_probe_forbids_assessment")

    def critique(self, *args):
        self.forbidden_calls.append("critique")
        raise RuntimeError("letter_probe_forbids_requalification")

    def repair_quotes(self, *args):
        self.forbidden_calls.append("repair_quotes")
        raise RuntimeError("letter_probe_forbids_citation_repair")


def run_probe(config_path, source_database, source_run, company_domain, output_path):
    config = load_config(config_path)
    source = Path(source_database).resolve()
    output = Path(output_path).resolve()
    protected = {source, Path(config["database"]).resolve()}
    if any(output == path or (output.exists() and path.exists() and os.path.samefile(output, path)) for path in protected):
        raise ValueError("probe_cannot_write_source_database")
    if output.exists():
        raise ValueError("probe_output_already_exists")
    host = domain(company_domain)
    before = snapshot(source, source_run, host)
    saved = before["candidate"]
    review = json.loads(saved["review"] or "{}")
    validation = json.loads(saved["validation"] or "{}")
    assessment = json.loads(saved["assessment"])
    if not accepted_research_review(review) or not validation.get("contact_valid") or not review.get("contact_official"):
        raise ValueError("source_candidate_not_reviewed_for_local_letter")
    config.update(database=str(output), campaign="letter-probe-" + digest([source_run, host])[:16], enabled=False,
        mode="preview", queries=["Only reuse a reviewed candidate for isolated writing and letter review"],
        max_candidates=1, max_pages=max(1, len(before["pages"])), max_model_calls=4)
    runtime = CachedLetterRuntime(config)
    store = Store(output)
    try:
        with store.exclusive():
            run = store.start(config, "isolated-cached-letter-replay", fingerprint(config))
            ident = run["id"]
            with store.db:
                store.db.execute("UPDATE queries SET state='done' WHERE run_id=?", (ident,))
                store.db.execute("""INSERT INTO candidates(run_id,domain,data,stage,assessment,validation,review,
                    followup_attempts,followup,citation_attempts,citation_repair) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (ident, host, saved["data"], "write_letter", saved["assessment"], saved["validation"], saved["review"],
                    1, dumps({"state": "cached_probe"}), 1, saved.get("citation_repair")))
                store.db.executemany("INSERT INTO pages(run_id,url,data) VALUES(?,?,?)",
                    [(ident, page["url"], page["data"]) for page in before["pages"]])
        outcome = Engine(config, store, runtime).run(slot="isolated-cached-letter-replay")
        row = store.rows("SELECT * FROM candidates WHERE run_id=? AND domain=?", (ident, host))[0]
        result = json.loads(row["result"]) if row["result"] else None
        letter_review = json.loads(row["letter_review"]) if row["letter_review"] else None
        qualified = validation.get("eligible") and accepted_review(review) and assessment["signal_kind"] in ("explicit_need", "manual_work")
        report = {"source_run": source_run, "probe_run": ident, "domain": host,
            "state": store.run(ident)["state"], "model_calls": store.run(ident)["model_calls"],
            "candidate_stage": row["stage"], "candidate_error": row["error"], "writer_attempts": row["letter_attempts"],
            "source_snapshot_unchanged": digest(before) == digest(snapshot(source, source_run, host)),
            "assessment_unchanged": json.loads(row["assessment"]) == assessment,
            "research_review_unchanged": json.loads(row["review"]) == review,
            "only_letter_models": not runtime.forbidden_calls, "forbidden_calls": runtime.forbidden_calls,
            "qualification_not_promoted": bool(result) and result["lead"]["qualification_status"] == ("qualified" if qualified else "needs_review"),
            "letter_review": letter_review, "result": result, "email_sending": False, "crm_calls": 0, "outcome_ok": outcome.get("ok")}
        report["passed"] = bool(result and result.get("draft") and letter_review and letter_review.get("approved")) and all(
            report[key] for key in ("source_snapshot_unchanged", "assessment_unchanged", "research_review_unchanged", "only_letter_models", "qualification_not_promoted"))
        report_path = output.with_suffix(".report.json")
        with open(report_path, "x", encoding="utf-8") as stream:
            os.chmod(report_path, 0o600)
            json.dump(report, stream, ensure_ascii=False, indent=2)
        return report
    finally:
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--source-database", required=True)
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run_probe(args.config, args.source_database, args.source_run, args.domain, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
