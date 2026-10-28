"""Offline, synthetic contract tests for the opt-in OpenHands importer."""
import copy
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import openhands_usage as oh  # noqa: E402


def token_record(response_id="r1", **updates):
    record = dict(model="openai/test-model", response_id=response_id,
                  prompt_tokens=100, completion_tokens=20, cache_read_tokens=30,
                  cache_write_tokens=10, reasoning_tokens=5, context_window=100000,
                  per_turn_token=120)
    record.update(updates)
    return record


def metrics(*records):
    return dict(model_name="wrong-current-model", token_usages=list(records),
                accumulated_token_usage={name: sum(r.get(name, 0) for r in records)
                                         for name in oh.TOKEN_FIELDS},
                response_latencies=[{"response_id": r.get("response_id")} for r in records],
                costs=[{"cost": 900, "timestamp": 1}], accumulated_cost=900)


def stats(*records, **other_groups):
    return {"usage_to_metrics": {"default": metrics(*records), **other_groups}}


def completion(response_id="r1", *, responses=False, **usage_updates):
    if responses:
        usage = dict(input_tokens=100, output_tokens=20, total_tokens=120,
                     input_tokens_details={"cached_tokens": 30},
                     output_tokens_details={"reasoning_tokens": 5})
    else:
        usage = dict(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                     prompt_tokens_details={"cached_tokens": 30, "cache_creation_tokens": 10},
                     completion_tokens_details={"reasoning_tokens": 5})
    usage.update(usage_updates)
    return dict(timestamp=1791806400.125, latency_sec=0.25, cost=0,
                messages=[{"role": "user", "content": "PRIVATE-PROMPT"}],
                kwargs={"api_key": "PRIVATE-KEY"},
                response=dict(id=response_id, model="openai/test-model", usage=usage,
                              object="response" if responses else "chat.completion",
                              choices=[{"message": {"content": "PRIVATE-RESPONSE"}}]))


