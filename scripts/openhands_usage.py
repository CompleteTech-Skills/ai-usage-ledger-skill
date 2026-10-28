#!/usr/bin/env python3
"""Opt-in, offline OpenHands SDK usage -> existing ledger scanner JSONL.

Read full native-LLM Metrics histories or SDK completion logs, not streaming
chunks, gateway responses, or accumulated counters. This adapter never estimates
from text, but SDK/LiteLLM records can contain upstream estimates; their provider
origin is not attested. No SDK dependency, callbacks, credentials or networking.
See references/openhands-sdk.md for evidence and coverage boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOOL = "openhands-sdk"
SDK_REVISION = "acca181af1f353975965bcf6486e1d0a58f444c0"
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_TOKENS = (1 << 53) - 1  # Existing stores round-trip counts through float.
TOKEN_FIELDS = (
    "prompt_tokens", "completion_tokens", "cache_read_tokens",
    "cache_write_tokens", "reasoning_tokens",
)
BUCKETS = ("input_uncached", "cache_read", "cache_write", "output", "reasoning")
USAGE_ORIGIN_NOTE = (
    "SDK/LiteLLM may estimate missing streaming usage. Recorded usage does not "
    "attest provider origin; reconcile with provider evidence before calling it verified."
)


class UsageError(ValueError):
    """A fixed diagnostic code; never contains input text or provider errors."""


def _object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise UsageError("invalid_object")
    return value


def _label(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise UsageError("invalid_identifier")
    if any(ord(c) < 32 for c in value):
        raise UsageError("invalid_identifier")
    return value


def _count(value: Any) -> int:
    # bool is an int subclass; rounding/truncating floats would invent usage.
    if type(value) is not int or not 0 <= value <= MAX_TOKENS:
        raise UsageError("invalid_token_count")
    return value


def _optional(obj: Mapping[str, Any], key: str) -> int:
    value = obj.get(key)
    return 0 if value is None else _count(value)


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    try:
        if type(value) in (int, float) and math.isfinite(value):
            stamp = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return None  # Never guess a timezone.
        else:
            return None
        return stamp.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UsageError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_: str) -> None:
    raise UsageError("nonfinite_json_number")


def _decode(data: bytes | str) -> Mapping[str, Any]:
    if len(data) > MAX_INPUT_BYTES:
        raise UsageError("input_too_large")
    try:
        return _object(json.loads(data, object_pairs_hook=_unique_object,
                                  parse_constant=_invalid_constant))
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise UsageError("invalid_json") from None


def load_source(path: Path) -> Mapping[str, Any]:
    """Read only an explicitly selected regular file, with a bounded read."""
    if not path.is_file():
        raise UsageError("source_not_file")
    try:
        with path.open("rb") as stream:
            return _decode(stream.read(MAX_INPUT_BYTES + 1))
    except OSError:
        raise UsageError("source_unreadable") from None


def _tokens(record: Mapping[str, Any]) -> tuple[int, int, int, int, int]:
    if record.get("prompt_tokens") is None or record.get("completion_tokens") is None:
        raise UsageError("missing_input_or_output_usage")
    values = tuple(_optional(record, name) for name in TOKEN_FIELDS)
    prompt, output, read, write, reasoning = values
    # Native LiteLLM prompt counts include BOTH cache buckets; reasoning is
    # already included in completion tokens. ACP turn metrics are different.
    if read + write > prompt or reasoning > output or prompt + output > MAX_TOKENS:
        raise UsageError("inconsistent_token_buckets")
    return prompt, output, read, write, reasoning


def _provider_usage(usage: Any) -> tuple[Mapping[str, Any], list[str]]:
    usage = _object(usage)
    chat = "prompt_tokens" in usage or "completion_tokens" in usage
    responses = "input_tokens" in usage or "output_tokens" in usage
    if chat == responses:
        raise UsageError("unsupported_or_ambiguous_usage_shape")
    inp, out = (("prompt_tokens", "completion_tokens") if chat
                else ("input_tokens", "output_tokens"))
    details = usage.get(inp + "_details")
    details = {} if details is None else _object(details)
    output_details = usage.get(out + "_details")
    output_details = {} if output_details is None else _object(output_details)
    mapped = {
        "prompt_tokens": usage.get(inp), "completion_tokens": usage.get(out),
        "cache_read_tokens": details.get("cached_tokens"),
        "cache_write_tokens": details.get("cache_creation_tokens"),
        "reasoning_tokens": output_details.get("reasoning_tokens"),
    }
    # Older LiteLLM serialized Anthropic cache fields at the usage root too.
    # They are aliases, NOT additional token buckets. Disagreement is unsafe.
    for target, alias in (("cache_read_tokens", "cache_read_input_tokens"),
                          ("cache_write_tokens", "cache_creation_input_tokens")):
        if usage.get(alias) is not None:
            if mapped[target] is not None and mapped[target] != usage[alias]:
                raise UsageError("conflicting_cache_aliases")
            mapped[target] = usage[alias]
    prompt, output, _, _, _ = _tokens(mapped)
    if usage.get("total_tokens") is not None:
        if _count(usage["total_tokens"]) != prompt + output:
            raise UsageError("inconsistent_total_tokens")
    unknown = [name for name in TOKEN_FIELDS[2:] if mapped[name] is None]
    return mapped, unknown


@dataclass
class UsageBatch:
    """One explicit import. Diagnostics contain codes/counts, never raw payloads.

    Response IDs must be unique within (host, namespace). Use a different
    namespace for independent sources whose providers reuse response IDs.
    Repeated snapshots, merged delegate metrics and completion-log copies use
    the SAME namespace to de-duplicate. No missing ID is guessed from tokens.
    """

    host: str
    namespace: str = "default"
    events: list[dict[str, Any]] = field(default_factory=list, init=False)
    diagnostics: list[dict[str, Any]] = field(default_factory=list, init=False)
    duplicates: int = field(default=0, init=False)
    _seen: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        _label(self.host)
        _label(self.namespace)

    def note(self, code: str, *, source: str, count: int = 1,
             exclusion: bool = True) -> None:
        self.diagnostics.append(dict(code=code, source=source, count=count,
                                     exclusion=exclusion))

    def _add(self, record: Mapping[str, Any], *, session: str, source: str,
             model: Any, usage_id: str | None = None, timestamp: Any = None,
             evidence: str = "sdk_metrics", unknown: list[str] | None = None,
             reported_cost: Any = None) -> None:
        prompt, output, read, write, reasoning = _tokens(record)
        response_id = record.get("response_id")
        if not response_id:
            raise UsageError("missing_response_id")
        response_id = _label(response_id)
        identity = json.dumps([self.namespace, response_id], ensure_ascii=True)
        request_id = "openhands:" + hashlib.sha256(identity.encode()).hexdigest()
        model = _label(model) if model else "unknown"
        ts = _timestamp(timestamp)
        event = dict(
            tool=TOOL, host=self.host, session=_label(session), model=model,
            request_id=request_id, response_id=response_id, ts=ts,
            date=ts[:10] if ts else None, input_uncached=prompt-read-write,
            cache_read=read, cache_write=write, output=output, reasoning=reasoning,
            total=prompt+output, src=source if os.path.isabs(source) else None,
            usage_source=source, kind="unknown", entrypoint="openhands-sdk",
            granularity="call", evidence_class="A", usage_evidence=evidence,
            # Even response.usage can originate in LiteLLM's tokenizer fallback.
            # The legacy usage_evidence label identifies shape, not verification.
            provider_usage_verified=False, sdk_usage_origin="not_attested",
            timestamp_source="sdk_completion_log" if ts else "unavailable",
            request_namespace=self.namespace, sdk_prompt_tokens=prompt, model_aliases=[model],
            observed_sessions=[session], usage_ids=[_label(usage_id)] if usage_id else [],
            unreported_token_fields=unknown or [],
            cache_write_ttl="unspecified_by_sdk" if write else None,
        )
        # Do not set cost_usd: Telemetry mixes provider and calculated cost,
        # and serializes missing cost as 0. Keep an explicitly qualified extra.
        if type(reported_cost) in (int, float) and 0 <= reported_cost <= sys.float_info.max:
            event["sdk_reported_cost_usd"] = reported_cost
            event["sdk_cost_evidence"] = "unverified_sdk_value; zero_may_mean_unavailable"
        old = self._seen.get(request_id)
        if old is not None:
            if any(old[key] != event[key] for key in BUCKETS):
                raise UsageError("conflicting_response_usage")
            self.duplicates += 1
            # Enrich before ingestion; the existing store itself is append-only.
            sessions = sorted(set(old["observed_sessions"] + event["observed_sessions"]))
            usages = sorted(set(old["usage_ids"] + event["usage_ids"]))
            models = sorted(set(old["model_aliases"] + event["model_aliases"]))
            if (old["ts"] is None and ts is not None) or (
                    evidence == "provider_response_usage" and old["usage_evidence"] != evidence):
                old.update(event)
            old["observed_sessions"], old["usage_ids"] = sessions, usages
            old["model_aliases"] = models
            return
        self._seen[request_id] = event
        self.events.append(event)

    def add_stats(self, payload: Any, *, conversation_id: str,
                  source: str = "openhands:stats") -> None:
        """Read full native ConversationStats or persisted state.stats.

        Network MetricsSnapshot objects are NOT per-request evidence. Neither
        costs nor response_latencies are zipped to token_usages for timestamps.
        """
        _label(conversation_id)
        payload = _object(payload)
        agent = payload.get("agent")
        if isinstance(agent, Mapping) and (agent.get("agent_kind") == "acp" or
                str(agent.get("kind", "")).endswith("ACPAgent")):
            self.note("acp_turn_metrics_not_supported", source=source)
            return
        stats = _object(payload.get("stats", payload))
        groups = _object(stats.get("usage_to_metrics"))
        for usage_id, raw in groups.items():
            try:
                self._add_metrics(_object(raw), session=conversation_id,
                                  usage_id=_label(usage_id), source=source)
            except UsageError as exc:
                if str(exc) == "conflicting_response_usage":
                    raise  # No output may be published for contradictory evidence.
                self.note(str(exc), source=source)

    def _add_metrics(self, metrics: Mapping[str, Any], *, session: str,
                     usage_id: str, source: str) -> None:
        records = metrics.get("token_usages")
        if not isinstance(records, list):
            raise UsageError("snapshot_without_request_history")
        sums = [0] * len(TOKEN_FIELDS)
        ids: set[str] = set()
        for record in records:
            try:
                record = _object(record)
                values = _tokens(record)
                self._add(record, session=session, source=source,
                          model=record.get("model") or metrics.get("model_name"),
                          usage_id=usage_id)
                response_id = record["response_id"]
                if response_id not in ids:
                    sums = [a + b for a, b in zip(sums, values)]
                    ids.add(response_id)
            except UsageError as exc:
                if str(exc) == "conflicting_response_usage":
                    raise
                self.note(str(exc), source=source)
        accumulated = metrics.get("accumulated_token_usage")
        if isinstance(accumulated, Mapping):
            for index, name in enumerate(TOKEN_FIELDS):
                if accumulated.get(name) is not None and _count(accumulated[name]) != sums[index]:
                    self.note("accumulated_usage_does_not_reconcile", source=source)
                    break
        latencies = metrics.get("response_latencies", [])
        if not isinstance(latencies, list):
            self.note("invalid_latency_history", source=source)
        else:
            missing = sum(1 for item in latencies if isinstance(item, Mapping)
                          and (not isinstance(item.get("response_id"), str)
                               or item.get("response_id") not in ids))
            if missing:
                self.note("responses_without_token_usage", source=source, count=missing)

    def add_completion_log(self, payload: Any, *, conversation_id: str,
                           source: str = "openhands:completion-log") -> None:
        """Accept Telemetry JSON or LLMCompletionLogEvent, not gateway JSON."""
        _label(conversation_id)
        data = _object(payload)
        usage_id = None
        context_model = None
        if "log_data" in data:
            kind = str(data.get("kind", ""))
            if kind not in ("LLMCompletionLogEvent", "openhands.sdk.event.llm_completion_log.LLMCompletionLogEvent"):
                raise UsageError("unsupported_event_kind")
            usage_id, context_model = data.get("usage_id"), data.get("model_name")
            if not isinstance(data["log_data"], str):
                raise UsageError("invalid_log_data")
            data = _decode(data["log_data"])
        if "timestamp" not in data or "latency_sec" not in data:
            raise UsageError("not_an_sdk_completion_log")
        response = data.get("response")
        if not isinstance(response, Mapping) or response.get("usage") is None:
            self.note("response_without_provider_usage", source=source)
            return
        if response.get("object") not in ("chat.completion", "response"):
            self.note("not_a_final_response", source=source)
            return
        # A Responses API failure/incomplete result can still report usage.
        # Do not infer zero from its status or finish_reason. SDK logs do not
        # attest whether the usage originated at the provider or in LiteLLM.
        mapped, unknown = _provider_usage(response["usage"])
        record = dict(mapped, response_id=response.get("id"))
        self._add(record, session=conversation_id, source=source,
                  model=response.get("model") or context_model or data.get("model"),
                  usage_id=usage_id, timestamp=data["timestamp"],
                  evidence="provider_response_usage", unknown=unknown,
                  reported_cost=data.get("cost"))

    def inventory(self) -> dict[str, Any]:
        return dict(
            tool=TOOL, schema_version=1, reviewed_sdk_revision=SDK_REVISION,
            events=len(self.events), duplicate_records=self.duplicates,
            undated_events=sum(e["ts"] is None for e in self.events),
            cache_write_ttl_unspecified_events=sum(e["cache_write"] > 0 for e in self.events),
            provider_usage_verified=False, origin_unverified_events=len(self.events),
            usage_origin_note=USAGE_ORIGIN_NOTE,
            partial=any(d["exclusion"] for d in self.diagnostics),
            diagnostics=self.diagnostics,
            coverage="Available native SDK request records only; not provider billing completeness.",
        )


def _metric_projection(metrics: Any) -> dict[str, Any]:
    """Project Metrics without serializing LLM config, messages or credentials."""
    result = {"model_name": metrics.model_name}
    if hasattr(metrics, "token_usages"):
        result["token_usages"] = [
            {name: getattr(item, name, None) for name in (*TOKEN_FIELDS, "model", "response_id")}
            for item in metrics.token_usages
        ]
    accumulated = metrics.accumulated_token_usage
    if accumulated is not None:
        result["accumulated_token_usage"] = {
            name: getattr(accumulated, name, None) for name in TOKEN_FIELDS
        }
    if hasattr(metrics, "response_latencies"):
        result["response_latencies"] = [
            {"response_id": item.response_id} for item in metrics.response_latencies
        ]
    return result


def collect_conversation(conversation: Any, *, host: str,
                         namespace: str = "default") -> UsageBatch:
    """Read a quiescent conversation after run/arun (also after pause/error).

    This installs no callback and changes no SDK settings. Read native stats
    PLUS public registry metrics: early model switches can be missing from
    ConversationStats in the reviewed SDK. Remote snapshots remain incomplete.
    """
    batch = UsageBatch(host, namespace)
    session = str(conversation.id)
    if type(conversation.agent).__name__ == "ACPAgent":
        batch.note("acp_turn_metrics_not_supported", source="openhands:conversation")
        return batch
    stats = conversation.conversation_stats
    groups = {name: _metric_projection(metrics)
              for name, metrics in stats.usage_to_metrics.items()}
    batch.add_stats({"usage_to_metrics": groups}, conversation_id=session)
    registry = getattr(conversation, "llm_registry", None)
    if registry is not None:
        groups = {name: _metric_projection(llm.metrics)
                  for name, llm in registry.usage_to_llm.items()}
        batch.add_stats({"usage_to_metrics": groups}, conversation_id=session,
                        source="openhands:registry")
    return batch


def write_batch(batch: UsageBatch, output_dir: Path | str) -> Path:
    """Write scanner-compatible data in a NEW, owner-only directory.

    Existing directories are never overwritten. Concurrent exporters must use
    different output directories; the ledger store remains single-writer.
    """
    output_dir = Path(output_dir)
    # Materialize before any writes; allow_nan=False defends the output format.
    rows = "".join(json.dumps(e, allow_nan=False) + "\n" for e in batch.events)
    inventory = json.dumps(batch.inventory(), indent=2, allow_nan=False) + "\n"
    output_dir.mkdir(mode=0o700)  # No recursive discovery/creation; fail if exists.
    for name, content in (("events.openhands.jsonl", rows), ("openhands-coverage.json", inventory)):
        fd = os.open(output_dir / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return output_dir / "events.openhands.jsonl"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stats", type=Path, action="append", default=[], help="Full native SDK stats/state JSON (repeatable)")
    parser.add_argument("--completion-log", type=Path, action="append", default=[], help="Existing SDK completion log/event JSON (repeatable)")
    parser.add_argument("--host", required=True, help="Stable execution-host label used by the ledger")
    parser.add_argument("--namespace", default="default", help="Stable response-ID namespace; same for overlapping sources")
    parser.add_argument("--conversation-id", required=True)
    parser.add_argument("--out-dir", required=True, type=Path, help="NEW output directory; its parent must exist")
    parser.add_argument("--allow-partial", action="store_true", help="Exit 0 despite documented exclusions (inventory remains partial)")
    args = parser.parse_args(argv)
    if not args.stats and not args.completion_log:
        parser.error("at least one --stats or --completion-log file is required")
    try:
        batch = UsageBatch(args.host, args.namespace)
        _label(args.conversation_id)
        # Prefer dated SDK completion records when a call is in both sources.
        for paths, convert in ((args.completion_log, batch.add_completion_log), (args.stats, batch.add_stats)):
            for path in paths:
                try:
                    convert(load_source(path), conversation_id=args.conversation_id, source=os.path.abspath(path))
                except UsageError as exc:
                    if str(exc) == "conflicting_response_usage":
                        raise
                    batch.note(str(exc), source=os.path.abspath(path))
        write_batch(batch, args.out_dir)
    except (UsageError, OSError) as exc:
        # Never print provider payloads, messages or raw OS exceptions.
        code = str(exc) if isinstance(exc, UsageError) else "output_write_failed"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 1
    print(json.dumps(batch.inventory()))
    return 2 if batch.inventory()["partial"] and not args.allow_partial else 0


if __name__ == "__main__":
    raise SystemExit(main())
