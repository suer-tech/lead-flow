from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from worker.engine import Engine
from worker.probes.quote_repair import run_probe
from worker.store import Store
from worker.tests.test_research import FakeRuntime, TEXTS


class QuoteProbeTests(unittest.TestCase):
    def test_isolated_replay_preserves_source_and_never_searches_or_fetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.sqlite"
            config = {"campaign": "source", "database": str(source), "iva_root": str(Path(tmp) / "unused-iva"),
                "queries": ["Source research for an isolated replay"], "max_candidates": 1, "max_pages": 12,
                "max_model_calls": 12, "max_signal_age_days": 180, "timezone": "UTC", "enabled": False, "mode": "preview"}
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            runtime = FakeRuntime()
            runtime.texts[0] = TEXTS[0] + " " + " ".join(["описание"] * 26)
            runtime.assessment["identity_quote"] = runtime.texts[0]
            store = Store(source)
            try:
                result = Engine(config, store, runtime, clock=lambda: datetime(2026, 10, 26, tzinfo=timezone.utc)).run()
                ident = result["runs"][0]["id"]
                self.assertEqual("rejected", store.rows("SELECT stage FROM candidates")[0]["stage"])
            finally:
                store.close()
            probe_runtime = FakeRuntime()
            probe_runtime.quote_patch = {"identity_quote": TEXTS[0]}
            probe_runtime.forbidden_calls = []
            for method in ("search", "followup", "fetch"):
                setattr(probe_runtime, method, Mock(side_effect=AssertionError("No source network is allowed")))
            output = Path(tmp) / "isolated.sqlite"
            with patch("worker.probes.quote_repair.CachedAnalysisRuntime", return_value=probe_runtime):
                report = run_probe(str(config_path), ident, "pravo.example", str(output))
                self.assertTrue(report["passed"])
                self.assertEqual(4, report["model_calls"])
                self.assertEqual("preview", report["candidate_stage"])
                self.assertEqual(0, report["crm_calls"])
                for method in ("search", "followup", "fetch"):
                    getattr(probe_runtime, method).assert_not_called()
                with self.assertRaisesRegex(ValueError, "probe_output_already_exists"):
                    run_probe(str(config_path), ident, "pravo.example", str(output))
                with self.assertRaisesRegex(ValueError, "probe_cannot_write_source_database"):
                    run_probe(str(config_path), ident, "pravo.example", str(source))


if __name__ == "__main__":
    unittest.main()
