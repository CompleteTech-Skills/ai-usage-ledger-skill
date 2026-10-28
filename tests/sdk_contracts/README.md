# Installed OpenHands SDK contracts

These tests complement the standard-library parser and ledger workflow suites.
They install and execute the actual OpenHands SDK, pinned at
`acca181af1f353975965bcf6486e1d0a58f444c0`, with its frozen upstream dependency
lock (SDK 1.54.0, LiteLLM 1.93.0). The production ledger still has **no SDK
runtime dependency**.

The [dedicated CI job](../../.github/workflows/openhands-sdk-contract.yml) installs
only the SDK package from the upstream workspace. Dependency installation uses
network access; test execution refuses socket connections and DNS lookups,
including attempts swallowed by a library. Tests use an isolated temporary HOME,
workspace, conversation storage and logs. No operator credentials, personal usage
records, scheduled tasks, external telemetry or paid model calls are used.

## What executes

`check_openhands_sdk.py` exercises real `LLM.completion` / `acompletion` /
`responses` / `aresponses`, `Telemetry`, `Metrics`, `ConversationStats`, completion
log serialization, `LLMCompletionLogEvent`, stream assembly, and
`LocalConversation.send_message`, `run`, `switch_llm`, close and resume. Provider
transport entry points receive deterministic fixture responses; the ledger
adapter and SDK accounting, serialization and persistence are not mocked.
The unavailable-cost test explicitly replaces cost calculation with `None`.
The missing-stream-usage test drives the real LiteLLM fallback with `token_counter`
returning artificial counts (7 input, 3 output), avoiding tokenizer-asset downloads.
It proves such SDK-recorded estimates are NOT labeled provider-verified.

The persistence test executes three requests across two models and a resumed
conversation. It combines real SDK-written completion logs with full histories,
exports through the adapter, ingests into SQLite/JSON/CSV, repeats ingestion after
reopening each store, queries totals and provenance, and runs existing pricing and
reporting. Expected totals are three requests and 360 tokens, not six requests from
counting both logs and history. Deliberately synthetic rates test arithmetic, not
current provider prices. The retry test records a successful response without
manufacturing usage for a preceding rate-limit failure that supplied none.
The pinned SDK passes `num_retries` to `stop_after_attempt`, so this test configures
2 total attempts for one failure followed by success.

Important SDK distinction: `LLMResponse.metrics` is a `MetricsSnapshot`, **not** a
request-history list. Full histories live on `llm.metrics.token_usages` and the
members of `conversation.conversation_stats.usage_to_metrics`. The snapshot and
combined totals must not be ingested as additional calls. See the pinned
[LLMResponse definition](https://github.com/OpenHands/software-agent-sdk/blob/acca181af1f353975965bcf6486e1d0a58f444c0/openhands-sdk/openhands/sdk/llm/llm_response.py).

## Reproduce

From the ledger repository root, with a separate SDK checkout already at the
pinned revision above:

```bash
export SDK_CHECKOUT=/path/to/software-agent-sdk
export UV_PROJECT_ENVIRONMENT="$PWD/.venv-sdk-contract"
uv sync --project "$SDK_CHECKOUT" --frozen --no-dev --package openhands-sdk --python 3.12
"$UV_PROJECT_ENVIRONMENT/bin/python" tests/sdk_contracts/check_openhands_sdk.py
```

This is an explicitly invoked integration script, not an import-time dependency
of `python -m unittest discover -s tests -p 'test_openhands_*.py'`. Run the latter,
the package checks and the unchanged Quality gate as well. CI logs print actual
SDK and LiteLLM versions before the test results.

Passing these contracts establishes compatibility with the pinned SDK and
synthetic provider responses. It is not a live provider, Agent Server transport,
all-provider conformance, invoice reconciliation or deployment claim. See the
[integration guide](../../references/openhands-sdk.md) for timestamp, source
coverage, ACP, cost provenance and append-only-store limitations.
