import contextlib
import gzip
import io
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import compile_ai_logs as scanner  # noqa: E402
import ledger_store  # noqa: E402
import native_sources  # noqa: E402
import run_pipeline  # noqa: E402
import source_streams  # noqa: E402


def jsonl(rows):
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows).encode()


def rows(buffer):
    return [json.loads(line) for line in buffer.getvalue().splitlines()]


class ExtendedSources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_compressed_rollout_representations_share_identity_and_preserve_unique_tail(self):
        stamp = "2026-01-01T00:00:00Z"
        first = {"type": "session_meta", "timestamp": stamp, "payload": {"cwd": "/repo", "id": "session"}}

        def usage(ts, total):
            return {"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count",
                    "info": {"total_token_usage": {"total_tokens": total},
                             "last_token_usage": {"input_tokens": 8, "cached_input_tokens": 5, "output_tokens": 2}}}}

        name = "rollout-2026-01-01T00-00-00-aaaa0000-0000-0000-0000-000000000001.jsonl"
        mirror = self.root / "archived_sessions" / (name + ".gz")
        plain = self.root / "sessions" / name
        mirror.parent.mkdir()
        plain.parent.mkdir()
        one = usage("2026-01-01T00:00:01Z", 10)
        second = usage("2026-01-01T00:00:02Z", 20)
        plain.write_bytes(jsonl([first, one]))
        mirror.write_bytes(gzip.compress(jsonl([first, one, second])))
        output, stats, sessions = io.StringIO(), scanner.Stats(), []
        scanner.scan_codex(str(self.root), "fixture", output, stats, sessions, {})
        events = rows(output)
        self.assertEqual((stats.files, stats.events, stats.dedup, stats.read_errors), (2, 2, 1, 0))
        self.assertEqual({entry["session"] for entry in events}, {"aaaa0000-0000-0000-0000-000000000001"})
        self.assertEqual(ledger_store.event_id(events[0], 1), ledger_store.event_id(dict(events[0], src=str(mirror)), 1))
        self.assertEqual(ledger_store.session_id(sessions[0]), ledger_store.session_id(sessions[1]))
        self.assertTrue(events[-1]["archived"])

    @unittest.skipUnless(source_streams.zstd is not None or shutil.which("zstd"), "Zstandard decoder unavailable")
    def test_zstandard_reader_streams_valid_data(self):
        contents = b'{"foo":1}\n{"foo":2}\n'
        if source_streams.zstd is not None:
            compressed = source_streams.zstd.compress(contents)
        else:
            compressed = subprocess.run(["zstd", "-q", "-c"], input=contents, capture_output=True, check=True).stdout
        fixture = self.root / "session.jsonl.zst"
        fixture.write_bytes(compressed)
        with source_streams.open_lines(fixture) as stream:
            self.assertEqual(b"".join(stream), contents)

    def test_missing_zstandard_reader_reports_an_error(self):
        fixture = self.root / "session.jsonl.zst"
        fixture.write_bytes(b"unused")
        with patch.object(source_streams, "zstd", None), patch.object(source_streams.shutil, "which", return_value=None):
            with self.assertRaisesRegex(OSError, "Zstandard input requires"):
                with source_streams.open_lines(fixture):
                    self.fail("unexpected reader")

    def test_desktop_and_cli_share_native_message_identity(self):
        desktop = self.root / "desktop"
        cli = self.root / "projects" / "project"
        desktop.mkdir()
        cli.mkdir(parents=True)
        message = {"id": "same", "model": "claude-test", "usage": {"input_tokens": 11, "output_tokens": 4}}
        (cli / "session.jsonl").write_bytes(jsonl([{"type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                                                  "message": message, "sessionId": "cli"}]))
        (desktop / "audit.jsonl").write_bytes(jsonl([
            {"type": "assistant", "_audit_timestamp": 1767225600, "session_id": "desktop", "message": message},
            {"type": "assistant", "_audit_timestamp": 1767225601, "session_id": "desktop", "message": dict(message, id="new")},
        ]))
        output, stats, sessions, seen = io.StringIO(), scanner.Stats(), [], set()
        scanner.scan_claude(str(self.root), "fixture", output, stats, seen, sessions)
        scanner.scan_claude(str(desktop), "fixture", output, stats, seen, sessions, desktop=True)
        self.assertEqual((stats.events, stats.dedup), (2, 1))
        latest = rows(output)[1]
        self.assertEqual((latest["session"], latest["entrypoint"], latest["date"]), ("desktop", "claude-desktop", "2026-01-01"))

    def test_opencode_v2_adds_messages_and_skips_legacy_ids(self):
        path = self.root / "adapter #2.sqlite"
        with contextlib.closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE session(id, directory, title, model, time_created, time_updated, cost);"
                "CREATE TABLE message(id PRIMARY KEY, session_id, time_created, data);"
                "CREATE TABLE session_message(id PRIMARY KEY, session_id, type, time_created, data);"
            )
            v1 = {"role": "assistant", "providerID": "example", "modelID": "model",
                  "tokens": {"input": 10, "output": 5, "cache": {"read": 2}}}
            v2 = {"model": {"providerID": "example", "id": "model2"},
                  "tokens": {"input": 9, "output": 3, "cache": {"read": 11}}}
            db.execute("INSERT INTO message VALUES(?,?,?,?)", ("same", "one", 1767225600000, json.dumps(v1)))
            db.executemany("INSERT INTO session_message VALUES(?,?,?,?,?)",
                           [(mid, "two", "assistant", 1767225600000, json.dumps(v2)) for mid in ("same", "new")])
        output, stats, sessions = io.StringIO(), scanner.Stats(), []
        counters = scanner.scan_opencode(str(path), "fixture", output, stats, sessions, scanner._NullWriter())
        self.assertEqual(counters, {"legacy_events": 1, "v2_events": 1, "matching_message_ids_skipped": 1})
        item = rows(output)[1]
        self.assertEqual((item["model"], item["cache_read"], item["request_id"]), ("example/model2", 11, "opencode-message:new"))

    def test_dsh_counts_completed_steps_once_and_preserves_cache_split(self):
        completed = {"type": "assistant/message", "surfaceOp": "append", "time": 1767225600000,
                     "data": {"turn": 0, "step": 2, "message": {"source": {"model": "deepseek-test"}},
                              "usage": {"inputTokens": 2, "outputTokens": 8, "cacheReadTokens": 100, "reasoningTokens": 5}}}
        stream_copy = {"type": "agent/chunk", "data": {"chunk": {"type": "usage", "usage": completed["data"]["usage"]}}}
        (self.root / "test.jsonl").write_bytes(jsonl([
            {"type": "session", "version": 0, "id": "probe", "createdAt": 1767225600000}, stream_copy, completed, completed,
        ]))
        output, counters, meta = io.StringIO(), scanner.Stats(), []
        scanner.scan_dsh(str(self.root), "fixture", output, counters, meta)
        self.assertEqual((counters.events, counters.dedup, counters.errors), (1, 1, 0))
        item = rows(output)[0]
        self.assertEqual((item["input_uncached"], item["cache_read"], item["total"], item["model"]), (2, 100, 110, "deepseek-test"))

    def test_database_and_paseo_mirror_counters_are_separate_from_events(self):
        db_path = self.root / "counter.sqlite"
        with contextlib.closing(sqlite3.connect(db_path)) as db, db:
            db.executescript(
                "CREATE TABLE session_model_usage(session_id, model, api_call_count, input_tokens, output_tokens);"
                "INSERT INTO session_model_usage VALUES('one','example',4,20,10);"
                "INSERT INTO session_model_usage VALUES('one','other',1,7,3);"
            )
        path = self.root / "fixture.tar.gz"
        with tarfile.open(path, "w:gz") as archive:
            evidence = jsonl([{"type": "assistant", "message": {"id": "same",
                             "usage": {"input_tokens": 5, "output_tokens": 4}}}] * 2)
            member = tarfile.TarInfo("transcripts/session/fixture.jsonl")
            member.size = len(evidence)
            archive.addfile(member, io.BytesIO(evidence))
        self.assertEqual(native_sources.native_counters("hermes", db_path)["totals"]["api_calls"], 5)
        self.assertEqual(native_sources.paseo_archive_counters(path)["totals"]["rows"], 1)
        host = {"name": "fixture", "native_counter_dbs": ["hermes=" + str(db_path)], "paseo_roots": [str(self.root)]}
        output = self.root / "result"
        subprocess.run([sys.executable, scanner.__file__, *run_pipeline.scan_args(host), "--out-dir", str(output)],
                       capture_output=True, check=True)
        receipt = json.loads((output / "inventory.fixture.json").read_text())
        self.assertEqual(receipt["events"], 0)
        self.assertEqual({row["source_kind"] for row in receipt["roots"]}, {"native-counter", "archive-counter"})
        self.assertFalse((output / "prompts.fixture.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
