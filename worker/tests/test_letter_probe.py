from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from worker.engine import Engine
from worker.probes.letter_draft import run_probe
from worker.store import Store
from worker.tests.test_research import FakeRuntime


class LetterProbeTests(unittest.TestCase):
    def test_letter_only_probe_keeps_unknown_need_and_never_requalifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite"
            config = {"campaign": "source", "database": str(source), "iva_root": str(Path(tmp) / "unused-iva"),
                "queries": ["Source research for the isolated letter replay"], "max_candidates": 1, "max_pages": 12,
                "max_model_calls": 12, "max_signal_age_days": 180, "timezone": "UTC", "enabled": False, "mode": "preview"}
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = FakeRuntime()
            runtime.companies[0]["segment"] = "crm_integrator"
            runtime.assessment.update(signal_kind="profile_process", current_unmet_need=False, remaining_manual_step="")
            runtime.review.update(need_is_unmet=False, need_quote_supports_claim=False)
            store = Store(source)
            try:
                outcome = Engine(config, store, runtime, clock=lambda: datetime(2026, 10, 26, tzinfo=timezone.utc)).run()
                ident = outcome["runs"][0]["id"]
                self.assertEqual("needs_review", json.loads(store.rows("SELECT result FROM candidates")[0]["result"])["lead"]["qualification_status"])
            finally:
                store.close()
            probe = FakeRuntime()
            probe.forbidden_calls = []
            for method in ("search", "fetch", "followup", "assess", "critique", "repair_quotes"):
                setattr(probe, method, Mock(side_effect=AssertionError("Only writing and review are allowed")))
            output = Path(tmp) / "isolated.sqlite"
            with patch("worker.probes.letter_draft.CachedLetterRuntime", return_value=probe):
                result = run_probe(str(config_path), str(source), ident, "pravo.example", str(output))
                self.assertTrue(result["passed"])
                self.assertEqual(2, result["model_calls"])
                self.assertEqual(1, result["writer_attempts"])
                self.assertTrue(result["source_snapshot_unchanged"])
                self.assertEqual("needs_review", result["result"]["lead"]["qualification_status"])
                for method in ("search", "fetch", "followup", "assess", "critique", "repair_quotes"):
                    getattr(probe, method).assert_not_called()
                with self.assertRaisesRegex(ValueError, "probe_output_already_exists"):
                    run_probe(str(config_path), str(source), ident, "pravo.example", str(output))


if __name__ == "__main__":
    unittest.main()