class OpenHandsUsageTests(unittest.TestCase):
    def setUp(self):
        self.batch = oh.UsageBatch("fixture-host")

    def add_stats(self, payload):
        self.batch.add_stats(payload, conversation_id="c1")

    def add_log(self, payload):
        self.batch.add_completion_log(payload, conversation_id="c1")

    def test_native_metrics_bucket_math_and_no_fabricated_time_or_cost(self):
        self.add_stats(stats(token_record()))
        event, = self.batch.events
        self.assertEqual([event[k] for k in oh.BUCKETS], [60, 30, 10, 20, 5])
        self.assertEqual(event["total"], 120)
        self.assertEqual(event["model"], "openai/test-model")
        self.assertIsNone(event["ts"])
        self.assertNotIn("cost_usd", event)
        self.assertNotIn("sdk_reported_cost_usd", event)
        self.assertFalse(self.batch.inventory()["partial"])
        self.assertEqual(self.batch.inventory()["undated_events"], 1)

    def test_chat_log_and_responses_final_usage(self):
        self.add_log(completion())
        self.add_log(completion("r2", responses=True))
        self.assertEqual([e["total"] for e in self.batch.events], [120, 120])
        self.assertEqual(self.batch.events[1]["input_uncached"], 70)
        self.assertIn("cache_write_tokens", self.batch.events[1]["unreported_token_fields"])
        self.assertEqual(self.batch.events[0]["timestamp_source"], "sdk_completion_log")

    def test_repeated_growing_and_out_of_order_snapshots(self):
        first = token_record()
        second = token_record("r2", prompt_tokens=200)
        for payload in (stats(first), stats(first, second), stats(first), stats(first, second)):
            self.add_stats(payload)
        self.assertEqual(len(self.batch.events), 2)
        self.assertEqual(sum(e["total"] for e in self.batch.events), 340)
        self.assertFalse(self.batch.inventory()["partial"])

    def test_merged_delegates_are_deduplicated_and_models_are_per_record(self):
        first = token_record()
        second = token_record("r2", model="anthropic/another-model")
        payload = stats(first, condenser=metrics(second), delegated=metrics(first, second))
        self.add_stats(payload)
        self.assertEqual(len(self.batch.events), 2)
        self.assertEqual(self.batch.events[1]["model"], "anthropic/another-model")
        self.assertEqual(self.batch.events[0]["usage_ids"], ["default", "delegated"])

    def test_logs_and_stats_share_identity_and_enrich_only_before_ingest(self):
        self.add_stats(stats(token_record()))
        original_id = self.batch.events[0]["request_id"]
        self.add_log(completion())
        self.assertEqual(len(self.batch.events), 1)
        self.assertEqual(self.batch.events[0]["request_id"], original_id)
        self.assertIsNotNone(self.batch.events[0]["ts"])
        self.assertEqual(self.batch.events[0]["usage_ids"], ["default"])
        self.assertEqual(self.batch.events[0]["usage_evidence"], "provider_response_usage")

    def test_same_request_in_fork_keeps_both_observed_sessions(self):
        self.add_stats(stats(token_record()))
        self.batch.add_stats(stats(token_record()), conversation_id="fork")
        self.assertEqual(len(self.batch.events), 1)
        self.assertEqual(self.batch.events[0]["observed_sessions"], ["c1", "fork"])

    def test_conflicting_request_id_is_fatal_not_silently_skipped(self):
        self.add_stats(stats(token_record()))
        with self.assertRaisesRegex(oh.UsageError, "conflicting_response_usage"):
            self.add_stats(stats(token_record(prompt_tokens=101)))

    def test_identical_counts_on_different_requests_are_not_collapsed(self):
        self.add_stats(stats(token_record(), token_record("r2")))
        self.assertEqual(len(self.batch.events), 2)

    def test_namespace_separates_independent_sources_not_sessions(self):
        self.add_stats(stats(token_record()))
        other = oh.UsageBatch("fixture-host", namespace="other-installation")
        other.add_stats(stats(token_record()), conversation_id="c1")
        self.assertNotEqual(self.batch.events[0]["request_id"], other.events[0]["request_id"])

    def test_cumulative_only_snapshot_is_not_a_request(self):
        self.add_stats({"usage_to_metrics": {"default": {"accumulated_token_usage": token_record()}}})
        self.assertEqual(self.batch.events, [])
        self.assertTrue(self.batch.inventory()["partial"])
        self.assertEqual(self.batch.diagnostics[0]["code"], "snapshot_without_request_history")

    def test_known_zero_and_missing_usage_are_distinct(self):
        zero = completion(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                          prompt_tokens_details=None, completion_tokens_details=None)
        self.add_log(zero)
        no_usage = completion("r2")
        no_usage["response"].pop("usage")
        self.add_log(no_usage)
        self.assertEqual([e["total"] for e in self.batch.events], [0])
        self.assertTrue(self.batch.inventory()["partial"])

    def test_invalid_primary_counts_are_never_defaulted_or_coerced(self):
        for value in (None, True, False, -1, 1.5, "100", {}, [], float("nan"), 2**53):
            with self.subTest(value=value):
                with self.assertRaises(oh.UsageError):
                    self.add_log(completion(prompt_tokens=value))
        self.assertEqual(self.batch.events, [])

    def test_inconsistent_totals_cache_reasoning_and_aliases_rejected(self):
        cases = [dict(total_tokens=121),
                 dict(prompt_tokens_details={"cached_tokens": 101}),
                 dict(completion_tokens_details={"reasoning_tokens": 21}),
                 dict(cache_read_input_tokens=99)]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(oh.UsageError):
                self.add_log(completion(**change))

    def test_optional_details_null_and_aliases_do_not_double_count(self):
        self.add_log(completion(cache_read_input_tokens=30, cache_creation_input_tokens=10))
        self.assertEqual(self.batch.events[0]["total"], 120)
        self.add_log(completion("r2", prompt_tokens_details={"cache_creation_tokens": None}))
        self.assertEqual(self.batch.events[1]["input_uncached"], 100)
        for bad in ([], False, 0, ""):
            with self.subTest(bad=bad), self.assertRaises(oh.UsageError):
                self.add_log(completion(prompt_tokens_details=bad))

    def test_missing_response_id_is_excluded_not_guessed(self):
        self.add_stats(stats(token_record(response_id="")))
        self.assertEqual(self.batch.events, [])
        self.assertIn("missing_response_id", [d["code"] for d in self.batch.diagnostics])

    def test_reconciliation_and_missing_response_usage_are_visible(self):
        payload = stats(token_record())
        m = payload["usage_to_metrics"]["default"]
        m["accumulated_token_usage"]["prompt_tokens"] += 50
        m["response_latencies"].append({"response_id": "missing-call"})
        self.add_stats(payload)
        self.assertEqual(len(self.batch.events), 1)
        self.assertEqual([d["code"] for d in self.batch.diagnostics],
                         ["accumulated_usage_does_not_reconcile", "responses_without_token_usage"])

    def test_failed_and_incomplete_calls_keep_usage_if_reported(self):
        log = completion(responses=True)
        log["response"]["status"] = "incomplete"
        log["response"]["error"] = "PRIVATE-ERROR"
        self.add_log(log)
        self.assertEqual(self.batch.events[0]["total"], 120)
        self.assertNotIn("PRIVATE-ERROR", json.dumps(self.batch.events))

    def test_error_log_without_response_has_no_fake_zero_event(self):
        self.add_log({"timestamp": 1, "latency_sec": 2, "cost": 0,
                      "error": {"message": "PRIVATE-ERROR"}})
        self.assertFalse(self.batch.events)
        self.assertTrue(self.batch.inventory()["partial"])

    def test_streamed_chunks_and_gateway_responses_are_not_calls(self):
        log = completion()
        log["response"]["object"] = "chat.completion.chunk"
        self.add_log(log)
        self.assertFalse(self.batch.events)
        with self.assertRaisesRegex(oh.UsageError, "not_an_sdk_completion_log"):
            self.add_log(completion()["response"])

    def test_completion_event_wrapper_projects_only_allowlisted_data(self):
        self.add_log(dict(kind="openhands.sdk.event.llm_completion_log.LLMCompletionLogEvent",
                          log_data=json.dumps(completion()), usage_id="condenser",
                          filename="PRIVATE-FILENAME", model_name="configured-alias"))
        text = json.dumps(self.batch.events)
        self.assertNotIn("PRIVATE-", text)
        self.assertEqual(self.batch.events[0]["usage_ids"], ["condenser"])
        self.assertNotIn("cost_usd", self.batch.events[0])
        self.assertIn("zero_may_mean_unavailable", self.batch.events[0]["sdk_cost_evidence"])

    def test_cost_values_cannot_turn_missing_tokens_into_usage(self):
        for cost in (-1, "12", False, float("inf"), float("nan"), 10**400):
            log = completion(str(cost))
            log["cost"] = cost
            self.add_log(log)
        self.assertTrue(all("sdk_reported_cost_usd" not in e for e in self.batch.events))

    def test_timestamps_never_use_file_mtime_cost_timestamp_or_local_timezone(self):
        for timestamp in (None, "2026-10-12T12:00:00", "invalid", float("inf"), True):
            log = completion(str(timestamp))
            log["timestamp"] = timestamp
            self.add_log(log)
        self.assertTrue(all(e["ts"] is None for e in self.batch.events))
        log = completion("aware")
        log["timestamp"] = "2026-10-12T12:00:00-04:00"
        self.add_log(log)
        self.assertEqual(self.batch.events[-1]["ts"], "2026-10-12T16:00:00+00:00")

    def test_collect_uses_public_registry_for_early_switched_llm(self):
        raw = metrics(token_record())
        live = SimpleNamespace(model_name=raw["model_name"],
                               token_usages=[SimpleNamespace(**r) for r in raw["token_usages"]],
                               response_latencies=[SimpleNamespace(**r) for r in raw["response_latencies"]],
                               accumulated_token_usage=SimpleNamespace(**raw["accumulated_token_usage"]))
        llm = SimpleNamespace(metrics=live)
        conversation = SimpleNamespace(id="c1", agent=SimpleNamespace(),
            conversation_stats=SimpleNamespace(usage_to_metrics={}),
            llm_registry=SimpleNamespace(usage_to_llm={"early-switch": llm}))
        batch = oh.collect_conversation(conversation, host="fixture-host")
        self.assertEqual(len(batch.events), 1)
        self.assertEqual(batch.events[0]["usage_ids"], ["early-switch"])
        conversation.conversation_stats.usage_to_metrics["early-switch"] = live
        again = oh.collect_conversation(conversation, host="fixture-host")
        self.assertEqual(len(again.events), 1)
        self.assertIs(llm.metrics, live)

    def test_acp_state_is_explicitly_excluded(self):
        self.add_stats({"agent": {"kind": "openhands.sdk.agent.acp_agent.ACPAgent"},
                        "stats": stats(token_record())})
        self.assertEqual(self.batch.events, [])
        self.assertEqual(self.batch.diagnostics[0]["code"], "acp_turn_metrics_not_supported")

    def test_invalid_groups_records_and_latency_ids_do_not_crash(self):
        payload = {"usage_to_metrics": {"bad-group": [], "default": metrics(token_record())}}
        m = payload["usage_to_metrics"]["default"]
        m["token_usages"].append(None)
        m["response_latencies"].append({"response_id": {"bad": "id"}})
        self.add_stats(payload)
        self.assertEqual(len(self.batch.events), 1)
        self.assertTrue(self.batch.inventory()["partial"])

    def test_source_data_is_not_mutated(self):
        payload = stats(token_record())
        before = copy.deepcopy(payload)
        self.add_stats(payload)
        self.assertEqual(payload, before)
        payload = completion()
        before = copy.deepcopy(payload)
        self.add_log(payload)
        self.assertEqual(payload, before)


