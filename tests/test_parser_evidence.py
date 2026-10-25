"""Synthetic regressions for preserving native parser usage and cost evidence."""

import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("evidence_compiler", ROOT / "scripts/compile_ai_logs.py")
assert SPEC and SPEC.loader
compiler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compiler)


class ParserEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    @staticmethod
    def claude_row(*, message_id=None, uuid=None, second=1):
        row = {
            "type": "assistant", "sessionId": "synthetic-session",
            "timestamp": f"2026-10-25T00:00:{second:02d}Z",
            "message": {"model": "synthetic", "usage": {"input_tokens": 10, "output_tokens": 2}},
        }
        if message_id is not None:
            row["message"]["id"] = message_id
        if uuid is not None:
            row["uuid"] = uuid
        return row

    def scan_claude(self, files):
        for name, rows in files.items():
            self.write(self.root / "projects" / "fixture" / name, rows)
        output = io.StringIO()
        stats = compiler.Stats()
        seen = set()
        compiler.scan_claude(str(self.root), "fixture", output, stats, seen, [])
        return [json.loads(line) for line in output.getvalue().splitlines()], stats

    def test_claude_missing_ids_do_not_collapse_distinct_usage(self):
        rows, stats = self.scan_claude({"session.jsonl": [self.claude_row(), self.claude_row(second=2)]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["total"] for row in rows), 24)
        self.assertEqual(stats.dedup, 0)

    def test_claude_missing_ids_do_not_collapse_across_files(self):
        rows, stats = self.scan_claude({
            "one.jsonl": [self.claude_row()], "two.jsonl": [self.claude_row(second=2)],
        })
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats.dedup, 0)

    def test_claude_empty_ids_do_not_identify_a_replay(self):
        rows, stats = self.scan_claude({
            "session.jsonl": [self.claude_row(message_id="", uuid=""),
                              self.claude_row(message_id="", uuid="", second=2)],
        })
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats.dedup, 0)

    def test_claude_message_ids_still_deduplicate_across_files(self):
        rows, stats = self.scan_claude({
            "one.jsonl": [self.claude_row(message_id="native-id")],
            "two.jsonl": [self.claude_row(message_id="native-id", second=2)],
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(stats.dedup, 1)

    def test_claude_uuid_fallback_still_deduplicates(self):
        rows, stats = self.scan_claude({"session.jsonl": [
            self.claude_row(uuid="native-uuid"), self.claude_row(uuid="native-uuid", second=2),
            self.claude_row(uuid="other-uuid", second=3),
        ]})
        self.assertEqual(len(rows), 2)
        self.assertEqual(stats.dedup, 1)

    def test_openclaw_and_pi_preserve_zero_positive_and_unknown_costs(self):
        for tool in ("openclaw", "pi"):
            with self.subTest(tool=tool):
                rows = []
                for index, cost in enumerate(({"total": 0}, {"total": 0.125}, {"total": None}, None, {})):
                    usage = {"input": 10, "output": 2}
                    if index != 4:
                        usage["cost"] = cost
                    rows.append({
                        "type": "message", "id": f"synthetic-{index}",
                        "timestamp": f"2026-10-25T00:00:{index:02d}Z",
                        "message": {"role": "assistant", "provider": "synthetic", "model": "synthetic", "usage": usage},
                    })
                self.write(self.root / "agents" / "fixture" / "sessions" / "session.jsonl", rows)
                output = io.StringIO()
                stats = compiler.Stats()
                compiler.scan_openclaw(str(self.root), "fixture", output, stats, [], io.StringIO(), tool=tool)
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertEqual([row["cost_usd"] for row in events], [0, 0.125, None, None, None])
                self.assertEqual([row["total"] for row in events], [12] * 5)
                self.assertEqual(stats.events, 5)


if __name__ == "__main__":
    unittest.main()
