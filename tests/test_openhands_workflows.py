"""Synthetic integration tests against the REAL ledger storage/query/pricer/report.

Run in a full checkout. No personal configuration, SDK install, model call,
credential discovery, real log scan, network request or scheduler is used.
"""
import copy
import json
import os
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
import anonymize  # noqa: E402


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
    def test_anonymized_export_omits_malformed_provenance_without_mutating_store(self):
        malformed = [None, "private-scalar", {"private-key": "private-value"},
                     42, ["private-id", None], [{"private-key": "private-value"}], [""]]
        for kind in ("sqlite", "json", "csv"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "input.jsonl"
                events = []
                for i, value in enumerate(malformed):
                    event = dict(batch().events[0], request_id="request-%d" % i,
                                 observed_sessions=value, usage_ids=value)
                    events.append(event)
                source.write_text("".join(json.dumps(e) + "\n" for e in events))
                before = source.read_bytes()
                path = str(root / "store")
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(store.ingest_events_file(str(source)), (len(events), 0))
                    original = list(store.iter_events())
                finally:
                    store.close()
                home = root / "home"
                home.mkdir()
                (home / "config.json").write_text(json.dumps({
                    "store": {"kind": kind, "path": path},
                    "anonymize": {"salt": "synthetic-salt"}}))
                for fmt in ("json", "jsonl", "csv"):
                    target = root / ("anonymous." + fmt)
                    result = subprocess.run([sys.executable, str(ROOT / "scripts/ledger.py"),
                        "export", "--table", "events", "--format", fmt,
                        "--out", str(target), "--anonymize"], capture_output=True, text=True,
                        env=dict(os.environ, AI_USAGE_LEDGER_HOME=str(home)), check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("private-", target.read_text())
                    if fmt != "csv":
                        exported = (json.loads(target.read_text()) if fmt == "json" else
                                    [json.loads(line) for line in target.read_text().splitlines()])
                        self.assertEqual(len(exported), len(events))
                        for row in exported:
                            self.assertNotIn("observed_sessions", row)
                            self.assertNotIn("usage_ids", row)
                            self.assertEqual(row["total"], events[0]["total"])
                self.assertEqual(source.read_bytes(), before)
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(list(store.iter_events()), original)
                    self.assertEqual(store.ingest_events_file(str(source)), (0, len(events)))
                finally:
                    store.close()

    def test_anonymization_keeps_empty_and_absent_sdk_lists_compatible(self):
        an = anonymize.Anonymizer("synthetic-salt")
        event = batch().events[0]
        self.assertEqual(an.event(event)["usage_ids"], [])
        event["observed_sessions"] = []
        self.assertEqual(an.event(event)["observed_sessions"], [])
        for field in ("response_id", "request_namespace", "usage_ids", "observed_sessions"):
            event.pop(field)
        result = an.event(event)
        for field in ("response_id", "request_namespace", "usage_ids", "observed_sessions"):
            self.assertNotIn(field, result)
        self.assertEqual(result["total"], event["total"])

    def test_anonymized_exports_hide_sdk_identifiers_on_every_backend(self):
        from test_openhands_usage import completion, stats, token_record

        usage = oh.UsageBatch("private-host", namespace="private-namespace")
        for session in ("private-conversation", "private-fork"):
            usage.add_stats(stats(token_record("private-response")), conversation_id=session)
        usage.add_completion_log({"kind": "LLMCompletionLogEvent",
            "log_data": json.dumps(completion("private-response")),
            "usage_id": "private-slot"}, conversation_id="private-conversation",
            source="/private/source.json")
        original = copy.deepcopy(usage.events)
        an = anonymize.Anonymizer("synthetic-salt")
        expected = an.event(original[0])
        self.assertEqual(usage.events, original)
        self.assertEqual(expected, anonymize.Anonymizer("synthetic-salt").event(original[0]))
        other = anonymize.Anonymizer("other-salt").event(original[0])
        for field in ("response_id", "request_namespace", "observed_sessions", "usage_ids"):
            self.assertNotEqual(expected[field], other[field])
        self.assertIn(expected["session"], expected["observed_sessions"])
        self.assertEqual(len(set(expected["observed_sessions"])), 2)
        for field in (*oh.BUCKETS, "total", "ts", "model", "model_aliases",
                      "provider_usage_verified", "sdk_usage_origin", "usage_evidence",
                      "sdk_reported_cost_usd", "sdk_cost_evidence"):
            self.assertEqual(expected[field], original[0][field])
        self.assertNotIn("usage_source", expected)
        self.assertNotIn("private-", json.dumps(expected))

        for kind in ("sqlite", "json", "csv"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = oh.write_batch(usage, root / "input")
                before = source.read_bytes()
                path = str(root / "store")
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(store.ingest_events_file(str(source)), (1, 0))
                    rows = list(store.iter_events())
                finally:
                    store.close()
                home = root / "home"
                home.mkdir()
                (home / "config.json").write_text(json.dumps({
                    "store": {"kind": kind, "path": path},
                    "anonymize": {"salt": "synthetic-salt"}}))
                for fmt in ("json", "jsonl", "csv"):
                    target = root / ("anonymous." + fmt)
                    result = subprocess.run([sys.executable, str(ROOT / "scripts/ledger.py"),
                        "export", "--table", "events", "--format", fmt,
                        "--out", str(target), "--anonymize"], capture_output=True, text=True,
                        env=dict(os.environ, AI_USAGE_LEDGER_HOME=str(home)), check=False)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("private-", target.read_text())
                    self.assertNotIn("/private/", target.read_text())
                    if fmt != "csv":
                        exported = (json.loads(target.read_text())[0] if fmt == "json"
                                    else json.loads(target.read_text()))
                        self.assertEqual(exported, an.event(rows[0]))
                        for field in ("response_id", "request_namespace", "observed_sessions",
                                      "usage_ids", "total", "provider_usage_verified"):
                            self.assertEqual(exported[field], expected[field])
                self.assertEqual(source.read_bytes(), before)
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(list(store.iter_events()), rows)
                    self.assertEqual(store.ingest_events_file(str(source)), (0, 1))
                finally:
                    store.close()

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
