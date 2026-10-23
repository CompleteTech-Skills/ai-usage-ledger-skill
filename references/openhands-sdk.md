# OpenHands SDK token tracking

This opt-in adapter imports **SDK-recorded native LLM request usage** into the
ledger's existing scanner schema. It does not add an agent controller, telemetry
service, billing database, dashboard, provider dependency or background process.
Normal ledger storage, queries, pricing and reports remain authoritative.

## Evidence boundary: SDK-recorded is not automatically provider-verified

The adapter preserves reported input/output/cache/reasoning counts and never
calculates tokens from text, cost, context size or quotas. However, the pinned
LiteLLM stream assembler can itself substitute tokenizer estimates when a stream
has no usage, or a count is zero. The resulting SDK metrics and completion logs
do not reliably distinguish that fallback from provider-supplied counts. See
[LiteLLM 1.93.0 ChunkProcessor.calculate_usage](https://github.com/BerriAI/litellm/blob/052b5a2169d8d3082e1d66e69f200a72b0c1e274/litellm/litellm_core_utils/streaming_chunk_builder_utils.py).

Every exported request therefore carries `provider_usage_verified: false` and
`sdk_usage_origin: "not_attested"`. This does **not** mean all counts are estimates;
it means these source formats do not attest their origin. The coverage sidecar
and CLI output include `origin_unverified_events` and `usage_origin_note`.
The existing `usage_evidence: "provider_response_usage"` label is retained as a
response-format/source label, not as a verification claim; `sdk_metrics` identifies
the history source. Evidence class A means a recorded request, not a provider
invoice or an independent accuracy certification.

The existing reports aggregate SDK-recorded quantities and price those quantities;
they do not independently verify provider origin. Carry `openhands-coverage.json`
with any report or handoff and do not describe its totals as exclusively verified
provider usage. Preserve JSON/JSONL exports for evidence extras (the ordinary flat
CSV report/export selects fixed columns). SQLite, JSON and CSV stores retain the
extras; they are queryable through the existing SQL interface, for example:

```bash
python scripts/ledger.py query sql "SELECT json_extract(extra, '$.sdk_usage_origin') AS origin, COUNT(*) AS calls, SUM(total) AS tokens FROM events WHERE tool='openhands-sdk' GROUP BY 1"
```

Absent flags on older stored rows mean unknown, not verified. Provider-side
reconciliation is necessary for a provider-verified accounting claim; no invoice
or billed-amount claim can be inferred from this integration alone.

## Compatibility and discovery record

Reviewed on 2026-10-12 against ledger base
`e9d5e225a802d8eddcc7e1a2fc72cded6db1437e` and OpenHands SDK
`acca181af1f353975965bcf6486e1d0a58f444c0` (SDK 1.54.0). The installed-SDK contract
uses that revision's frozen dependency lock, including LiteLLM 1.93.0. This is a
source-shape compatibility target, not a guarantee for every past/future release.

| Pinned SDK source | Integration consequence |
|---|---|
| [Metrics / TokenUsage](https://github.com/OpenHands/software-agent-sdk/blob/acca181af1f353975965bcf6486e1d0a58f444c0/openhands-sdk/openhands/sdk/llm/utils/metrics.py) | `token_usages` is the request history. `accumulated_token_usage` is a reconciliation counter, not another call. Request records have response IDs but no timestamps. |
| [Telemetry](https://github.com/OpenHands/software-agent-sdk/blob/acca181af1f353975965bcf6486e1d0a58f444c0/openhands-sdk/openhands/sdk/llm/utils/telemetry.py) | Normalizes LiteLLM Chat Completions and Responses usage, records metrics, and optionally logs final responses, timestamps and SDK cost. Missing usage is not zero. |
| [ConversationStats](https://github.com/OpenHands/software-agent-sdk/blob/acca181af1f353975965bcf6486e1d0a58f444c0/openhands-sdk/openhands/sdk/conversation/conversation_stats.py) | `usage_to_metrics` separates usage slots. Do not add combined totals to their members. Network snapshots may omit histories. |
| [LLMResponse](https://github.com/OpenHands/software-agent-sdk/blob/acca181af1f353975965bcf6486e1d0a58f444c0/openhands-sdk/openhands/sdk/llm/llm_response.py) | `LLMResponse.metrics` is a `MetricsSnapshot`, not full history. Read `llm.metrics.token_usages` or conversation statistics for the history. |
| [LLMRegistry](https://github.com/OpenHands/software-agent-sdk/blob/acca181af1f353975965bcf6486e1d0a58f444c0/openhands-sdk/openhands/sdk/llm/llm_registry.py) | The public `usage_to_llm` view exposes metrics without replacing the single subscriber. |
| [LLMCompletionLogEvent](https://github.com/OpenHands/software-agent-sdk/blob/acca181af1f353975965bcf6486e1d0a58f444c0/openhands-sdk/openhands/sdk/event/llm_completion_log.py) | Existing remote log events carry JSON `log_data` plus `usage_id`. Only allowlisted usage/metadata is exported. |

SDK sync/async Chat Completions and Responses use its existing telemetry path.
No SDK patch, callback replacement or observability service is required. OpenHands
is not routed through the generic recursive sniffer: nested responses, snapshots
and copied histories would be unsafe to treat as independent calls. The open
prompt-composition PR [#4623](https://github.com/OpenHands/software-agent-sdk/pull/4623)
estimates composition offline; those estimates are not an input format here.

## Accepted evidence and normalization

`scripts/openhands_usage.py` reads explicitly selected JSON files only.

**Full native statistics:** `{"usage_to_metrics": {...}}`, or persisted state with
`stats.usage_to_metrics`. Include full `token_usages` lists for historical agent,
condenser and delegate groups. Do not supply `Metrics.diff()` slices as complete
histories. Available accumulated counters are checked against unique request
records; discrepancies are diagnostics, never guessed extra requests.

**Existing completion logs:** SDK Telemetry JSON containing `timestamp`,
`latency_sec`, and a final `response` whose `object` is `chat.completion` or
`response`. Serialized `LLMCompletionLogEvent` wrappers are accepted. Only final
`response.usage` is read; `usage_summary` and `raw_response` are not counted again.
Streaming chunks, tool/action events and gateway responses are not calls. A
failed/incomplete final response with usage retains that usage. An error without
usage becomes a coverage exclusion, not a fabricated zero-token event.

Primary input/output counts must be nonnegative integers. Booleans, numeric
strings, fractions, non-finite values, conflicting aliases, inconsistent totals,
and cache/reasoning subsets exceeding their parents are rejected. Counts are
bounded to the exact integer range supported by existing float-based store
round-trips. For native LiteLLM semantics:

```text
input_uncached = prompt_tokens - cache_read_tokens - cache_write_tokens
output        = completion_tokens                 # includes reasoning
reasoning     = reasoning_tokens                  # subset, not extra output
total         = input_uncached + cache_read + cache_write + output
```

Responses `input_tokens` / `output_tokens` map to the same quantities. Missing
optional detail fields normalize to zero with names in `unreported_token_fields`;
that zero is not asserted to have been reported. SDK metrics already contain
SDK defaults and cannot recover which optional provider fields were absent.

Aggregate cache-write tokens are retained, with no invented TTL. TTL-specific
columns stay absent; `cache_write_ttl` and coverage flag the limitation. The
existing pricer uses its short-duration fallback for aggregate writes, so that
part of API-equivalent cost is uncertain when the actual TTL is unknown.

SDK dollars remain in `sdk_reported_cost_usd` with an explicit unverified label,
not `cost_usd`. SDK cost may come from a proxy header or a calculation, and the
SDK writes zero when cost is unavailable. It never overrides ledger price
configuration. Unknown models remain unpriced in scoped reports; this adapter
adds no model-family price fallback. Provider invoices remain the billing source
of truth, and legacy `logged_cost_usd` query totals do not measure OpenHands spend.

## Import approved files into the existing ledger

Follow normal [onboarding and consent boundaries](../ONBOARDING.md) first. The
import command does not initialize the ledger, discover home directories, enable
SDK logging or start an agent. Supply the actual conversation ID and a stable
execution-host label. The new output directory's parent must exist.

```bash
python scripts/openhands_usage.py \
  --completion-log /approved/conversation/request.json \
  --stats /approved/conversation/base_state.json \
  --host runner-a --conversation-id CONVERSATION_ID \
  --out-dir ./openhands-export-001
```

Both input flags are repeatable; all files in one invocation belong to the
declared conversation. Import separate conversations separately. Combine the
richest evidence before first ingestion: completion logs provide timestamps that
metrics lack. `events.openhands.jsonl` is scanner-compatible; the separate
`openhands-coverage.json` carries diagnostics and origin uncertainty. It is not
named `inventory.*.json`, which is the compiler's different schema.

Exit `0` means no known exclusions in supplied evidence, not verified origin or
billing completeness; `2` means an export with coverage exclusions; `1` means a
fatal conflict or output failure. `--allow-partial` changes exit 2 to 0 without
removing diagnostics. Conflicting counts for the same response ID within a batch
abort before writing an export. Undated requests and origin uncertainty are
reported separately from exclusions. Always inspect the coverage sidecar.

Use the **actual configured store kind and path**, not a parallel store. The
example uses SQLite; JSON and CSV work too. Do not run concurrent store writers.

```bash
python scripts/ledger_store.py --kind sqlite --path /approved/ledger/store.sqlite \
  ingest ./openhands-export-001/events.openhands.jsonl
python scripts/ledger.py query totals --tool openhands-sdk
python scripts/ledger.py run --no-scan
python scripts/ledger.py report --since today --tz America/New_York
```

For an explicitly selected test store:

```bash
python scripts/ledger_query.py totals --tool openhands-sdk \
  --store-kind sqlite --store-path /approved/ledger/store.sqlite --format json
```

Unfiltered queries include undated records. Scoped reports exclude unusable
timestamps and expose that limitation. No import time, file modification time,
cost-list timestamp or conversation-end timestamp is assigned to historical calls.

## Post-run Python capture

Add the ledger's `scripts` directory to the existing application's import path.
Capture when its conversation is quiescent, after `run()` / `arun()` finishes,
pauses or fails, and before disposing of an LLM whose history is not retained.

```python
from pathlib import Path
from openhands_usage import collect_conversation, write_batch

# The application creates, owns and runs conversation.
usage = collect_conversation(conversation, host="runner-a")
write_batch(usage, Path("./openhands-export-002"))
```

Use application error handling so export failure does not mask an agent failure.
Each export directory must be new. The helper reads public conversation statistics
plus public registry metrics, projecting metric fields rather than serializing
LLM configuration. It never resets metrics, installs/replaces callbacks, enables
logs or sends data anywhere. Repeated exports deduplicate when ingested.

Existing logs may contain sensitive prompts/configuration; select approved files
only. They are parsed in memory, but messages, completions, tool arguments,
response bodies, tracebacks, headers and credential fields are not exported.
Source paths and request/conversation/model identifiers are retained metadata.
POSIX directories/files use 0700/0600. Use an ACL-restricted parent on Windows.
Existing output directories are refused.

## Identity and upstream coverage limits

The request key hashes `(namespace, response_id)`; the store additionally keys by
tool and host. It ignores source filename, snapshot order, usage slot and
conversation ID to deduplicate copied/resumed/forked/delegate histories. Different
IDs with identical counts remain distinct calls. `observed_sessions`, `usage_ids`
and `model_aliases` retain multiple observations; primary session is the first
observed attribution, not proof a shared call ran twice. Models come from the
request record, never the current agent configuration alone.

Use different namespaces for independent providers/proxies that reuse response
IDs, and the same namespace for overlapping sources. Missing IDs are excluded,
not guessed from tokens. Do not import the same calls through a generic parser
under another tool name. The existing store is append-only: later imports cannot
silently enrich/correct stored records. Reconcile corrections explicitly with the
operator and select the richest evidence before first ingestion.

Known upstream issues, not fixes claimed by this PR:

- [#5647](https://github.com/OpenHands/software-agent-sdk/issues/5647): continued
  gateway requests can report conversation totals; not accepted as native calls.
- [#5639](https://github.com/OpenHands/software-agent-sdk/issues/5639): early-switched
  LLM can be missing from stats. Local capture also reads the public registry;
  a state file alone cannot recover an omitted group.
- [#5561](https://github.com/OpenHands/software-agent-sdk/issues/5561): persisted/API
  snapshots can lag live nested metrics; not proof of the latest in-flight usage.
- [#5687](https://github.com/OpenHands/software-agent-sdk/issues/5687): ACP turn/context
  metrics differ. Known ACP state/agent objects are rejected. Stats-only files
  have no agent discriminator; their producer must establish native provenance.

Telemetry may omit zero-token rows and have no usage after a transport failure or
cancellation. Some retried attempts are not retained as separate error logs.
Available latency records lacking usage are diagnosed, but neither their absence
nor a successful import proves all provider attempts were captured. Counters and
costs never manufacture missing requests.

## Validation

```bash
python -m unittest discover -s tests -p 'test_openhands_*.py' -v
python -m unittest discover -s tests -p 'test_package_*.py' -v
python scripts/validate_package.py
python scripts/validate_quality.py
```

The standard-library OpenHands workflow runs on Python 3.12 and 3.13. A separate
[installed-SDK contract](../tests/sdk_contracts/README.md) executes the pinned real
SDK, telemetry, serialization, stream assembly, conversation run/switch/resume,
and existing SQLite/JSON/CSV storage, queries, pricing and reporting. Its synthetic
responses avoid paid calls; network guards are active during test execution.
The missing-stream-usage case drives LiteLLM's real fallback with a deterministic
tokenizer substitute and verifies that estimated quantities are not labeled
provider-verified. Dependency installation is CI-only, not a production dependency.
Existing package and Quality gates remain unchanged. No live provider, live Agent
Server transport, independent approval or deployment claim follows from these tests.
