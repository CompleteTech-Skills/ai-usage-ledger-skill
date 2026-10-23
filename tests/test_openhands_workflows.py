"""Synthetic integration tests against the REAL ledger storage/query/pricer/report.

Run in a full checkout. No personal configuration, SDK install, model call,
credential discovery, real log scan, network request or scheduler is used.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import compile_ai_logs as compiler  # noqa: E402
import ledger_query  # noqa: E402
import ledger_store  # noqa: E402
import openhands_usage as oh  # noqa: E402
import scoped_report  # noqa: E402


PRICING = {"_as_of": "2024-06-15", "models": {
    "synthetic-model": {"input": 2, "cache_read": .2, "cache_write_5m": 2.5,
                        "cache_write_1h": 4, "output": 10,
                        "source": "https://example.org/synthetic-pricing",
                        "verified_at": "2024-06-15", "service_tier": "standard"}}}
NOW = datetime(2024, 6, 15, 18, tzinfo=timezone.utc)


def batch():
    result = oh.UsageBatch("synthetic-host")
    result.add_completion_log({
        "timestamp": "2024-06-15T12:00:00+00:00", "latency_sec": 1, "cost": 999,
        "response": {"id": "synthetic-response", "model": "provider/synthetic-model",
                     "object": "chat.completion", "usage": {
                         "prompt_tokens": 100, "completion_tokens": 20,
                         "prompt_tokens_details": {"cached_tokens": 30, "cache_creation_tokens": 10},
                         "completion_tokens_details": {"reasoning_tokens": 5}, "total_tokens": 120}}},
        conversation_id="synthetic-conversation")
    result.add_stats({"usage_to_metrics": {"condenser": {
        "model_name": "unknown-model", "token_usages": [{
            "model": "unknown-model", "response_id": "undated-response",
            "prompt_tokens": 50, "completion_tokens": 10,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "reasoning_tokens": 0}],
        "accumulated_token_usage": {"prompt_tokens": 50, "completion_tokens": 10}}}},
        conversation_id="synthetic-conversation")
    return result


class OpenHandsWorkflowTests(unittest.TestCase):
    def test_replay_reopen_query_export_and_scoped_report_on_every_backend(self):
        for kind in ("sqlite", "json", "csv"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = oh.write_batch(batch(), root / "input")
                path = str(root / ("store.sqlite" if kind == "sqlite" else "store"))
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(store.ingest_events_file(str(source)), (2, 0))
                    self.assertEqual(store.ingest_events_file(str(source)), (0, 2))
                finally:
                    store.close()
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(store.ingest_events_file(str(source)), (0, 2))
                    rows = list(store.iter_events())
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(sum(e["total"] for e in rows), 180)
                    observed = next(e for e in rows if e["response_id"] == "synthetic-response")
                    self.assertEqual(observed["sdk_reported_cost_usd"], 999)
                    self.assertEqual(observed["usage_evidence"], "provider_response_usage")
                    self.assertIsNone(observed.get("cost_usd"))
                    self.assertEqual(observed["usage_ids"], [])
                    exported = ledger_store.export_jsonl(store, str(root / "export"))
                    self.assertEqual(exported["events"][1], 2)
                    contents = Path(exported["events"][0]).read_text()
                    self.assertIn("sdk_reported_cost_usd", contents)
                finally:
                    store.close()
                db = ledger_query.open_db(kind, path)
                try:
                    cols, queried = ledger_query.run_query(db,
                        ledger_query.PRESETS["by-tool"][1].format(where="tool = ?"), [oh.TOOL])
                    totals = dict(zip(cols, queried[0]))
                    self.assertEqual(totals["calls"], 2)
                    self.assertEqual(totals["total"], 180)
                finally:
                    db.close()
                scope = scoped_report.parse_scope("2024-06-15", "2024-06-15", "UTC", NOW)
                report, selected = scoped_report.collect(rows, scope, PRICING, NOW)
                self.assertEqual(report["totals"]["calls"], 1)
                self.assertEqual(report["totals"]["total"], 120)
                self.assertEqual(report["limitations"]["records_with_unusable_timestamps"], 1)
                # 60 fresh, 30 cached, 10 cache-write, 20 output. SDK cost ignored.
                self.assertAlmostEqual(report["totals"]["api_equivalent_usd"], .000351)
                self.assertEqual(len(selected), 1)

    def test_existing_compiler_report_includes_undated_and_identical_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            usage = batch()
            twin = dict(usage.events[0], response_id="another-response", request_id="another-id")
            usage.events.append(twin)
            source = oh.write_batch(usage, root / "input")
            price_file = root / "prices.json"
            price_file.write_text(json.dumps(PRICING))
            output = root / "compiled"
            compiler.cmd_report(SimpleNamespace(out_dir=str(output), events=[str(source)],
                pricing=str(price_file), accounts=None, sessions=[], prompts=[], inventory=[]))
            summary = json.loads((output / "summary.json").read_text())
            self.assertEqual(summary["events"], 3)
            self.assertEqual(summary["total"]["total"], 300)
            self.assertTrue((output / "SUMMARY.md").is_file())
            self.assertEqual(summary["by_tool"][0]["tool"], oh.TOOL)

    def test_existing_store_and_query_clis_accept_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = oh.write_batch(batch(), root / "input")
            path = str(root / "store.sqlite")
            command = [sys.executable, str(ROOT / "scripts/ledger_store.py"), "--kind", "sqlite",
                       "--path", path, "ingest", str(source)]
            first = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("(2, 0)", first.stdout)
            second = subprocess.run(command, capture_output=True, text=True, check=False)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("(0, 2)", second.stdout)
            queried = subprocess.run([sys.executable, str(ROOT / "scripts/ledger_query.py"),
                "totals", "--tool", oh.TOOL, "--store-kind", "sqlite", "--store-path", path,
                "--format", "json"], capture_output=True, text=True, check=False)
            self.assertEqual(queried.returncode, 0, queried.stderr)
            self.assertEqual(json.loads(queried.stdout)[0]["total"], 180)

    def test_existing_pricer_uses_buckets_not_sdk_cost_or_reasoning_twice(self):
        observed = batch().events[0]
        cost, nocache, priced_as, assumed = compiler.price_event(observed, PRICING)
        self.assertAlmostEqual(cost, .000351)
        self.assertAlmostEqual(nocache, .0004)
        self.assertEqual(priced_as, "synthetic-model")
        self.assertIsNone(assumed)

    def test_unknown_model_remains_unpriced_not_free_in_scoped_report(self):
        observed = batch().events[0]
        observed["model"] = "unknown-model"
        scope = scoped_report.parse_scope("2024-06-15", "2024-06-15", "UTC", NOW)
        report, selected = scoped_report.collect([observed], scope, PRICING, NOW)
        self.assertEqual(report["totals"]["unpriced_calls"], 1)
        self.assertEqual(report["totals"]["unpriced_tokens"], 120)
        self.assertIsNone(selected[0]["api_equivalent_usd"])


if __name__ == "__main__":
    unittest.main()
