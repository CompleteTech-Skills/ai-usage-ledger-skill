"""Contracts against the pinned, INSTALLED OpenHands SDK (no paid calls).

Run explicitly in the SDK environment; not part of stdlib-only discovery.
SDK classes, serialization, telemetry, stream assembly and conversation execution
are real. Provider transports and the documented missing-usage tokenizer fallback
receive deterministic fixtures; no live model or personal usage data is used.
"""
from __future__ import annotations

import asyncio
import atexit
import importlib.metadata
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
# Before SDK import: isolate any SDK caches/settings and disable optional tracing.
HOME = tempfile.TemporaryDirectory(prefix="openhands-contract-home-")
atexit.register(HOME.cleanup)
os.environ.update(HOME=HOME.name, XDG_CONFIG_HOME=HOME.name, XDG_CACHE_HOME=HOME.name,
                  OPENHANDS_SUPPRESS_BANNER="1", LITELLM_LOCAL_MODEL_COST_MAP="True",
                  OTEL_SDK_DISABLED="true", DO_NOT_TRACK="1")
for key in tuple(os.environ):
    if key.startswith(("LMNR_", "OTEL_EXPORTER_")):
        os.environ.pop(key)

NETWORK_ATTEMPTS = []


def refuse_network(*args, **kwargs):
    NETWORK_ATTEMPTS.append(True)
    raise AssertionError("Network access is forbidden in SDK contract tests")


# Keep guards active during imports and all test methods. SDK imports can catch
# network failures, so tearDown also asserts that no attempt was swallowed.
GUARDS = ExitStack()
atexit.register(GUARDS.close)
for target in ("socket.create_connection", "socket.getaddrinfo", "socket.socket.connect",
               "socket.socket.connect_ex"):
    GUARDS.enter_context(patch(target, side_effect=refuse_network))

import openhands_usage as adapter  # noqa: E402
import compile_ai_logs as compiler  # noqa: E402
import ledger_query  # noqa: E402
import ledger_store  # noqa: E402
import scoped_report  # noqa: E402
from litellm import CustomStreamWrapper, RateLimitError  # noqa: E402
from litellm.types.llms.openai import ResponseAPIUsage, ResponsesAPIResponse  # noqa: E402
from litellm.types.utils import ModelResponse, ModelResponseStream, Usage  # noqa: E402
from openhands.sdk import LLM, Agent, LocalConversation  # noqa: E402
from openhands.sdk.conversation.conversation_stats import ConversationStats  # noqa: E402
from openhands.sdk.event.llm_completion_log import LLMCompletionLogEvent  # noqa: E402
from openhands.sdk.llm import Message, TextContent  # noqa: E402
from openhands.sdk.llm.utils.metrics import Metrics  # noqa: E402
from openhands.sdk.llm.utils.telemetry import Telemetry  # noqa: E402

MODEL = "openai/gpt-4o"
SECRET_MARKER = "fixture-credential-DO-NOT-EXPORT"
PROMPT_MARKER = "fixture-content-DO-NOT-EXPORT"


def chat_response(response_id="chat-contract", *, usage=True, prompt=100, output=20, model="gpt-4o"):
    kwargs = dict(id=response_id, model=model, object="chat.completion", created=1704067200,
                  choices=[dict(index=0, finish_reason="stop",
                                message=dict(role="assistant", content="Done."))])
    if usage:
        kwargs["usage"] = Usage(prompt_tokens=prompt, completion_tokens=output,
                                total_tokens=prompt + output,
                                prompt_tokens_details={"cached_tokens": 30,
                                                       "cache_creation_tokens": 10},
                                completion_tokens_details={"reasoning_tokens": 5})
    return ModelResponse(**kwargs)


def responses_response(response_id="resp-contract"):
    return ResponsesAPIResponse(
        id=response_id, model="gpt-4o", created_at=1704067200, status="completed",
        output=[dict(id="message-contract", type="message", role="assistant", status="completed",
                     content=[dict(type="output_text", text="Done.", annotations=[])])],
        parallel_tool_calls=False, tool_choice="auto", top_p=None, tools=[], instructions="",
        usage=ResponseAPIUsage(input_tokens=100, output_tokens=20, total_tokens=120,
                               input_tokens_details={"cached_tokens": 30},
                               output_tokens_details={"reasoning_tokens": 5}),
    )


