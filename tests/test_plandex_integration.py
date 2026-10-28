"""Synthetic Plandex export -> existing stores -> existing HTML/report/pricing tests.

Run in a full ledger checkout; imports deliberately fail in a partial checkout.
No Plandex database, personal ledger, provider API or model execution is used.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_plandex as collector  # noqa: E402
import ledger_store  # noqa: E402
import scoped_report  # noqa: E402


class PlandexIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.rows = json.loads((ROOT / "tests/plandex_sources/usage-v1.json").read_text())["records"]
        self.source = self.root / "synthetic-export.jsonl"
        self.source.write_text("".join(json.dumps(row) + "\n" for row in self.rows))
        self.config = {"schema": "plandex-collector/1", "approved_project_scope": True,
                       "sources": [{"source_id": "synthetic-server", "project_ids": ["project-a", "project-b"],
                                    "versions": ["2.2.1+usage-export.1"], "files": [str(self.source)]}]}
        # Deliberately synthetic rates, not a claim about current provider prices.
        self.prices = self.root / "synthetic-pricing.json"
        self.prices.write_text(json.dumps({"_as_of": "2026-10-12", "models": {
            model: {"input": 2, "cache_read": .2, "cache_write_5m": 2.5, "cache_write_1h": 4,
                    "output": 10, "source": "synthetic-test-fixture", "verified_at": "2026-10-12",
                    "service_tier": "standard"} for model in ("gpt-4.1", "gpt-4.1-mini")}}))

    def run_backend(self, kind):
        checkpoint = self.root / ("checkpoint-" + kind)
        store_path = self.root / ("ledger-" + kind + (".sqlite" if kind == "sqlite" else ""))
        original = (self.source.read_bytes(), self.source.stat().st_mode, self.source.stat().st_mtime_ns)
        first = collector.collect(self.config, checkpoint, kind, store_path)
        self.assertEqual(first["ledger_ingest"], {"added": 2, "skipped": 0})
        self.assertEqual(first["unfinished_attempts"], 0)
        second = collector.collect(self.config, checkpoint, kind, store_path)
        self.assertEqual(second["ledger_ingest"], {"added": 0, "skipped": 2})
        self.assertEqual(second["scan"].get("parsed_lines", 0), 0)
        self.assertEqual(original, (self.source.read_bytes(), self.source.stat().st_mode, self.source.stat().st_mtime_ns))
        store = ledger_store.Store.open(kind, str(store_path))
        try:
            events = list(store.iter_events())
            self.assertEqual(len(events), 2)
            self.assertEqual(sum(e["total"] for e in events), 2200)
            self.assertEqual(sum(e["input_uncached"] for e in events), 1500)
            self.assertEqual(sum(e["cache_read"] for e in events), 400)
            self.assertEqual(sum(e["cache_write"] for e in events), 100)
            self.assertEqual(sum(e["output"] for e in events), 200)
            self.assertEqual(sum(e["reasoning"] for e in events), 60)
            self.assertEqual({e["provider"] for e in events}, {"openai", "openrouter"})
            self.assertEqual(len({e["plandex_plan_id"] for e in events}), 2)
            self.assertEqual(len({e["cwd"] for e in events}), 2)
            self.assertEqual(store.counts()["prompts"], 0)
            self.assertTrue(all(e.get("cost_usd") in (None, "") for e in events))
            native = [e["plandex_native_cost"] for e in events if e["plandex_native_cost"]]
            self.assertEqual(native[0]["amount"], "0.00123")
        finally:
            store.close()
        report_config = {"store": {"kind": kind, "path": str(store_path)}, "timezone": "UTC",
                         "workdir": str(self.root / ("reports-" + kind)), "pricing_path": str(self.prices)}
        out, data = scoped_report.generate_report(
            report_config, "2026-10-12", until="2026-10-12",
            now=datetime(2026, 10, 13, tzinfo=timezone.utc))
        self.assertEqual(data["totals"]["calls"], 2)
        self.assertEqual(data["totals"]["total"], 2200)
        self.assertAlmostEqual(data["totals"]["api_equivalent_usd"], .00533, places=12)
        self.assertIn("plandex", (out / "report.html").read_text().lower())
        for line in (out / "SHA256SUMS").read_text().splitlines():
            expected, name = line.split("  ")
            self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), expected)
        third = copy.deepcopy(self.rows[-1])
        third["attempt_id"] = "additional-attempt"
        third["native_request_id"] = "additional-native-request"
        with self.source.open("a") as fh:
            fh.write(json.dumps(third) + "\n")
        receipt = collector.collect(self.config, checkpoint, kind, store_path)
        self.assertEqual(receipt["ledger_ingest"], {"added": 1, "skipped": 2})
        self.assertEqual(receipt["scan"]["parsed_lines"], 1)
        self.assertEqual(receipt["events"], 3)
        # Rebuilding only the checkpoint must still deduplicate against the native store.
        rebuilt = collector.collect(self.config, self.root / ("rebuilt-" + kind), kind, store_path)
        self.assertEqual(rebuilt["ledger_ingest"], {"added": 0, "skipped": 3})
        out, data = scoped_report.generate_report(
            report_config, "2026-10-12", until="2026-10-12",
            now=datetime(2026, 10, 13, tzinfo=timezone.utc))
        self.assertEqual(data["totals"]["total"], 3300)
        self.assertAlmostEqual(data["totals"]["api_equivalent_usd"], .007995, places=12)

    def test_sqlite_store_report_and_incremental_replay(self):
        with mock.patch("socket.socket", side_effect=AssertionError("tests must stay offline")):
            self.run_backend("sqlite")

    def test_json_store_report_and_incremental_replay(self):
        with mock.patch("socket.socket", side_effect=AssertionError("tests must stay offline")):
            self.run_backend("json")

    def test_csv_store_report_and_incremental_replay(self):
        with mock.patch("socket.socket", side_effect=AssertionError("tests must stay offline")):
            self.run_backend("csv")

    def test_unpriced_model_is_not_free_and_native_cost_does_not_override(self):
        row = copy.deepcopy(self.rows[-1])
        row["model"] = "unpriced-synthetic-model"
        self.source.write_text(json.dumps(row) + "\n")
        output = self.root / "collected"
        collector.collect(self.config, output)
        events = [json.loads(line) for line in (output / "events.plandex.jsonl").read_text().splitlines()]
        now = datetime(2026, 10, 13, tzinfo=timezone.utc)
        scope = scoped_report.parse_scope("2026-10-12", "2026-10-12", "UTC", now)
        data, selected = scoped_report.collect(events, scope, json.loads(self.prices.read_text()), now)
        self.assertEqual(data["totals"]["unpriced_calls"], 1)
        self.assertEqual(data["totals"]["unpriced_tokens"], 1100)
        self.assertIsNone(selected[0]["api_equivalent_usd"])


if __name__ == "__main__":
    unittest.main()
