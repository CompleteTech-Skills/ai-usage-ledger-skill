"""Synthetic coverage for lifetime reports and previous-install collection."""

from contextlib import closing
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import compile_ai_logs  # noqa: E402
import installation_history  # noqa: E402
import ledger_store  # noqa: E402
import lifetime_reconciliation as lifetime  # noqa: E402
import safety  # noqa: E402
import scoped_report  # noqa: E402

DAY1 = "2026-01-01T12:00:00Z"
DAY2 = "2026-01-02T12:00:00Z"
DAY3 = "2026-01-03T12:00:00Z"
DAY4 = "2026-01-04T12:00:00Z"
NOW = datetime(2026, 1, 5, tzinfo=timezone.utc)
S1 = "00000000-0000-4000-8000-000000000001"
S2 = "00000000-0000-4000-8000-000000000002"
S3 = "00000000-0000-4000-8000-000000000003"


def account(alias, **changes):
    return {"provider": "openai", "account": alias, "product": "codex", "scope": "account",
            "scope_id": alias, **changes}


def snapshot(alias, tokens, when=DAY3, **changes):
    return {**account(alias), "metric": "total_with_cache", "metric_label": "Lifetime tokens",
            "tokens": tokens, "precision": 1, "source": "Synthetic counter", "observed_at": when, **changes}


def evidence(alias, host, **changes):
    return {**account(alias), "host": host, "basis": "exclusive_account_window",
            "since": "2025-01-01T00:00:00Z", "until": DAY4, "level": "verified", **changes}


def event(host, sid, total, stamp=DAY1, *, output=None, **changes):
    output = min(2, total) if output is None else output
    return {"host": host, "tool": "codex", "session": sid, "ts": stamp, "src": f"/synthetic/{host}/{sid}.jsonl",
            "model": "fixture", "input_uncached": total - output, "cache_read": 0, "cache_write": 0,
            "output": output, "reasoning": min(1, output), "total": total, "plan": "pro", **changes}


def write_json(path, value):
    safety.write_private(str(path), json.dumps(value))
    return path


def input_file(path, snapshots, evidence_rows):
    return write_json(path, {"schema": lifetime.SCHEMA, "snapshots": snapshots, "account_evidence": evidence_rows})


def rollout(home, sid, total, *, compressed=False, stamp=DAY1, model="fixture"):
    sessions = home / "sessions/2026/01/01"
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"rollout-2026-01-01T12-00-00-{sid}.jsonl"
    rows = [{"timestamp": stamp, "type": "session_meta", "payload": {"id": sid}},
            {"timestamp": stamp, "type": "turn_context", "payload": {"model": model}},
            {"timestamp": stamp, "type": "event_msg", "payload": {"type": "token_count", "info": {
                "last_token_usage": {"input_tokens": total - 2, "output_tokens": 2, "reasoning_output_tokens": 1},
                "total_token_usage": {"total_tokens": total}}}}]
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode()
    if compressed:
        path = path.with_suffix(".jsonl.gz")
        path.write_bytes(gzip.compress(payload))
    else:
        path.write_bytes(payload)
    return path


class LifetimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def run_data(self, rows, snapshots=(), evidence_rows=(), archive=(), end=DAY4):
        path = input_file(self.root / "counters.json", list(snapshots), list(evidence_rows))
        inputs = lifetime.read_inputs([path, path])
        return lifetime.reconcile(rows, archive, inputs, datetime.fromisoformat(end.replace("Z", "+00:00")), True, [])

    def test_scope_and_attribution_at_provider_cutoff(self):
        rows = [event("a", S1, 9_000_000_000), event("b", S2, 7_000_000_000),
                event("shadow", S3, 3_000_000, creator_account_id="a"),
                event("weak", S1, 4_000_000, DAY2), event("a", S1, 4_000_000, DAY4)]
        data = self.run_data(rows, [snapshot("team-a", 12_000_000_000), snapshot("team-b", 7_000_000_000)],
                             [evidence("team-a", "a"), evidence("team-b", "b"),
                              evidence("team-a", "weak", level="inferred", basis="plan_hint")])
        a, b = data["accounts"]
        self.assertEqual(len(data["inputs"]["snapshots"]), 2)
        self.assertEqual(a["lifetime"]["confirmed_tokens"], 9_000_000_000)
        self.assertEqual(a["lifetime"]["difference_tokens"], 3_000_000_000)
        self.assertEqual(a["lifetime"]["coverage_percent"], "75.00")
        self.assertEqual(a["lifetime"]["inferred_tokens"], 4_000_000)
        self.assertEqual(a["lifetime"]["unattributed_tokens"], 3_000_000)
        self.assertEqual(b["status"], "matched")
        self.assertEqual(b["lifetime"]["confirmed_tokens"], 7_000_000_000)

    def test_metrics_windows_resets_unmatched_scopes_and_exact_integers(self):
        large = 2**53 + 107
        rows = [event("host", S1, large, DAY1, output=16), event("host", S2, 34, DAY2, output=8)]
        snapshots = [
            snapshot("valid", 20, DAY1, metric="output"), snapshot("valid", 30, DAY2, metric="output", precision=10),
            snapshot("valid", 200, DAY1, metric="unknown", metric_label="Tokens generated"),
            snapshot("valid", large + 20, DAY3), snapshot("reset", 120, DAY1), snapshot("reset", 100, DAY2),
            snapshot("workspace", 10, DAY3, scope="workspace", scope_id="separate"),
        ]
        data = self.run_data(rows, snapshots, [evidence("valid", "host"), evidence("reset", "unused"),
                                               evidence("workspace", "unused-2"), evidence("missing", "unused-3")])
        by_key = {(row["account"], row["metric"]): row for row in data["accounts"]}
        output = by_key["valid", "output"]
        self.assertEqual(output["lifetime"]["confirmed_tokens"], 24)
        self.assertEqual(output["lifetime"]["difference_tokens"], 6)
        self.assertEqual(output["intervals"][0]["confirmed_tokens"], 8)
        self.assertEqual(output["intervals"][0]["difference_tokens"], 2)
        self.assertEqual(output["snapshot"]["precision"], 10)
        total = by_key["valid", "total_with_cache"]
        self.assertEqual(total["status"], "ledger_exceeds_counter")
        self.assertEqual(total["lifetime"]["difference_tokens"], -14)
        self.assertEqual(by_key["reset", "total_with_cache"]["status"], "counter_reset")
        self.assertIsNone(by_key["reset", "total_with_cache"]["lifetime"]["difference_tokens"])
        self.assertEqual(by_key["valid", "unknown"]["status"], "incompatible_metric")
        self.assertEqual(by_key["workspace", "total_with_cache"]["status"], "incompatible_scope")
        self.assertEqual(by_key["missing", "total_with_cache"]["status"], "missing_baseline")

    def test_path_boundary_and_conflicting_copies(self):
        shared = event("one", S1, 9, src=r"C:\Home\Codex\sessions\same.jsonl")
        copied = dict(shared, host="two", src="/old/sessions/same.jsonl")
        sibling = event("one", S2, 17, src=r"C:\Home\Codex-other\sessions\unique.jsonl")
        data = self.run_data([shared, shared, sibling], [snapshot("a", 12), snapshot("b", 22)],
                             [evidence("a", "one", source_path=r"c:\home\codex"), evidence("b", "two")], [copied])
        self.assertEqual(data["observed"]["calls"], 3)
        self.assertEqual(data["observed"]["total"], 35)
        self.assertEqual(data["notes"]["conflicting_attribution_calls"], 1)
        self.assertEqual(data["observed"]["unattributed_total"], 26)
        self.assertEqual(data["accounts"][0]["lifetime"]["confirmed_tokens"], 9)
        self.assertEqual(data["duplicate_sources"][0]["copies_removed"], 1)
        self.assertEqual({row["path"] for row in data["duplicate_sources"][0]["paths"]},
                         {shared["src"], copied["src"]})

    def test_import_rejects_bad_evidence_and_conflicting_snapshots(self):
        path = input_file(self.root / "input.json", [snapshot("a", 12), snapshot("a", 13)], [])
        with self.assertRaisesRegex(ValueError, "conflicting provider snapshots"):
            lifetime.read_inputs([path])
        path = input_file(path, [], [evidence("a", "host", basis="creator_tag")])
        with self.assertRaisesRegex(ValueError, "verified evidence"):
            lifetime.read_inputs([path])

    def test_rounded_changes_and_each_proven_interval(self):
        rows = [event("a", S1, 120), event("a", S2, 10, DAY2), event("a", S3, 30, DAY3),
                event("b", S1, 104, DAY2)]
        snapshots = [snapshot("a", 120, DAY1), snapshot("a", 140, DAY2), snapshot("a", 170, DAY3),
                     snapshot("b", 100, DAY3, precision=10),
                     snapshot("uncertain", 101, DAY1), snapshot("uncertain", 100, DAY2, precision=10)]
        data = self.run_data(rows, snapshots, [evidence("a", "a"), evidence("b", "b"), evidence("uncertain", "unused")])
        a, b, uncertain = data["accounts"]
        self.assertEqual([row["difference_tokens"] for row in a["intervals"]], [10, 0])
        self.assertEqual(a["unresolved_difference_tokens"], 0)
        self.assertEqual(b["status"], "within_reported_precision")
        self.assertEqual(b["lifetime"]["difference_range_tokens"], [-13, 5])
        self.assertEqual(uncertain["status"], "counter_change_uncertain")

    def test_same_account_can_have_account_and_workspace_evidence(self):
        workspace = {"scope": "workspace", "scope_id": "project"}
        data = self.run_data([event("host", S1, 9)],
                             [snapshot("a", 12), snapshot("a", 10, **workspace)],
                             [evidence("a", "host"), evidence("a", "host", **workspace)])
        self.assertEqual([row["lifetime"]["confirmed_tokens"] for row in data["accounts"]], [9, 9])
        self.assertEqual(data["observed"]["total"], 9)
        self.assertEqual(data["notes"]["conflicting_attribution_calls"], 0)

    def test_previous_scope_uses_native_identity_and_survives_damaged_lines(self):
        profiles = self.root / "Users"
        home = profiles / "fixture/.codex"
        archive = rollout(home, S1, 9, model=None)
        renamed = archive.rename(archive.with_name("rollout-renamed.jsonl"))
        with renamed.open("ab") as handle:
            handle.write(b'{"type":"event_msg","payload":["token_count"]}\n{"truncated')
        rollout(home, S2, 12, stamp=DAY4)
        coverage = []
        prior = installation_history.usage({"prior-old": profiles}, coverage, end=datetime.fromisoformat(DAY3.replace("Z", "+00:00")))
        inputs = lifetime.read_inputs([])
        result = lifetime.reconcile([event("current", S1, 9)], prior, inputs, NOW, True, coverage)
        self.assertEqual(result["observed"]["total"], 9)
        self.assertEqual(result["duplicate_sources"][0]["copies_removed"], 1)
        self.assertEqual(coverage[0]["events_after_cutoff"], 1)
        self.assertEqual(coverage[0]["parse_errors"], 2)
        self.assertEqual(coverage[0]["status"], "partial")
        self.assertEqual(coverage[0]["first"], coverage[0]["last"])

    def test_historical_validation_preserves_active_fork_replay_timing(self):
        home = self.root / ".codex"
        path = rollout(home, S1, 9)
        meta, _, usage = map(json.loads, path.read_text(encoding="utf-8").splitlines())
        meta["payload"]["forked_from_id"] = S2
        usage["timestamp"] = "2026-01-01T12:00:03Z"
        middle = [{"timestamp": f"2026-01-01T12:00:0{second}Z", "type": "response_item",
                   "payload": {"type": "message"}} for second in (1, 2)]
        path.write_text("".join(json.dumps(row) + "\n" for row in (meta, *middle, usage)), encoding="utf-8")

        current = io.StringIO()
        compile_ai_logs.scan_codex(str(home), "current", current, compile_ai_logs.Stats(), [], {})
        coverage = []
        archived = list(installation_history.usage({"prior-old": home}, coverage))
        self.assertEqual([json.loads(row)["total"] for row in current.getvalue().splitlines()], [9])
        self.assertEqual([row["total"] for row in archived], [9])
        self.assertEqual(coverage[0]["status"], "scanned")

    def test_selected_mount_and_linked_ancestor_are_distinguished(self):
        mount = self.root / "selected"
        profile = mount / "Users"
        profile.mkdir(parents=True)
        with patch.object(installation_history, "_link", side_effect=lambda p: p == mount), \
                patch.object(installation_history.os.path, "ismount", side_effect=lambda p: p == mount):
            self.assertEqual(installation_history._selected_status(mount), "readable")
            self.assertEqual(installation_history._selected_status(profile), "readable")
        with patch.object(installation_history, "_link", side_effect=lambda p: p == mount), \
                patch.object(installation_history.os.path, "ismount", return_value=False):
            self.assertEqual(installation_history._selected_status(profile), "linked_path")

    def test_store_append_after_interval_read_is_absent_from_both_sections(self):
        store_path = str(self.root / "ledger.sqlite")
        write_json(self.root / "pricing.json", {"models": {}})
        rows = self.root / "calls.jsonl"
        rows.write_text(json.dumps(event("host", S1, 9)) + "\n", encoding="utf-8")
        config = {"store": {"kind": "sqlite", "path": store_path}, "workdir": str(self.root),
                  "timezone": "UTC", "pricing_path": str(self.root / "pricing.json")}
        with closing(ledger_store.Store.open("sqlite", store_path)):
            pass
        collect = scoped_report.collect

        def append_after_read(*args, **kwargs):
            interval = collect(*args, **kwargs)
            with closing(ledger_store.Store.open("sqlite", store_path)) as store:
                self.assertEqual(store.ingest_events_file(rows), (1, 0))
            return interval

        with patch.object(scoped_report, "collect", side_effect=append_after_read):
            _, data = scoped_report.generate_report(config, "2026-01-01", DAY3, out=str(self.root / "report"),
                                                   now=NOW, lifetime=[input_file(
                                                       self.root / "counter.json", [snapshot("a", 12)], [evidence("a", "host")])])
        self.assertEqual(data["totals"]["total"], 0)
        self.assertEqual(data["lifetime"]["observed"]["total"], 0)

    def test_prior_install_name_cannot_match_a_recorded_host(self):
        archive = self.root / "earlier/.codex"
        rollout(archive, S1, 9)
        rows = self.root / "calls.jsonl"
        rows.write_text(json.dumps(event("Prior-Old", S2, 99)) + "\n", encoding="utf-8")
        store_path = str(self.root / "ledger.sqlite")
        with closing(ledger_store.Store.open("sqlite", store_path)) as store:
            self.assertEqual(store.ingest_events_file(rows), (1, 0))
        config = {"store": {"kind": "sqlite", "path": store_path}, "timezone": "UTC",
                  "pricing_path": str(write_json(self.root / "pricing.json", {"models": {}}))}
        report = self.root / "report"
        counters = input_file(self.root / "input.json", [snapshot("a", 9)], [evidence("a", "prior-early")])
        with self.assertRaisesRegex(ValueError, "conflicts with a recorded host"):
            scoped_report.generate_report(config, "2026-01-02", DAY3, out=report, now=NOW,
                                          lifetime=[counters], prior_installs=[f"old={archive}"])
        self.assertFalse(report.exists())
        _, data = scoped_report.generate_report(config, "2026-01-02", DAY3, out=report, now=NOW,
                                               lifetime=[counters], prior_installs=[f"early={archive}"])
        self.assertEqual(data["lifetime"]["accounts"][0]["lifetime"]["confirmed_tokens"], 9)
        self.assertEqual(data["lifetime"]["observed"]["unattributed_total"], 99)

    def test_zstandard_is_scanned_or_reported_unavailable(self):
        home = self.root / ".codex"
        path = rollout(home, S1, 9)
        zstd = installation_history.zstd
        payload = zstd.compress(path.read_bytes()) if zstd else b"fixture"
        path.with_suffix(".jsonl.zst").write_bytes(payload)
        path.unlink()
        coverage = []
        rows = list(installation_history.usage({"prior-zstd": home}, coverage))
        if zstd:
            self.assertEqual([row["total"] for row in rows], [9])
            self.assertEqual(coverage[0]["status"], "scanned")
        else:
            self.assertEqual(rows, [])
            self.assertEqual(coverage[0]["problems"][0]["status"], "zstandard_unavailable")

    def test_windows_inventory_and_unmounted_status(self):
        # Windows drive strings are relative names on POSIX. Mock only the
        # directory probes to exercise the same marker decisions on every OS.
        volumes = [{"DriveLetter": "C:"}, {"DriveLetter": "D:"},
                   {"DriveLetter": None, "DeviceID": "volume-fixture", "FileSystem": "NTFS", "Capacity": 42}]
        path_status = {"c:/windows.old": "readable", "c:/windows.old/users": "readable",
                       "d:": "readable", "d:/users": "access_denied"}

        def directories(path):
            key = str(path).replace("\\", "/").rstrip("/").replace("//", "/").lower()
            return path_status.get(key, "missing_or_unmounted")

        with patch.object(installation_history, "_directory", side_effect=directories), \
                patch.object(installation_history.os, "scandir") as fake:
            fake.return_value.__enter__.return_value = iter(())
            results = installation_history.discover_windows(volumes, "C:")
        self.assertEqual([row["status"] for row in results], ["discovered", "discovered", "unverified_unmounted"])
        self.assertEqual(results[1]["profiles"], "access_denied")

    def test_cli_prior_install_copies_and_all_store_backends(self):
        current = self.root / "current/.codex"
        old = self.root / "Windows.old"
        old_home = old / "Users/synthetic/.codex"
        current_call = rollout(current, S1, 9)
        prior_copy = rollout(old_home, S1, 9)
        prior_unique = rollout(old_home, S2, 17, compressed=True)
        self.assertEqual(current_call.read_bytes(), prior_copy.read_bytes())
        original_sources = {path: path.read_bytes() for path in (current_call, prior_copy, prior_unique)}
        ledger_event = event("current", S1, 9, src=str(current_call))
        event_file = self.root / "events.jsonl"
        event_file.write_text(json.dumps(ledger_event) + "\n", encoding="utf-8")
        counters = input_file(self.root / "inputs.json", [snapshot("account", 30)],
                              [evidence("account", "current"), evidence("account", "prior-early")])
        prices = write_json(self.root / "pricing.json", {"models": {}})
        for backend in ("sqlite", "json", "csv"):
            with self.subTest(backend=backend):
                store_path = str(self.root / ("db-" + backend))
                with closing(ledger_store.Store.open(backend, store_path)) as store:
                    self.assertEqual(store.ingest_events_file(event_file), (1, 0))
                config = {"store": {"kind": backend, "path": store_path}, "workdir": str(self.root),
                          "timezone": "UTC", "pricing_path": str(prices)}
                report_dir = self.root / ("report-" + backend)
                values = [f"early={old}", f"missing={self.root / 'unmounted'}"]
                if backend == "sqlite":
                    write_json(self.root / "config.json", config)
                    command = [sys.executable, "-B", str(ROOT / "scripts/ledger.py"), "report", "--since", "2026-01-02",
                               "--until", DAY3, "--out", str(report_dir), "--lifetime", str(counters)]
                    for value in values:
                        command += ["--prior-install", value]
                    result = subprocess.run(command, env={**os.environ, "AI_USAGE_LEDGER_HOME": str(self.root)},
                                            capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    data = json.loads((report_dir / "report-data.json").read_text(encoding="utf-8"))
                else:
                    _, data = scoped_report.generate_report(config, "2026-01-02", DAY3, out=str(report_dir),
                                                           now=NOW, lifetime=[counters], prior_installs=values)
                self.assertEqual(data["totals"]["total"], 0)
                report = data["lifetime"]
                self.assertEqual(report["observed"]["total"], 26)
                self.assertEqual(report["accounts"][0]["lifetime"]["difference_tokens"], 4)
                self.assertEqual(report["installation_sources"][1]["status"], "missing_or_unmounted")
                self.assertEqual(report["duplicate_sources"][0]["copies_removed"], 1)
                self.assertIn("Codex lifetime reconciliation", (report_dir / "report.html").read_text(encoding="utf-8"))
                checksums = (report_dir / "SHA256SUMS").read_text(encoding="utf-8")
                self.assertIn(hashlib.sha256((report_dir / "report-data.json").read_bytes()).hexdigest(), checksums)
        self.assertEqual({path: path.read_bytes() for path in original_sources}, original_sources)


if __name__ == "__main__":
    unittest.main()
