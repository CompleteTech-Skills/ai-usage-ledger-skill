# Pinned Plandex producer companion

`usage-export.patch.txt` applies only to `plandex-ai/plandex` at
`e2d772072efadbe41d2946d97d79be55532dbab5` (server release marker 2.2.1).
This is a locally maintained extension, not upstream or stock support.

The patch adds an opt-in POSIX exporter using only the Go standard library,
wires the shared HTTP-send boundary and both model-request scope paths, and
adds native transport tests. It leaves provider responses unchanged. Failure to
write evidence logs a fixed diagnostic and never retries the paid request.
Such failures can leave an unfinished start record or no record: inspect the
server's diagnostics and the collector's coverage, rather than assume completeness.

## Reproduce without live services

Use a clean full source checkout. The repository workflow runs these same steps
with Go 1.26.3 and Python 3.12; the ledger runtime gains no Go dependency.

```bash
git clone https://github.com/plandex-ai/plandex.git plandex
git -C plandex checkout --detach e2d772072efadbe41d2946d97d79be55532dbab5
# Run from the ledger checkout; do not apply over unrelated local changes.
git -C plandex apply --check --unidiff-zero "$PWD/integrations/plandex/usage-export.patch.txt"
git -C plandex apply --unidiff-zero "$PWD/integrations/plandex/usage-export.patch.txt"
export PLANDEX_BASE_DIR="$(mktemp -d)"
export PLANDEX_TEST_EXPORT_DIR="$(mktemp -d)"
(cd plandex/app/server && go build ./... && \
  go test -race ./usageexport ./model -v)
python tests/check_plandex_native.py --exports "$PLANDEX_TEST_EXPORT_DIR" \
  --evidence /absolute/new-test-output/plandex-native-evidence.json
```

Tests use deterministic HTTP transports, not a database, gateway service or real
provider. The native client produces two attempts, then a third, then a gateway
attempt. The verifier checks all ledger stores, replay, growth, the actual
OpenRouter cache-write field, coverage-only gateway counts and scoped HTML prices.
The server compiles as a whole; the tests do not launch or certify a deployment.

## Enabling a verified self-hosted build

This is a separate operator action. Do not enable it just to test installation.
Create an existing owner-only export directory (0700) and config file (0600):

```json
{
  "source_id": "your-opaque-server-id",
  "directory": "/absolute/private/plandex-exports",
  "project_ids": ["an-explicitly-approved-project-id"],
  "approved_project_scope": true
}
```

Set `PLANDEX_USAGE_EXPORT_CONFIG` to that config's absolute path before starting
the server. Configuration is loaded once per process on the first model request;
restart through your approved deployment process when changing it. Each process
creates its own private `journal-<id>.jsonl`. The exporter requires POSIX private
file modes; Windows collectors can read an explicitly approved local copy.

Only matching project/plan scope is exported. Provider/model IDs, timestamps,
opaque attempt/scope IDs, token counts and qualified cost evidence are retained.
Raw request bodies and credentials are never read by the exporter. Response bytes
pass through a bounded transient SSE-line buffer; only allowlisted usage metadata
is retained. Source prompts/responses are not archived by this integration.

Direct HTTPS OpenAI/OpenRouter endpoints have an understood inclusive-token
contract. Gateway/custom endpoints remain `gateway_reported_unverified` and
cannot contribute token totals. A completed record is not proof of provider
billing or complete capture. Mid-process crashes, unwritten data, requests without
approved scope, transport-internal redirects and SDK/provider-internal retries
remain coverage limits. No provider test calls, credentials, schedule, upstream
publication or production deployment are authorized by applying this patch.
