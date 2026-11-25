"""Synthetic coverage for the public census command and private read-only evidence."""

from contextlib import closing
import gzip
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from census_sources import archive_member, blocked_file  # noqa: E402
from session_census import database_counts  # noqa: E402


WHEN = "2026-02-10T12:00:00+00:00"
LIVE = "00000000-0000-4000-8000-000000000001"
RETAINED = "00000000-0000-4000-8000-000000000002"
PROMPT = "synthetic content that must stay in its source"


def rollout(identifier, stamp, tokens):
    rows = [
        {"type": "session_meta", "timestamp": stamp, "payload": {"id": identifier}},
        {"type": "event_msg", "timestamp": stamp,
         "payload": {"type": "token_count", "info": {
             "total_token_usage": {"total_tokens": tokens + 2},
             "last_token_usage": {"input_tokens": tokens, "cached_input_tokens": 3, "output_tokens": 2},
         }}},
    ]
    return ("\n".join(json.dumps(row) for row in rows) + "\n").encode()


def run_census(*args):
    return subprocess.run([sys.executable, "-B", str(SCRIPTS / "ledger.py"), "census", *map(str, args)],
                          capture_output=True, text=True, encoding="utf-8", check=True)


class SessionCensusTests(unittest.TestCase):
    def test_local_metadata_private_output_and_archived_call_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "older install"
            codex = root / "alternate" / ".codex" / "sessions"
            claude = root / "old profile" / ".claude" / "projects" / "fixture-project"
            codex.mkdir(parents=True)
            claude.mkdir(parents=True)
            (codex.parent / "auth.json").write_text(PROMPT, encoding="utf-8")
            known_leaf = f"rollout-2026-02-10T12-00-00-{LIVE}.jsonl"
            old_leaf = f"rollout-2026-02-10T12-00-01-{RETAINED}.jsonl"
            (codex / known_leaf).write_bytes(rollout(LIVE, WHEN, 8))
            (claude / "unusual.jsonl").write_text(json.dumps({
                "type": "user", "sessionId": "sample-claude", "message": {"content": PROMPT},
            }) + "\n", encoding="utf-8")
            image = root / "retained.vhdx"
            image.write_bytes(b"synthetic image payload")
            archive = root / "saved.zip"
            with zipfile.ZipFile(archive, "w") as z:
                z.writestr("active/.codex/sessions/" + known_leaf, rollout(LIVE, WHEN, 8))
                older = rollout(RETAINED, "2026-02-10T12:00:01+00:00", 13)
                z.writestr("earlier/.codex/archived_sessions/" + old_leaf + ".gz", gzip.compress(older))
                z.writestr("mirror/.codex/sessions/" + old_leaf, older)
                z.writestr("../escape/" + old_leaf, PROMPT)
            ledger = base / "metered.sqlite"
            with closing(sqlite3.connect(ledger)) as db:
                db.executescript("""
                    CREATE TABLE events(tool, session, ts, total, output, input_uncached, cache_read, cache_write,
                        reasoning);
                    CREATE INDEX events_ts ON events(ts);
                    CREATE TABLE sessions(tool, session);
                """)
                db.execute("INSERT INTO events VALUES ('codex', ?, ?, 10, 2, 5, 3, 0, 0)", (LIVE, WHEN))
                db.execute("INSERT INTO sessions VALUES ('codex', ?)", (LIVE,))
                db.commit()
            output = base / "results"
            run_census("--root", root, "--output", output, "--archive", archive, "--ledger", ledger,
                       "--inspect-archives")
            report = json.loads((output / "census.json").read_text(encoding="utf-8"))
            self.assertEqual(report["sources"][0]["candidate_paths"], 4)
            self.assertEqual(report["harnesses"]["claude-code"]["distinct_transcript_ids"], 1)
            self.assertEqual(report["harnesses"]["codex"]["distinct_transcript_ids"], 1)
            retained = report["archives"][0]["codex"]
            self.assertEqual((retained["rollout_members"], retained["distinct_session_ids"]), (3, 2))
            self.assertEqual((retained["matched_calls"], retained["unmatched_calls"], retained["unmatched_tokens"]),
                             (1, 1, 15))
            self.assertEqual(retained["unmatched_session_ids"], 1)
            self.assertEqual(report["ledger"][0]["tokens"], 10)
            for artifact in output.iterdir():
                text = artifact.read_text(encoding="utf-8")
                self.assertNotIn(PROMPT, text)
                self.assertNotIn("auth.json", text)
            self.assertEqual(image.read_bytes(), b"synthetic image payload")
            with self.assertRaises(subprocess.CalledProcessError):
                run_census("--root", root, "--output", output)

    def test_tar_headers_are_read_by_explicit_archive_option(self):
        self.assertEqual(archive_member("./transcripts/sample.jsonl"), ("paseo", "transcript candidate"))
        self.assertIsNone(archive_member("tests/seed/rollout-2026-02-10T12-00-00-" + LIVE + ".jsonl"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / f"rollout-2026-02-10T12-00-00-{LIVE}.jsonl"
            source.write_bytes(rollout(LIVE, WHEN, 8))
            package = path / "backup.tar.gz"
            with tarfile.open(package, "w:gz") as archive:
                archive.add(source, arcname=source.name)
            output = path / "results"
            run_census("--root", path, "--output", output, "--archive", package)
            j = json.loads((output / "census.json").read_text(encoding="utf-8"))
            self.assertEqual(j["archives"][0]["codex"]["parsed_tokens"], 10)
            self.assertNotIn("unmatched_calls", j["archives"][0]["codex"])

    def test_reading_wal_does_not_change_source_files(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session-store.db"
            db = sqlite3.connect(source)
            try:
                self.assertEqual(db.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
                db.execute("PRAGMA wal_autocheckpoint=0")
                db.execute("CREATE TABLE sessions (id)")
                db.execute("INSERT INTO sessions VALUES (1)")
                db.commit()
                before = {path.name: path.read_bytes() for path in source.parent.iterdir()}
                self.assertEqual(database_counts(source), {"sessions": 1})
                self.assertEqual({path.name: path.read_bytes() for path in source.parent.iterdir()}, before)
            finally:
                db.close()

    def test_explicitly_recognizes_offline_file_attributes(self):
        from types import SimpleNamespace

        meta = SimpleNamespace(st_file_attributes=0x40000, st_mode=0)
        self.assertEqual(blocked_file(meta), "offline placeholder")


if __name__ == "__main__":
    unittest.main()