def messages():
    return [Message(role="system", content=[TextContent(text="Reply briefly.")]),
            Message(role="user", content=[TextContent(text=PROMPT_MARKER)])]


class SDKContracts(unittest.TestCase):
    def setUp(self):
        self.assertEqual(NETWORK_ATTEMPTS, [], "A previous operation attempted networking")
        self.temp = tempfile.TemporaryDirectory(prefix="openhands-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.assertEqual(NETWORK_ATTEMPTS, [], "SDK attempted a real network operation")

    def llm(self, usage_id="agent", **kwargs):
        config = dict(model=MODEL, api_key=SECRET_MARKER, usage_id=usage_id, num_retries=0,
                      log_completions=True, log_completions_folder=str(self.root / "logs"))
        config.update(kwargs)
        return LLM(**config)

    def batch_for(self, llm):
        batch = adapter.UsageBatch("sdk-contract")
        for file in sorted((self.root / "logs").glob("*.json")):
            batch.add_completion_log(adapter.load_source(file), conversation_id="conversation",
                                     source=str(file))
        stats = ConversationStats(usage_to_metrics={llm.usage_id: llm.metrics})
        batch.add_stats(stats.model_dump(mode="json"), conversation_id="conversation")
        return batch

    def assert_usage(self, batch, *, write=10, count=1):
        self.assertEqual(len(batch.events), count)
        for event in batch.events:
            self.assertEqual([event[k] for k in adapter.BUCKETS], [70-write, 30, write, 20, 5])
            self.assertEqual(event["total"], 120)
            self.assertFalse(event["provider_usage_verified"])
            self.assertEqual(event["sdk_usage_origin"], "not_attested")
        serialized = json.dumps(batch.events)
        self.assertNotIn(SECRET_MARKER, serialized)
        self.assertNotIn(PROMPT_MARKER, serialized)

    def test_native_metrics_full_history_and_snapshot(self):
        metrics = Metrics(model_name=MODEL)
        metrics.add_token_usage(100, 20, 30, 10, 8192, "native-1", reasoning_tokens=5)
        stats = ConversationStats(usage_to_metrics={"agent": metrics})
        batch = adapter.UsageBatch("sdk-contract")
        batch.add_stats(stats.model_dump(mode="json"), conversation_id="conversation")
        self.assert_usage(batch)
        self.assertIsNone(batch.events[0]["ts"])
        snapshot = adapter.UsageBatch("sdk-contract")
        snapshot.add_stats(stats.model_dump(mode="json", context={"use_snapshot": True}),
                           conversation_id="conversation")
        self.assertEqual(snapshot.events, [])
        self.assertTrue(snapshot.inventory()["partial"])

    def test_sync_chat_through_real_llm_and_log(self):
        llm = self.llm()
        with patch("openhands.sdk.llm.llm.litellm_completion", return_value=chat_response()) as call:
            result = llm.completion(messages())
        self.assertEqual(call.call_count, 1)
        # LLMResponse exposes a snapshot, not a second per-request history.
        self.assertNotIn("token_usages", result.metrics.model_dump())
        self.assertEqual(result.metrics.accumulated_token_usage.prompt_tokens, 100)
        self.assertEqual(llm.metrics.token_usages[0].prompt_tokens, 100)
        batch = self.batch_for(llm)
        self.assert_usage(batch)
        self.assertEqual(batch.duplicates, 1)
        self.assertIsNotNone(batch.events[0]["ts"])

    def test_async_chat_through_real_llm_and_log(self):
        llm = self.llm()
        with patch("openhands.sdk.llm.llm.litellm_acompletion", new_callable=AsyncMock,
                   return_value=chat_response()) as call:
            asyncio.run(llm.acompletion(messages()))
        self.assertEqual(call.await_count, 1)
        self.assert_usage(self.batch_for(llm))

    def test_sync_responses_through_real_llm_and_log(self):
        llm = self.llm()
        with patch("openhands.sdk.llm.llm.litellm_responses", return_value=responses_response()) as call:
            llm.responses(messages())
        self.assertEqual(call.call_count, 1)
        self.assert_usage(self.batch_for(llm), write=0)

    def test_async_responses_through_real_llm_and_log(self):
        llm = self.llm()
        with patch("openhands.sdk.llm.llm.litellm_aresponses", new_callable=AsyncMock,
                   return_value=responses_response()) as call:
            asyncio.run(llm.aresponses(messages()))
        self.assertEqual(call.await_count, 1)
        self.assert_usage(self.batch_for(llm), write=0)

    def test_real_stream_assembly_preserves_reported_usage(self):
        llm = self.llm()
        chunks = [ModelResponseStream(id="stream-contract", model="gpt-4o", created=1704067200,
                  choices=[dict(index=0, delta=dict(role="assistant", content="Done."),
                                finish_reason=None)]),
                  ModelResponseStream(id="stream-contract", model="gpt-4o", created=1704067200,
                  choices=[dict(index=0, delta=dict(content=None), finish_reason="stop")],
                  usage=chat_response().usage)]
        stream = MagicMock(spec=CustomStreamWrapper)
        stream.__iter__.return_value = iter(chunks)
        delivered = []
        # Do NOT mock stream_chunk_builder: its usage handling is part of this contract.
        with patch("openhands.sdk.llm.llm.litellm_completion", return_value=stream):
            llm.completion(messages(), stream=True, on_token=delivered.append)
        self.assertEqual(len(delivered), 2)
        self.assert_usage(self.batch_for(llm))

    def test_missing_stream_usage_can_be_estimated_upstream_not_provider_verified(self):
        llm = self.llm()
        chunks = [ModelResponseStream(id="unreported-stream", model="gpt-4o", created=1704067200,
                  choices=[dict(index=0, delta=dict(role="assistant", content="Done."),
                                finish_reason="stop")])]
        stream = MagicMock(spec=CustomStreamWrapper)
        stream.__iter__.return_value = iter(chunks)
        # Exercise the REAL LiteLLM fallback without fetching tokenizer assets.
        # The deliberately artificial counts are NOT provider observations.
        target = "litellm.litellm_core_utils.streaming_chunk_builder_utils.token_counter"
        with patch("openhands.sdk.llm.llm.litellm_completion", return_value=stream), \
             patch(target, side_effect=[7, 3]) as estimate:
            llm.completion(messages(), stream=True, on_token=lambda chunk: None)
        self.assertEqual(estimate.call_count, 2)
        batch = self.batch_for(llm)
        self.assertEqual(len(batch.events), 1)
        self.assertEqual(batch.events[0]["total"], 10)
        self.assertFalse(batch.events[0]["provider_usage_verified"])
        self.assertEqual(batch.events[0]["sdk_usage_origin"], "not_attested")
        self.assertEqual(batch.inventory()["origin_unverified_events"], 1)
        self.assertIn("estimate", batch.inventory()["usage_origin_note"])

    def test_real_telemetry_missing_usage_and_unavailable_cost(self):
        llm = self.llm()
        with patch("openhands.sdk.llm.llm.litellm_completion", return_value=chat_response(usage=False)):
            llm.completion(messages())
        batch = self.batch_for(llm)
        self.assertEqual(batch.events, [])
        self.assertTrue(batch.inventory()["partial"])

    def test_real_completion_event_and_failed_cost_keep_tokens(self):
        metrics = Metrics(model_name=MODEL)
        telemetry = Telemetry(model_name=MODEL, metrics=metrics, log_enabled=True)
        logs, updates = [], []
        telemetry.set_log_completions_callback(lambda name, data: logs.append((name, data)))
        telemetry.set_stats_update_callback(lambda: updates.append(True))
        telemetry.on_request({})
        with patch.object(Telemetry, "_compute_cost", return_value=None):
            telemetry.on_response(chat_response())
        self.assertEqual(updates, [True])
        wrapper = LLMCompletionLogEvent(filename=logs[0][0], log_data=logs[0][1],
                                        model_name=MODEL, usage_id="agent")
        batch = adapter.UsageBatch("sdk-contract")
        batch.add_completion_log(wrapper.model_dump(mode="json"), conversation_id="conversation")
        self.assert_usage(batch)
        self.assertNotIn("cost_usd", batch.events[0])
        self.assertEqual(batch.events[0]["sdk_reported_cost_usd"], 0)

    def test_real_conversation_run_collect_and_repeat(self):
        llm = self.llm()
        workspace = self.root / "workspace"
        workspace.mkdir()
        conversation = LocalConversation(agent=Agent(llm=llm, tools=[]), workspace=workspace,
                                         persistence_dir=str(self.root / "conversations"),
                                         visualizer=None, max_iteration_per_run=2)
        self.addCleanup(conversation.close)
        with patch("openhands.sdk.llm.llm.litellm_completion", return_value=chat_response()):
            conversation.send_message(PROMPT_MARKER)
            conversation.run()
        first = adapter.collect_conversation(conversation, host="sdk-contract")
        self.assert_usage(first)
        again = adapter.collect_conversation(conversation, host="sdk-contract")
        self.assertEqual(first.events, again.events)
        self.assertEqual(first.events[0]["session"], str(conversation.id))
        self.assertEqual(first.events[0]["usage_ids"], ["agent"])

    def test_actual_conversation_resume_to_all_ledger_backends(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        persistence = self.root / "conversations"
        conversation = LocalConversation(agent=Agent(llm=self.llm(), tools=[]), workspace=workspace,
            persistence_dir=str(persistence), visualizer=None, max_iteration_per_run=2,
            delete_on_close=False)
        self.addCleanup(conversation.close)
        with patch("openhands.sdk.llm.llm.litellm_completion", return_value=chat_response("first")):
            conversation.send_message(PROMPT_MARKER)
            conversation.run()
        conversation.switch_llm(self.llm(usage_id="second", model="openai/gpt-4o-mini"))
        with patch("openhands.sdk.llm.llm.litellm_completion",
                   return_value=chat_response("second", model="gpt-4o-mini")):
            conversation.send_message(PROMPT_MARKER)
            conversation.run()
        before = adapter.collect_conversation(conversation, host="sdk-contract")
        self.assert_usage(before, count=2)
        self.assertEqual({e["model"] for e in before.events}, {MODEL, "openai/gpt-4o-mini"})
        cid = conversation.id
        conversation.close()
        resumed = LocalConversation(agent=None, workspace=workspace, persistence_dir=str(persistence),
            conversation_id=cid, visualizer=None, max_iteration_per_run=2, delete_on_close=False)
        self.addCleanup(resumed.close)
        self.assert_usage(adapter.collect_conversation(resumed, host="sdk-contract"), count=2)
        with patch("openhands.sdk.llm.llm.litellm_completion",
                   return_value=chat_response("third", model="gpt-4o-mini")):
            resumed.send_message(PROMPT_MARKER)
            resumed.run()
        captured = adapter.collect_conversation(resumed, host="sdk-contract")
        self.assert_usage(captured, count=3)
        # Combine the real SDK-written logs with full histories BEFORE ingestion.
        batch = adapter.UsageBatch("sdk-contract")
        for file in sorted((self.root / "logs").glob("*.json")):
            batch.add_completion_log(adapter.load_source(file), conversation_id=str(cid), source=str(file))
        batch.add_stats(resumed.conversation_stats.model_dump(mode="json"), conversation_id=str(cid))
        self.assert_usage(batch, count=3)
        self.assertTrue(all(e["ts"] for e in batch.events))
        source = adapter.write_batch(batch, self.root / "export")
        # Deliberately synthetic rates: this tests arithmetic, not current pricing.
        rate = dict(input=2, cache_read=.2, cache_write_5m=2.5, cache_write_1h=4, output=10)
        pricing = {"models": {"gpt-4o": rate, "gpt-4o-mini": rate}}
        for kind in ("sqlite", "json", "csv"):
            with self.subTest(kind=kind):
                path = str(self.root / ("ledger-" + kind))
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(store.ingest_events_file(str(source)), (3, 0))
                    self.assertEqual(store.ingest_events_file(str(source)), (0, 3))
                finally:
                    store.close()
                store = ledger_store.Store.open(kind, path)
                try:
                    self.assertEqual(store.ingest_events_file(str(source)), (0, 3))
                    rows = list(store.iter_events())
                    self.assertEqual(sum(e["total"] for e in rows), 360)
                    self.assertTrue(all(e["provider_usage_verified"] is False for e in rows))
                finally:
                    store.close()
                db = ledger_query.open_db(kind, path)
                try:
                    columns, values = ledger_query.run_query(db,
                        ledger_query.PRESETS["by-tool"][1].format(where="tool = ?"), [adapter.TOOL])
                    totals = dict(zip(columns, values[0]))
                    self.assertEqual((totals["calls"], totals["total"]), (3, 360))
                    verified = db.execute("SELECT COUNT(*) FROM events WHERE "
                        "json_extract(extra, '$.provider_usage_verified') = 1").fetchone()[0]
                    self.assertEqual(verified, 0)
                finally:
                    db.close()
                now = datetime.now(timezone.utc)
                days = [e["ts"][:10] for e in rows]
                scope = scoped_report.parse_scope(min(days), max(days), "UTC", now)
                report, selected = scoped_report.collect(rows, scope, pricing, now)
                self.assertEqual((report["totals"]["calls"], report["totals"]["total"]), (3, 360))
                self.assertAlmostEqual(report["totals"]["api_equivalent_usd"], .001053)
                self.assertEqual(len(selected), 3)
        prices_file = self.root / "prices.json"
        prices_file.write_text(json.dumps(pricing))
        compiled = self.root / "compiled"
        compiler.cmd_report(SimpleNamespace(out_dir=str(compiled), events=[str(source)],
            pricing=str(prices_file), accounts=None, sessions=[], prompts=[], inventory=[]))
        summary = json.loads((compiled / "summary.json").read_text())
        self.assertEqual((summary["events"], summary["total"]["total"]), (3, 360))
        self.assertEqual(len(summary["by_model"]), 2)
        self.assertTrue((compiled / "SUMMARY.md").is_file())

    def test_native_retry_does_not_manufacture_failed_attempt_tokens(self):
        # The pinned SDK passes num_retries to stop_after_attempt (total attempts).
        llm = self.llm(num_retries=2, retry_min_wait=1, retry_max_wait=1)
        error = RateLimitError(message="synthetic rate limit", llm_provider="openai", model="gpt-4o")
        with patch("openhands.sdk.llm.llm.litellm_completion",
                   side_effect=[error, chat_response("retry-success")]) as call:
            llm.completion(messages())
        self.assertEqual(call.call_count, 2)
        batch = self.batch_for(llm)
        self.assert_usage(batch)
        self.assertEqual(len(llm.metrics.token_usages), 1)

    def test_real_early_model_switch_is_collected_from_registry(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        conversation = LocalConversation(agent=Agent(llm=self.llm(), tools=[]), workspace=workspace,
                                         persistence_dir=str(self.root / "conversations"),
                                         visualizer=None, max_iteration_per_run=2)
        self.addCleanup(conversation.close)
        conversation.switch_llm(self.llm(usage_id="early"))
        with patch("openhands.sdk.llm.llm.litellm_completion", return_value=chat_response("early-1")):
            conversation.send_message(PROMPT_MARKER)
            conversation.run()
        batch = adapter.collect_conversation(conversation, host="sdk-contract")
        self.assert_usage(batch)
        self.assertIn("early", batch.events[0]["usage_ids"])


if __name__ == "__main__":
    print(json.dumps({"sdk_version": importlib.metadata.version("openhands-sdk"),
                      "litellm_version": importlib.metadata.version("litellm"),
                      "reviewed_sdk_revision": adapter.SDK_REVISION}), flush=True)
    unittest.main(verbosity=2)
