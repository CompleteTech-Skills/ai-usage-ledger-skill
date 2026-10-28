"""Synthetic, offline tests for the proposed Plandex self-hosted export collector."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import collect_plandex as collector  # noqa: E402


def invocation(attempt="attempt-1", **overrides):
    row = {
        "schema": collector.SCHEMA, "kind": "attempt", "granularity": "attempt",
        "source_id": "synthetic-server", "plandex_version": "2.2.1+usage-export.1",
        "attempt_id": attempt, "native_request_id": "provider-" + attempt,
        "project_id": "project-a", "plan_id": "plan-a", "session_id": "session-a",
        "execution_id": "execution-a", "step_id": "step-a", "provider": "openai",
        "model": "gpt-4.1", "ts": "2026-10-12T12:00:00-04:00", "status": "completed",
        "usage_basis": "provider_reported", "token_semantics": "inclusive_input_output",
        "usage": {"input_tokens": 1000, "output_tokens": 100, "cache_read_tokens": 200,
                  "cache_write_tokens": 50, "reasoning_tokens": 30},
        "native_cost": {"amount": "0.00123", "currency": "USD", "provenance": "provider_response"},
    }
    row.update(overrides)
    return row


class PlandexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "usage.jsonl"
        self.output = self.root / "collected"
        self.config = {
            "schema": "plandex-collector/1", "approved_project_scope": True,
            "sources": [{"source_id": "synthetic-server", "project_ids": ["project-a", "project-b"],
                         "versions": ["2.2.1+usage-export.1"], "files": [str(self.source)]}],
        }

    def write(self, rows, append=False):
        with self.source.open("a" if append else "w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")

    def scan(self):
        return collector.collect(self.config, str(self.output))

    def events(self):
        return [json.loads(x) for x in (self.output / "events.plandex.jsonl").read_text().splitlines()]

    def normalize(self, row):
        return collector.normalize(row, self.config["sources"][0], "f" * 64)

    def config_file(self, config=None, name="sources.json"):
        path = self.root / name
        path.write_text(json.dumps(config or self.config))
        return str(path)

    def test_single_call_exact_tokens_and_attribution(self):
        rid, event, gaps, tokens = self.normalize(invocation())
        self.assertEqual(event["request_id"], rid)
        self.assertEqual([event[k] for k in ("input_uncached", "cache_read", "cache_write", "output", "reasoning", "total")],
                         [750, 200, 50, 100, 30, 1100])
        self.assertEqual(tokens["input_tokens"], 1000)
        self.assertEqual(event["ts"], "2026-10-12T16:00:00+00:00")
        self.assertEqual(event["provider"], "openai")
        self.assertEqual(event["model"], "gpt-4.1")
        self.assertEqual(event["version"], "2.2.1+usage-export.1")
        self.assertEqual(gaps, [])
        self.assertNotIn("plan", event)  # Existing ledger field means billing plan, not Plandex plan.
        self.assertNotIn("cost_usd", event)
        self.assertEqual(event["plandex_native_cost"]["amount"], "0.00123")
        self.assertIsNone(event["src"])  # Raw-log archiving cannot accidentally copy a multi-user export.

    def test_multiple_projects_plans_providers_and_model_switches(self):
        rows = [invocation(), invocation("b", provider="openrouter", model="anthropic/claude-sonnet-4", plan_id="plan-b"),
                invocation("c", provider="ollama", model="qwen3:32b", project_id="project-b", session_id="session-b")]
        self.write(rows)
        receipt = self.scan()
        self.assertEqual(receipt["events"], 3)
        events = self.events()
        self.assertEqual(sum(e["total"] for e in events), 3300)
        self.assertEqual(len({e["cwd"] for e in events}), 2)
        self.assertEqual(len({e["plandex_plan_id"] for e in events}), 2)
        self.assertEqual({e["provider"] for e in events}, {"openai", "openrouter", "ollama"})

    def test_unknown_cache_is_not_reported_as_measured_zero(self):
        row = invocation(usage={"input_tokens": 900, "output_tokens": 75}, native_cost=None)
        _, event, gaps, _ = self.normalize(row)
        self.assertEqual(event["total"], 975)
        self.assertIsNone(event["plandex_native_tokens"]["cache_read_tokens"])
        self.assertIsNone(event["plandex_native_tokens"]["reasoning_tokens"])
        self.assertIn("unreported_cache_write_tokens", gaps)
        self.assertIn("ordinary input", event["plandex_cache_assumption"])
        self.assertIn("native_cost_unavailable", gaps)

    def test_zero_tokens_are_not_missing(self):
        _, event, _, _ = self.normalize(invocation(usage={k: 0 for k in collector.TOKEN_FIELDS}))
        self.assertEqual(event["total"], 0)
        self.assertEqual(event["plandex_missing_fields"], [])

    def test_missing_input_output_preserves_known_evidence_without_fabrication(self):
        for missing in ("input_tokens", "output_tokens"):
            with self.subTest(missing=missing):
                row = invocation()
                del row["usage"][missing]
                _, event, gaps, known = self.normalize(row)
                self.assertIsNone(event)
                self.assertIn("incomplete_input_output", gaps)
                self.assertIsNone(known[missing])
                other = "output_tokens" if missing == "input_tokens" else "input_tokens"
                self.assertEqual(known[other], row["usage"][other])

    def test_missing_usage_and_estimates_are_coverage_only(self):
        for change, expected in (({"usage": None}, "missing_usage"),
                                 ({"usage_basis": "estimated"}, "unreported_or_estimated_usage")):
            _, event, gaps, _ = self.normalize(invocation(**change))
            self.assertIsNone(event)
            self.assertIn(expected, gaps)

    def test_all_execution_statuses_keep_actual_consumption(self):
        self.write([invocation(str(i), status=status) for i, status in enumerate(sorted(collector.STATUSES))])
        self.assertEqual(self.scan()["events"], 4)
        self.assertEqual({e["plandex_status"] for e in self.events()}, collector.STATUSES)

    def test_retry_and_provider_fallback_are_distinct_attempts(self):
        rows = [invocation("retry-1", status="error", usage=None),
                invocation("retry-2", provider="openrouter", status="error"),
                invocation("retry-3", provider="openai", model="gpt-4.1-mini")]
        self.write(rows)
        receipt = self.scan()
        self.assertEqual(receipt["events"], 2)
        self.assertEqual(receipt["coverage_gaps"]["missing_usage"], 1)
        self.assertEqual(sum(e["total"] for e in self.events()), 2200)

    def test_multiple_calls_within_step_not_collapsed(self):
        self.write([invocation("one"), invocation("two")])
        self.assertEqual(self.scan()["events"], 2)
        self.assertEqual(len({e["plandex_step_id"] for e in self.events()}), 1)

    def test_repeated_scan_and_incremental_append(self):
        self.write([invocation()])
        first = self.scan()
        self.assertEqual(first["scan"]["parsed_lines"], 1)
        before = (self.output / "events.plandex.jsonl").read_bytes()
        second = self.scan()
        self.assertEqual(second["scan"].get("parsed_lines", 0), 0)
        self.assertEqual(before, (self.output / "events.plandex.jsonl").read_bytes())
        self.write([invocation("next")], append=True)
        third = self.scan()
        self.assertEqual(third["scan"]["parsed_lines"], 1)
        self.assertEqual(third["scan"]["new_cached_events"], 1)
        self.assertEqual(third["events"], 2)

    def test_growing_active_plan_and_interrupted_export(self):
        start = invocation(kind="attempt_start")
        self.write([start])
        self.assertEqual(self.scan()["unfinished_attempts"], 1)
        serialized = json.dumps(invocation()).encode()
        with self.source.open("ab") as fh:
            fh.write(serialized[:50])
        partial = self.scan()
        self.assertEqual(partial["events"], 0)
        self.assertEqual(partial["scan"]["partial_tail_files"], 1)
        with self.source.open("ab") as fh:
            fh.write(serialized[50:] + b"\n")
        final = self.scan()
        self.assertEqual(final["events"], 1)
        self.assertEqual(final["unfinished_attempts"], 0)
        self.write([invocation("continued")], append=True)
        self.assertEqual(self.scan()["events"], 2)

    def test_complete_json_without_newline_is_not_committed(self):
        self.source.write_text(json.dumps(invocation()))
        self.assertEqual(self.scan()["events"], 0)
        with self.source.open("a") as fh:
            fh.write("\n")
        self.assertEqual(self.scan()["events"], 1)

    def test_detailed_plus_aggregate_and_tool_records_never_double_count(self):
        rows = [invocation(), invocation(kind="aggregate", granularity="plan"),
                invocation("tool", kind="tool_execution"), invocation("planned", kind="planned")]
        self.write(rows)
        result = self.scan()
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["coverage_gaps"]["non_request_record"], 3)
        aggregate = invocation(kind="aggregate", granularity="plan", usage={"input_tokens": 100000})
        self.write([aggregate], append=True)
        self.assertEqual(self.scan()["events"], 1)

    def test_copies_and_whitespace_duplicates_preserve_identity(self):
        self.write([invocation(), invocation()])
        other = self.root / "copy.jsonl"
        other.write_text(json.dumps(invocation(), separators=(",", ":")) + "\n")
        self.config["sources"][0]["files"].append(str(other))
        self.assertEqual(self.scan()["events"], 1)
        self.assertEqual(len(self.events()), 1)

    def test_same_attempt_id_across_sources_is_not_collapsed(self):
        self.write([invocation()])
        other = self.root / "other.jsonl"
        other.write_text(json.dumps(invocation(source_id="other-server")) + "\n")
        source = copy.deepcopy(self.config["sources"][0])
        source.update(source_id="other-server", files=[str(other)])
        self.config["sources"].append(source)
        self.assertEqual(self.scan()["events"], 2)
        self.assertEqual(len({e["host"] for e in self.events()}), 2)

    def test_rotation_replays_without_loss_and_detects_edited_history(self):
        self.write([invocation()])
        self.scan()
        self.write([invocation("new-file")])
        result = self.scan()
        self.assertEqual(result["scan"]["restarted_files"], 1)
        self.assertEqual(result["events"], 2)
        original = (self.output / "events.plandex.jsonl").read_bytes()
        changed = invocation()
        changed["usage"]["output_tokens"] += 1
        self.write([changed])
        with self.assertRaisesRegex(collector.CollectionError, "conflicting"):
            self.scan()
        self.assertEqual(original, (self.output / "events.plandex.jsonl").read_bytes())

    def test_conflicting_duplicates_abort_before_any_snapshot_is_published(self):
        self.write([invocation(), invocation(status="error")])
        with self.assertRaisesRegex(collector.CollectionError, "conflicting"):
            self.scan()
        self.assertFalse((self.output / "events.plandex.jsonl").exists())
        self.write([invocation()])
        self.assertEqual(self.scan()["events"], 1)

    def test_output_failure_does_not_strand_checkpoint(self):
        self.write([invocation()])
        with mock.patch.object(collector, "atomic_lines", side_effect=OSError("synthetic disk failure")):
            with self.assertRaises(OSError):
                self.scan()
        result = self.scan()
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["scan"]["parsed_lines"], 1)

    def test_unauthorized_project_and_source_never_leak(self):
        self.write([invocation("private", project_id="PRIVATE_PROJECT", plan_id="PRIVATE_PLAN", email="PRIVATE_EMAIL"),
                    invocation("other", source_id="PRIVATE_SOURCE"), invocation()])
        receipt = self.scan()
        self.assertEqual(receipt["events"], 1)
        self.assertEqual(receipt["scan"]["outside_scope_records"], 2)
        for path in self.output.iterdir():
            self.assertNotIn(b"PRIVATE_", path.read_bytes())

    def test_prompts_credentials_and_user_names_are_never_retained(self):
        self.write([invocation(prompt="PRIVATE_PROMPT", api_key="PRIVATE_KEY", email="PRIVATE_EMAIL", response="PRIVATE_RESPONSE")])
        self.scan()
        for path in self.output.iterdir():
            self.assertNotIn(b"PRIVATE_", path.read_bytes())
        self.assertNotIn('"project-a"', json.dumps(self.events()))
        self.assertNotIn('"plan-a"', json.dumps(self.events()))
        self.assertNotIn("provider-attempt-1", json.dumps(self.events()))

    def test_all_source_bytes_and_permissions_remain_unchanged(self):
        self.write([invocation()])
        os.chmod(self.source, 0o400)
        before = self.source.stat()
        checksum = hashlib.sha256(self.source.read_bytes()).digest()
        self.scan()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), checksum)
        self.assertEqual(self.source.stat().st_mode, before.st_mode)
        self.assertEqual(self.source.stat().st_mtime_ns, before.st_mtime_ns)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_output_privacy(self):
        self.write([invocation()])
        self.scan()
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o700)
        for path in self.output.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_unavailable_source_is_explicit_and_can_recover(self):
        result = self.scan()
        self.assertEqual(result["events"], 0)
        self.assertEqual(result["scan"]["unavailable_files"], 1)
        self.write([invocation()])
        self.assertEqual(self.scan()["events"], 1)
        self.source.unlink()
        result = self.scan()
        self.assertEqual(result["events"], 1)  # Known history retained, not falsely declared current.
        self.assertEqual(result["scan"]["unavailable_files"], 1)

    def test_unsupported_and_historical_schema_are_not_guessed(self):
        self.write([invocation("old", schema="plandex-usage/0"), invocation("future", schema="plandex-usage/2"),
                    invocation("version", plandex_version="9.0.0"), invocation()])
        result = self.scan()
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["coverage_gaps"]["unsupported_schema"], 2)
        self.assertEqual(result["coverage_gaps"]["unsupported_version"], 1)

    def test_invalid_metadata_is_quarantined(self):
        for change in ({"ts": "2026-10-12"}, {"ts": "invalid"}, {"status": "planned"},
                       {"model": None}, {"provider": {"oops": 1.5}}, {"plan_id": None},
                       {"token_semantics": "exclusive"}, {"attempt_id": None}):
            with self.subTest(change=change):
                _, event, gaps, _ = self.normalize(invocation(**change))
                self.assertIsNone(event)
                self.assertTrue(gaps)

    def test_bad_counts_not_silently_coerced(self):
        for value in (-1, True, 1.5, "100", collector.MAX_COUNT + 1, {}):
            with self.subTest(value=value):
                row = invocation()
                row["usage"]["input_tokens"] = value
                _, event, gaps, _ = self.normalize(row)
                self.assertIsNone(event)
                self.assertIn("invalid_token_count", gaps)
        for fields in ({"cache_read_tokens": 9999}, {"reasoning_tokens": 9999},
                       {"input_tokens": collector.MAX_COUNT, "output_tokens": 1}):
            row = invocation()
            row["usage"].update(fields)
            self.assertIn("inconsistent_token_counts", self.normalize(row)[2])

    def test_malformed_json_duplicate_keys_and_nonfinite_values(self):
        bad = ['{"source_id":"synthetic-server","source_id":"other"}', '{"x":NaN}', '[1,2,3]', 'not json']
        self.source.write_text("\n".join(bad) + "\n" + json.dumps(invocation()) + "\n")
        result = self.scan()
        self.assertEqual(result["events"], 1)
        self.assertEqual(result["coverage_gaps"]["invalid_record"], 4)

    def test_invalid_native_cost_does_not_destroy_valid_tokens(self):
        for cost in ({"amount": "NaN"}, {"amount": -1}, [], "secret"):
            _, event, gaps, _ = self.normalize(invocation(native_cost=cost))
            self.assertIsNotNone(event)
            self.assertIn("invalid_native_cost", gaps)
            self.assertIsNone(event["plandex_native_cost"])
        for amount in ("0", "1.234567890123456789"):
            row = invocation(native_cost={"amount": amount, "currency": "EUR", "provenance": "plandex_native"})
            self.assertEqual(self.normalize(row)[1]["plandex_native_cost"]["amount"], amount)

    def test_config_requires_explicit_project_approval(self):
        valid = collector.load_config(self.config_file())
        self.assertEqual(valid, self.config)
        bad = copy.deepcopy(self.config)
        bad["approved_project_scope"] = False
        with self.assertRaises(collector.CollectionError):
            collector.load_config(self.config_file(bad))
        for field in ("project_ids", "versions", "files"):
            bad = copy.deepcopy(self.config)
            bad["sources"][0][field] = []
            with self.assertRaises(collector.CollectionError):
                collector.load_config(self.config_file(bad))

    def test_databases_and_credentials_are_never_discovered_or_opened(self):
        bad = copy.deepcopy(self.config)
        bad["sources"][0]["dsn"] = "postgres://private:private@production/db"
        with mock.patch.object(sqlite3, "connect", side_effect=AssertionError("must not connect")):
            with self.assertRaises(collector.CollectionError):
                collector.load_config(self.config_file(bad))
        with mock.patch.object(collector, "read_file", side_effect=AssertionError("must not read")):
            with self.assertRaises(collector.CollectionError):
                collector.load_config(str(self.root / "credentials.json"))

    def test_remote_and_relative_paths_refused(self):
        for path in ("postgres://server/db", "https://host/usage.jsonl", "//server/export.jsonl", "\\\\server\\export.jsonl", "usage.jsonl"):
            with self.subTest(path=path):
                with self.assertRaises(collector.CollectionError):
                    collector.local_path(path)

    @unittest.skipIf(os.name == "nt", "symlink creation can require extra privileges on Windows")
    def test_symlink_hardlink_and_fifo_sources_are_not_read(self):
        target = self.root / "original.jsonl"
        target.write_text(json.dumps(invocation()) + "\n")
        self.source.symlink_to(target)
        with self.assertRaises(collector.CollectionError):
            collector.load_config(self.config_file())
        self.source.unlink()
        os.link(target, self.source)
        self.assertEqual(self.scan()["scan"]["unavailable_files"], 1)
        self.source.unlink()
        target.unlink()
        os.mkfifo(self.source)
        self.assertEqual(self.scan()["scan"]["unavailable_files"], 1)

    def test_output_must_not_contain_sources_or_be_ledger_database(self):
        self.write([invocation()])
        with self.assertRaises(collector.CollectionError):
            collector.collect(self.config, str(self.root))
        with self.assertRaises(collector.CollectionError):
            collector.collect(self.config, str(self.output), "sqlite", str(self.source))
        with self.assertRaises(collector.CollectionError):
            collector.collect(self.config, str(self.output), "sqlite", str(self.output / "checkpoint.sqlite"))

    def test_store_cannot_alias_collector_artifacts_or_their_directories(self):
        self.write([invocation()])
        before = self.source.read_bytes()
        names = ("coverage.json", "coverage-records.jsonl", "events.plandex.jsonl",
                 "checkpoint.sqlite", "checkpoint.sqlite-wal", "nested/ledger.sqlite")
        for name in names:
            with self.subTest(name=name), self.assertRaisesRegex(collector.CollectionError, "outside"):
                collector.collect(self.config, self.output, "sqlite", self.output / name)
            self.assertFalse(self.output.exists(), "reject before creating any collector files")
        for kind in ("json", "csv"):
            with self.subTest(kind=kind), self.assertRaisesRegex(collector.CollectionError, "outside"):
                collector.collect(self.config, self.output, kind, self.root)
            self.assertFalse(self.output.exists())
        self.assertEqual(self.source.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows junction regression")
    def test_output_junction_cannot_replace_source_export(self):
        source_dir = self.root / "source"
        source_dir.mkdir()
        self.source = source_dir / "events.plandex.jsonl"
        self.config["sources"][0]["files"] = [str(self.source)]
        self.write([invocation()])
        before = self.source.read_bytes()
        alias = self.root / "output-alias"
        result = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(source_dir)],
                                capture_output=True, text=True, check=False)
        if result.returncode:
            self.skipTest("junction creation unavailable")
        self.assertTrue(alias.is_junction())
        with self.assertRaisesRegex(collector.CollectionError, "reparse"):
            collector.collect(self.config, alias)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(list(source_dir.iterdir()), [self.source])

    def test_changed_scope_requires_new_checkpoint(self):
        self.write([invocation()])
        self.scan()
        self.config["sources"][0]["project_ids"] = ["project-b"]
        with self.assertRaisesRegex(collector.CollectionError, "scope"):
            self.scan()
        self.output = self.root / "new-scope"
        self.assertEqual(self.scan()["events"], 0)

    def test_discovery_only_checks_explicit_metadata(self):
        self.write([invocation()])
        with mock.patch.object(collector, "read_file", side_effect=AssertionError("no source content reads")):
            result = collector.discover(self.config)
        self.assertEqual(result[0]["available_files"], 1)
        self.assertEqual(result[0]["stock_plandex_backfill"], "not_supported")

    def test_line_limit_fails_without_committing_offset(self):
        self.source.write_bytes(b"x" * (collector.MAX_LINE + 1) + b"\n")
        with self.assertRaises(collector.CollectionError):
            self.scan()
        self.write([invocation()])
        self.assertEqual(self.scan()["events"], 1)

    def test_gateway_raw_counts_are_retained_but_never_billed(self):
        row = invocation(usage_basis="gateway_reported_unverified", raw_usage={
            "prompt_tokens": 1000, "cache_creation_input_tokens": 250,
            "messages": "never-retain-me", "api_key": "never-retain-me"})
        self.write([row])
        receipt = self.scan()
        self.assertEqual(receipt["events"], 0)
        gaps = [json.loads(line) for line in (self.output / "coverage-records.jsonl").read_text().splitlines()]
        self.assertEqual(gaps[0]["known_raw_usage"], {"prompt_tokens": 1000, "cache_creation_input_tokens": 250})
        self.assertNotIn("never-retain-me", (self.output / "coverage-records.jsonl").read_text())

    def test_explicit_directory_discovers_only_native_journal_names(self):
        directory = self.root / "approved-exports"
        directory.mkdir()
        journal = directory / ("journal-" + "a" * 32 + ".jsonl")
        journal.write_text(json.dumps(invocation()) + "\n")
        (directory / "other.jsonl").write_text(json.dumps(invocation("not-approved")) + "\n")
        (directory / "auth.json").write_text("never-read-me")
        self.config["sources"][0].pop("files")
        self.config["sources"][0]["directories"] = [str(directory)]
        self.assertEqual(collector.discover(self.config)[0]["matched_files"], 1)
        self.assertEqual(self.scan()["events"], 1)
        added = directory / ("journal-" + "b" * 32 + ".jsonl")
        added.write_text(json.dumps(invocation("second-process")) + "\n")
        self.assertEqual(self.scan()["events"], 2)
        self.assertEqual(self.scan()["scan"].get("parsed_lines", 0), 0)

    def test_invalid_optional_identifiers_cannot_export_free_text(self):
        row = invocation(session_id={"user": "private-user"}, step_id="not an opaque identifier")
        _, event, gaps, _ = self.normalize(row)
        self.assertIn("invalid_session_id", gaps)
        self.assertIn("invalid_step_id", gaps)
        self.assertIsNone(event["plandex_step_id"])
        self.assertNotIn("private-user", collector.canonical(event))


if __name__ == "__main__":
    unittest.main()
