# Quality Checks

Run the production-readiness checks from this repository root:

```bash
python3 scripts/validate_quality.py
```

The validator compiles Python files, runs Ruff, parses YAML/JSON files, renders Mermaid sources when Mermaid tooling is available, runs the pipeline `--help` smoke check, runs the parser fixture suite (`tests/make_fixtures.py`, `ALL OK`), the store and onboarding suite (`tests/test_store.py`, `STORE OK`), the security regression suite (`tests/test_security.py`, `SECURITY OK`: shell syntax in paths, manifest fields, the scheduler wrapper and branding stays literal; generated pages are self-contained with a CSP), checks that the committed example pages reference nothing on the network, and checks ClawHub bundle readiness.

GitHub Actions runs the same validator on push and pull request.

The gate also runs synthetic pricing and scoped-report tests. These cover local-midnight and daylight-saving boundaries, timestamp cutoffs, cached-token arithmetic and context pricing, report-only price overrides, read-only store access, private artifacts and HTML escaping. They do not scan personal agent logs or certify provider billing totals.

The same gate also runs `test_goose*.py`. A separate read-only GitHub Actions
matrix runs native Goose tests on Ubuntu, macOS and Windows with Python 3.12.
These tests use synthetic schema-17 databases and metadata only, including WAL,
concurrent changes, source-file preservation, corrupt/missing sources, legacy
headers, export exclusions, stable namespaces and per-model provider ambiguity.
The E2E test invokes the real scanner, each existing store backend, SQL queries,
shared snapshot pricing and HTML report generation, requiring zero additions on
an unchanged scan and only new native records after growth. See
[fixture notes](tests/README.goose.md). These checks do not certify live Goose
execution, historical release compatibility or provider billing.

## Experimental Plandex export collector

The Quality workflow additionally runs:

```bash
python -m unittest discover -s tests -p 'test_plandex*.py' -v
```

`test_plandex.py` covers scoped local discovery, nullable token evidence, retries and fallbacks, growing plans, starts without terminal records, partial JSONL, duplicate exports, aggregate exclusion, schema rejection, immutable-identity conflicts, checkpoint replay, source read-only behavior, and privacy/path boundaries. `test_plandex_integration.py` requires a full checkout and exercises the real SQLite/JSON/CSV stores, repeated ingestion, price separation, unpriced models and existing scoped HTML reports. Its rates and provider responses are synthetic, not current pricing claims.

`tests/plandex_sources/usage-v1.json` contains synthetic records emitted by the proposed Go exporter's `TestInteropExport`. It is deliberately outside `tests/fixtures`, which the existing fixture workflow regenerates and removes. No test connects to a Plandex database, personal account or live model. PostgreSQL role enforcement is not tested because this collector has no PostgreSQL path. The collector-owned SQLite checkpoint is not a Plandex source database.

Stock Plandex compatibility, upstream integration compilation, independent review, provider-specific gateway accounting and hosted-service support are separate gates. Passing synthetic tests does not establish those gates or authorize deployment. See [the integration guide](references/plandex-self-hosted.md).
