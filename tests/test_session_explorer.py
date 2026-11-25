import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def write_csv(path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        writer.writerows(rows)


class SessionExplorerTests(unittest.TestCase):
    def test_catalog_preserves_usage_and_inventory_only_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            fields = ("tool", "host", "session", "calls", "total", "output")
            same_id = "shared-session"
            inventory_id = "retained-session</script>"
            usage = [
                ("codex", "windows", same_id, 9, 441, 8),
                ("codex", "wsl", same_id, 2, 17, 4),
                *[("codex", "windows", f"thread-{index:03d}", 1, index, 1) for index in range(1, 58)],
            ]
            write_csv(folder / "by_session.csv", fields, usage)
            catalog = [
                ("codex", "windows", same_id, "2026-04-01T11:00:00Z", "2026-04-11T11:00:00Z", "private title"),
                ("codex", "windows", same_id, "2026-03-01T11:00:00Z", "2026-04-14T11:00:00Z", "copy title"),
                ("codex", "wsl", inventory_id, "", "", "private inventory title"),
            ]
            write_csv(folder / "all_sessions.csv", ("tool", "host", "session", "first_ts", "last_ts", "title"), catalog)
            summary = dict.fromkeys(("by_tool", "by_tool_host", "by_model", "by_month_tool", "by_day_tool",
                                     "by_kind", "by_entrypoint", "by_project", "top_sessions", "inventory"), [])
            summary.update(generated="2026-04-20", events=68, sessions=59, prompts=0, total={})
            (folder / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
            out = folder / "dashboard.html"
            subprocess.run([sys.executable, str(ROOT / "scripts/build_dashboard.py"), str(folder / "summary.json"), str(out)],
                           check=True, capture_output=True)
            page = out.read_text(encoding="utf-8")
            data = json.loads(page.split("const D = ", 1)[1].split(";\nconst TOOL", 1)[0])
            self.assertEqual(len(data["session_index"]), 60)
            rows = {(row["host"], row["session"]): row for row in data["session_index"]}
            self.assertEqual(rows["windows", same_id]["first"], "2026-03-01T11:00:00Z")
            self.assertEqual(rows["windows", same_id]["last"], "2026-04-14T11:00:00Z")
            self.assertEqual(rows["windows", same_id]["total"], 441)
            self.assertEqual(rows["wsl", same_id]["total"], 17)
            self.assertEqual(rows["wsl", inventory_id]["calls"], 0)
            self.assertNotIn("retained-session</script>", page)
            self.assertNotIn("private title", page)
            self.assertIn("id=\"sessionNext\"", page)


if __name__ == "__main__":
    unittest.main()
