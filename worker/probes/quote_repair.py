"""Replay one saved citation failure using cached pages and real analysis calls.

Run only after deployment, as iva, with an explicit NEW private output SQLite:
python -m worker.probes.quote_repair --config /etc/lead-flow/research.json \
  --source-run <completed-run-id> --domain myforce.ru \
  --output /opt/lead-flow/data/probes/myforce-quotes.sqlite3
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from ..contracts import citation_matches, domain
from ..engine import Engine, fingerprint, load_config
from ..runtime import IvaRuntime
from ..store import Store, dumps


def snapshot(path, run_id, host):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        row = db.execute("SELECT * FROM candidates WHERE run_id=? AND domain=?", (run_id, host)).fetchone()
        run = db.execute("SELECT state FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or not run or run["state"] not in ("completed", "completed_with_gaps"):
            raise ValueError("source_candidate_must_be_terminal")
        candidate = dict(row)
        data = json.loads(candidate["data"])
        urls = data["source_urls"]
        pages = [dict(item) for item in db.execute("SELECT url,data FROM pages WHERE run_id=? AND url IN ("
            + ",".join("?" for _ in urls) + ") ORDER BY url", [run_id, *urls])]
        if not candidate["assessment"] or not candidate["validation"] or not pages:
            raise ValueError("source_snapshot_incomplete")
        return {"candidate": candidate, "pages": pages}
    finally:
        db.close()


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


class CachedAnalysisRuntime(IvaRuntime):
    def __init__(self, config):
        super().__init__(config)
        self.forbidden_calls = []

    def search(self, *args):
        self.forbidden_calls.append("search")
        raise RuntimeError("cached_probe_forbids_search")

    def fetch(self, *args):
        self.forbidden_calls.append("fetch")
        raise RuntimeError("cached_probe_forbids_fetch")

    def followup(self, *args):
        self.forbidden_calls.append("followup")
        raise RuntimeError("cached_probe_forbids_followup")


def quote_checks(assessment, pages):
    sources = {json.loads(row["data"])["url"]: json.loads(row["data"]) for row in pages}
    result = {}
    for prefix in ("identity", "signal", "contact"):
        quote = assessment.get(prefix + "_quote", "")
        source = sources.get(assessment.get(prefix + "_url"), {})
        result[prefix] = {"characters": len(quote), "words": len(quote.split()),
            "literal_within_limit": citation_matches(quote, source, prefix)}
    return result


def run_probe(config_path, source_run, company_domain, output_path):
    config = load_config(config_path)
    source_path = Path(config["database"]).resolve()
    output = Path(output_path).resolve()
    if output == source_path or (output.exists() and os.path.samefile(output, source_path)):
        raise ValueError("probe_cannot_write_source_database")
    # A new path is deliberate: never reset an existing probe or production run.
    if output.exists():
        raise ValueError("probe_output_already_exists")
    host = domain(company_domain)
    before = snapshot(source_path, source_run, host)
    saved = before["candidate"]
    candidate = json.loads(saved["data"])
    assessment = json.loads(saved["assessment"])
    config.update(database=str(output), campaign="quote-probe-" + digest([source_run, host])[:16], enabled=False,
        mode="preview", queries=["Only reuse saved public-source evidence for the isolated citation replay"],
        max_candidates=1, max_pages=max(1, len(before["pages"])), max_model_calls=6)
    runtime = CachedAnalysisRuntime(config)
    store = Store(output)
    try:
        with store.exclusive():
            run = store.start(config, "isolated-cached-quote-replay", fingerprint(config))
            ident = run["id"]
            with store.db:
                store.db.execute("UPDATE queries SET state='done' WHERE run_id=?", (ident,))
                store.db.execute("""INSERT INTO candidates(run_id,domain,data,stage,assessment,validation,followup_attempts,followup)
                    VALUES(?,?,?,?,?,?,?,?)""", (ident, host, saved["data"], "citation_repair", saved["assessment"], saved["validation"],
                    1, dumps({"state": "cached_probe", "reason": "network discovery explicitly disabled"})))
                store.db.executemany("INSERT INTO pages(run_id,url,data) VALUES(?,?,?)",
                    [(ident, page["url"], page["data"]) for page in before["pages"]])
        outcome = Engine(config, store, runtime).run(slot="isolated-cached-quote-replay")
        after = store.rows("SELECT * FROM candidates WHERE run_id=? AND domain=?", (ident, host))[0]
        amended = json.loads(after["assessment"])
        excluded = {"identity_quote", "signal_quote", "contact_quote"}
        report = {"source_run": source_run, "probe_run": ident, "domain": host,
            "state": store.run(ident)["state"], "model_calls": store.run(ident)["model_calls"],
            "candidate_stage": after["stage"], "candidate_error": after["error"],
            "citation_repair": json.loads(after["citation_repair"]) if after["citation_repair"] else None,
            "before": quote_checks(assessment, before["pages"]), "after": quote_checks(amended, before["pages"]),
            "non_quote_fields_unchanged": all(amended.get(key) == value for key, value in assessment.items() if key not in excluded),
            "production_snapshot_unchanged": digest(before) == digest(snapshot(source_path, source_run, host)),
            "only_cached_sources": not runtime.forbidden_calls, "forbidden_calls": runtime.forbidden_calls,
            "research_review": json.loads(after["review"]) if after["review"] else None,
            "letter_review": json.loads(after["letter_review"]) if after["letter_review"] else None,
            "result": json.loads(after["result"]) if after["result"] else None,
            "email_sending": False, "crm_calls": 0, "outcome_ok": outcome.get("ok")}
        targets = (report["citation_repair"] or {}).get("fields", [])
        report["passed"] = bool(targets) and (report["citation_repair"] or {}).get("state") == "complete" and all(
            report["after"][key.replace("_quote", "")]["literal_within_limit"] for key in targets) and all(
            report[key] for key in ("production_snapshot_unchanged", "only_cached_sources", "non_quote_fields_unchanged")) and report["research_review"] is not None
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
    parser.add_argument("--source-run", required=True)
    parser.add_argument("--domain", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = run_probe(args.config, args.source_run, args.domain, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