class OpenHandsFileTests(unittest.TestCase):
    def test_strict_json_and_bounded_file_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            for content in ('{"x":1,"x":2}', '{"x":NaN}', '[]', 'incomplete', '{"x":Infinity}'):
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(oh.UsageError):
                    oh.load_source(path)
            path.write_bytes(b'{"x":0}')
            with patch.object(oh, "MAX_INPUT_BYTES", 4), self.assertRaisesRegex(oh.UsageError, "input_too_large"):
                oh.load_source(path)

    def test_output_is_private_nonoverwriting_and_no_inventory_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = oh.UsageBatch("fixture-host")
            batch.add_completion_log(completion(), conversation_id="c1")
            output = Path(tmp) / "export"
            events = oh.write_batch(batch, output)
            self.assertEqual(events.name, "events.openhands.jsonl")
            self.assertTrue((output / "openhands-coverage.json").is_file())
            self.assertFalse(list(output.glob("inventory.*.json")))
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(events.stat().st_mode), 0o600)
            self.assertNotIn("PRIVATE-", events.read_text())
            with self.assertRaises(FileExistsError):
                oh.write_batch(batch, output)

    def test_two_writers_cannot_overwrite_the_same_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            batch = oh.UsageBatch("fixture-host")
            target = Path(tmp) / "export"
            def write():
                try:
                    oh.write_batch(batch, target)
                    return True
                except FileExistsError:
                    return False
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: write(), range(2)))
            self.assertEqual(sorted(results), [False, True])

    def test_cli_end_to_end_partial_status_and_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "log.json"
            source.write_text(json.dumps(completion()))
            partial = Path(tmp) / "bad.json"
            partial.write_text('PRIVATE-INVALID-JSON')
            for allow_partial, expected in ((False, 2), (True, 0)):
                output = Path(tmp) / str(allow_partial)
                args = ["--completion-log", str(source), "--stats", str(partial),
                        "--host", "fixture-host", "--conversation-id", "c1", "--out-dir", str(output)]
                if allow_partial:
                    args.append("--allow-partial")
                with redirect_stdout(io.StringIO()) as stdout:
                    self.assertEqual(oh.main(args), expected)
                inventory = json.loads(stdout.getvalue())
                self.assertTrue(inventory["partial"])
                self.assertEqual(inventory["events"], 1)
                self.assertNotIn("PRIVATE-", stdout.getvalue())
                self.assertTrue((output / "events.openhands.jsonl").exists())
            self.assertIn("PRIVATE-PROMPT", source.read_text())  # Source unchanged.

    def test_cli_conflict_produces_no_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "a.json", Path(tmp) / "b.json"
            first.write_text(json.dumps(completion()))
            second.write_text(json.dumps(completion(prompt_tokens=101, total_tokens=121)))
            output = Path(tmp) / "out"
            with redirect_stderr(io.StringIO()) as stderr:
                code = oh.main(["--completion-log", str(first), "--completion-log", str(second),
                                "--host", "h", "--conversation-id", "c", "--out-dir", str(output)])
            self.assertEqual(code, 1)
            self.assertIn("conflicting_response_usage", stderr.getvalue())
            self.assertFalse(output.exists())

    def test_cli_help_requires_no_sdk_or_network(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/openhands_usage.py"), "--help"],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--completion-log", result.stdout)


if __name__ == "__main__":
    unittest.main()
