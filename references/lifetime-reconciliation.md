# Reconcile Codex lifetime usage

Use this workflow when the operator supplies a dated account counter and wants to measure coverage against recorded Codex usage. Provider exports and manual readings use the same private JSON input. Record a new dated snapshot each time the provider counter is checked.

## Prepare the inputs

Convert the account reading to the format below. Pick local aliases for the account and scope. For a workspace counter, use `workspace` and an alias for the specific workspace in `scope_id`.

```json
{
  "schema": "ai-usage-ledger-lifetime-v1",
  "snapshots": [
    {
      "provider": "openai",
      "account": "team-a",
      "product": "codex",
      "scope": "account",
      "scope_id": "team-a",
      "metric": "total_with_cache",
      "metric_label": "Lifetime tokens",
      "tokens": 12000000000,
      "precision": 1,
      "source": "Operator-recorded account counter",
      "observed_at": "2026-01-03T12:00:00Z"
    }
  ],
  "account_evidence": [
    {
      "provider": "openai",
      "account": "team-a",
      "product": "codex",
      "scope": "account",
      "scope_id": "team-a",
      "host": "fixture-desktop",
      "source_path": "/home/fixture/.codex/sessions",
      "since": "2026-01-01T00:00:00Z",
      "until": "2026-01-04T00:00:00Z",
      "level": "verified",
      "basis": "exclusive_account_window"
    }
  ]
}
```

- Use `output` only for a counter documented as the same output-token metric, including any reasoning already counted in output. Use `total_with_cache` for uncached input, cache reads, cache writes and output combined. Preserve the provider's actual `metric_label`. An ambiguous definition can use `unknown` and will show as incompatible.
- Enter a nonnegative integer for the displayed count. `precision` is the reporting increment in tokens, with `1` for an exact counter and `null` when precision is unknown. Add `counter_started_at` only when the provider supplies the beginning of that counter epoch. Use ISO timestamps with explicit offsets.
- Account evidence maps a stored host and half-open time window to the exact provider scope. Use an absolute `source_path` to narrow the match to a file or directory. Mark a window `verified` only with evidence for an exclusive account window or a provider match over that selected source and interval. Use `inferred` for weaker evidence such as plan, creator tags or working-directory hints. Leave events unassigned when they cannot be tied to a scope.

Pass `--lifetime` more than once to compare retained files together. Identical snapshots at the same observation collapse. Conflicting values or evidence about the same snapshot fail the import before report output is written. The normalized inputs are also retained in `report-data.json` under `lifetime.inputs`. Save that object as a separate private JSON input to replay it.

## Run and interpret the comparison

```bash
python scripts/ledger.py report --since 2026-01-03 --until 2026-01-03T12:00:00Z \
  --lifetime /private/counters.json
```

`--since` selects the interval section and its CSV. Each lifetime comparison uses all eligible recorded usage from the counter's known start, if supplied, through its observation. `--until` sets the latest usable observation. The account rows report confirmed usage, inferred usage and a shared unattributed pool. A positive difference is the amount above confirmed ledger usage. Ledger overage, incomparable scope or metric, missing baseline, missing verification, and a counter reset each have their own status.

Adjacent comparable snapshots can isolate differences between their dates. Any remaining lifetime difference stays unresolved. Source rows show the actual recorded dates and scan status. A gap in observed dates can also be a period of inactivity.

For rounded counters, the report shows a conservative range of up to one reporting increment on either side of the displayed number. Coverage percentages use the displayed value and are marked approximate. A decrease signals a reset when the reported ranges establish one. An overlapping or unknown range leaves the change uncertain.

## Include previous installations

On Windows, `--discover-installations` lists retained layouts such as `Windows.old` and the older `Users` tree on another fixed volume. It also lists unlettered volumes as unverified. This discovery reads only filesystem metadata.

Select an authorized installation, profile, or Codex home with `--prior-install NAME=ABSOLUTE_PATH`. An already-mounted, read-only Windows or Linux backup can be selected the same way. The old source appears as host `prior-NAME` for account evidence. Choose a name whose `prior-NAME` label is unused in the ledger. Repeat the flag for additional roots.

```bash
python scripts/ledger.py report --since 2026-01-03 --lifetime /private/counters.json \
  --prior-install 'old=/mnt/previous-windows/Windows.old'
```

The report reads Codex rollouts under the approved root and records inaccessible, missing, linked or partly readable sources. Plain JSONL and gzip are supported. Zstandard archives require Python 3.14 with `compression.zstd`. Previous-install observations appear in the lifetime section and its JSON evidence. The existing ledger, source files, interval totals and interval CSV keep their normal behavior.

Cross-source matches use the native Codex session UUID, timestamp and token breakdown. Conflicting known models are kept separate. Equal observations within one source retain their multiplicity. The JSON record preserves both paths for recognized copies and shows how many observations were folded. Usage with conflicting account evidence stays unattributed. Keep these outputs private because they can reveal account aliases, data locations and usage patterns.
