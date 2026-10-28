"""Full-content export contracts. Every input is synthetic and explicitly named."""
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import conversation_export as conversation  # noqa: E402


def codex_message(role, text):
    return {"type": "response_item", "timestamp": "2026-10-27T12:00:00Z", "payload": {
        "type": "message", "role": role, "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


class ConversationExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, value, name="conversation.json"):
        path = self.root / name
        data = json.dumps(value) if path.suffix == ".json" else "\n".join(json.dumps(row) for row in value) + "\n"
        path.write_text(data, encoding="utf-8")
        return path

    def test_codex_full_ordered_messages_tools_and_mirrors(self):
        rows = [{"type": "session_meta", "payload": {"id": "conversation-1"}},
                {"type": "event_msg", "payload": {"type": "user_message", "id": "user-1", "message": "repeat"}},
                codex_message("user", "repeat"), codex_message("assistant", "I will inspect it."),
                {"type": "event_msg", "payload": {"type": "agent_message", "id": "assistant-1", "message": "I will inspect it."}},
                {"type": "response_item", "payload": {"type": "function_call", "name": "shell", "call_id": "call-1", "arguments": '{"cmd":"synthetic"}'}},
                {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "call-1", "output": "complete tool output\nsecond line"}},
                codex_message("user", "repeat"), codex_message("assistant", "Finished."),
                {"type": "event_msg", "payload": {"type": "token_count", "info": {}}}]
        rows[2]["payload"]["id"] = "user-1"
        rows[3]["payload"]["id"] = "assistant-1"
        source = self.source(rows, "rollout.jsonl")
        original = source.read_bytes()
        result = conversation.gather([source])
        messages = result["messages"]
        self.assertEqual([row["role"] for row in messages], ["user", "assistant", "assistant", "tool", "user", "assistant"])
        self.assertEqual(messages[2]["content"]["arguments"], '{"cmd":"synthetic"}')
        self.assertEqual(messages[3]["content"], "complete tool output\nsecond line")
        self.assertEqual(sum(conversation.text_content(row["content"]) == "repeat" for row in messages), 2)
        self.assertTrue(all(row["session"] == "conversation-1" for row in messages))
        self.assertEqual(result["sources"][0]["ignored_metadata"]["mirrored_ui_message"], 2)
        self.assertEqual(source.read_bytes(), original)

    def test_codex_ambiguous_echoes_and_multimodal_data_are_not_discarded(self):
        rows = [{"type": "session_meta", "payload": {"id": "one"}},
                {"type": "event_msg", "timestamp": "2026-10-27T12:00:00Z", "payload": {
                    "type": "user_message", "message": "repeat", "local_images": ["/synthetic/image.png"]}},
                codex_message("user", "repeat"),
                {"type": "event_msg", "timestamp": "2026-10-27T12:01:00Z", "payload": {"type": "user_message", "message": "repeat"}},
                {"type": "session_meta", "payload": {"id": "two"}}, codex_message("assistant", "second session")]
        rows[2]["timestamp"] = "2026-10-27T12:01:00Z"
        rows[2]["payload"]["content"].append({"type": "input_image", "image_url": "https://example.invalid/image"})
        source = self.source(rows, "multimodal.jsonl")
        result = conversation.gather([source])
        self.assertEqual(len(result["messages"]), 4)
        self.assertEqual(result["messages"][0]["timestamp"], "2026-10-27T12:00:00Z")
        self.assertEqual(result["messages"][0]["local_images"], ["/synthetic/image.png"])
        self.assertEqual(result["messages"][1]["content"][1]["type"], "input_image")
        self.assertEqual(conversation.gather([source], session="two")["messages"][0]["content"][0]["text"], "second session")

    def test_claude_preserves_all_content_blocks_and_branch_ids(self):
        blocks = [{"type": "text", "text": "Checking"}, {"type": "thinking", "thinking": "Recorded summary"},
                  {"type": "tool_use", "id": "tool-1", "name": "Read", "input": {"file_path": "synthetic.txt"}}]
        rows = [{"type": "user", "sessionId": "claude-1", "uuid": "u1", "message": {"role": "user", "content": "Please check"}},
                {"type": "assistant", "sessionId": "claude-1", "uuid": "a1", "parentUuid": "u1", "message": {"role": "assistant", "content": blocks}},
                {"type": "user", "sessionId": "claude-1", "uuid": "u2", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool-1", "content": "entire result"}]}}]
        result = conversation.gather([self.source(rows, "claude.jsonl")])
        self.assertEqual(result["messages"][1]["content"], blocks)
        self.assertEqual(result["messages"][1]["parentUuid"], "u1")
        self.assertEqual(result["messages"][2]["content"][0]["tool_use_id"], "tool-1")

    def test_pi_and_messages_envelopes(self):
        pi = self.source([{"type": "session", "id": "pi-1"}, {"type": "model_change", "modelId": "fixture"},
                          {"type": "message", "id": "p1", "parentId": "parent", "message": {"role": "assistant", "content": "hello"}},
                          {"type": "message", "id": "p2", "message": {"role": "toolResult", "toolCallId": "tc1", "toolName": "read", "content": [{"type": "text", "text": "tool result"}],
                           "details": {"exitCode": 1}, "isError": True, "durationMs": 25, "timestamp": 123,
                           "nestedCalls": [{"name": "nested", "result": "recorded"}]}}], "pi.jsonl")
        result = conversation.gather([pi])
        self.assertEqual(result["messages"][0]["session"], "pi-1")
        self.assertEqual(result["messages"][0]["parentId"], "parent")
        self.assertEqual(result["messages"][1]["role"], "tool")
        self.assertEqual(result["messages"][1]["toolCallId"], "tc1")
        for key in ("details", "isError", "durationMs", "nestedCalls"):
            self.assertEqual(result["messages"][1][key], json.loads(pi.read_text().splitlines()[-1])["message"][key])
        self.assertEqual(result["messages"][1]["message_timestamp"], 123)
        generic = self.source({"sessionId": "generic-1", "messages": [
            {"role": "system", "content": "synthetic instructions"},
            {"type": "gemini", "content": [{"text": "response"}], "thoughts": [{"text": "stored thought"}]},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "test", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "tool response"}]})
        messages = conversation.gather([generic])["messages"]
        self.assertEqual([row["role"] for row in messages], ["system", "assistant", "assistant", "tool"])
        self.assertEqual(messages[1]["thoughts"], [{"text": "stored thought"}])

    def test_codex_builtin_tool_type_and_message_phase_are_retained(self):
        rows = [{"type": "response_item", "payload": {"type": "web_search_call", "id": "w1", "action": {"query": "synthetic"}}},
                {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": "answer"}}]
        messages = conversation.gather([self.source(rows, "builtin.jsonl")])["messages"]
        self.assertEqual(messages[0]["native_type"], "web_search_call")
        self.assertEqual(messages[1]["phase"], "final_answer")

    def test_standard_reasoning_and_citation_content_is_preserved(self):
        messages = [{"role": "assistant", "content": "Answer", "reasoning_content": "Recorded summary",
                     "annotations": [{"type": "url_citation", "url": "https://example.invalid", "title": "Source"}],
                     "citations": ["https://example.invalid"]},
                    {"role": "assistant", "reasoning": {"summary": "Only recorded reasoning"}},
                    {"role": "assistant", "content": None, "audio": {"id": "audio-1", "transcript": "Recorded audio"}},
                    {"role": "assistant", "toolCalls": [{"name": "read", "args": {"path": "example"}}]}]
        result = conversation.gather([self.source(messages)])["messages"]
        for index, source in enumerate(messages):
            for key, value in source.items():
                self.assertEqual(result[index][key], value)

    def test_compressed_sources_and_repeated_path_handling(self):
        source = self.source([{"role": "user", "content": "hello"}])
        compressed = source.with_name("archive.json.gz")
        compressed.write_bytes(gzip.compress(source.read_bytes()))
        result = conversation.gather([source, source, compressed])
        self.assertEqual(len(result["messages"]), 2)
        self.assertEqual(len(result["sources"]), 2)
        self.assertNotEqual(result["messages"][0]["session"], result["messages"][1]["session"])
        self.assertEqual(result["sources"][0]["sha256_uncompressed"], hashlib.sha256(source.read_bytes()).hexdigest())

    def test_malformed_type_tags_and_roles_are_coverage_gaps(self):
        rows = [codex_message("user", "keep"), {"type": [], "payload": {}},
                {"type": "response_item", "payload": {"type": []}},
                {"type": "response_item", "payload": {"type": "message", "role": [], "content": "bad"}},
                [{"role": "user", "content": "wrong format"}]]
        result = conversation.gather([self.source(rows, "malformed.jsonl")], allow_partial=True)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(len(result["coverage"]["gaps"]), 4)

    def test_corrupt_gzip_and_nonfinite_json_fail_without_output(self):
        compressed = self.root / "broken.json.gz"
        compressed.write_bytes(bytes.fromhex("1f8b08000000000000ff") + b"garbage")
        source = self.root / "nonfinite.json"
        source.write_text('[{"role":"user","content":"secret","timestamp":1e400}]')
        for path in (compressed, source):
            out = self.root / (path.name + "-out")
            error = io.StringIO()
            with mock.patch("sys.stderr", error):
                self.assertEqual(conversation.main(["export", "--source", str(path), "--out", str(out)]), 1)
            self.assertFalse(out.exists())
            self.assertNotIn("Traceback", error.getvalue())
            self.assertNotIn("secret", error.getvalue())

    def test_session_filter_and_native_repeated_turns(self):
        rows = [{"role": "user", "session_id": "one", "content": "same"},
                {"role": "user", "session_id": "one", "content": "same"},
                {"role": "assistant", "session_id": "two", "content": "different"}]
        source = self.source(rows)
        messages = conversation.gather([source], session="one")["messages"]
        self.assertEqual(len(messages), 2)
        with self.assertRaises(conversation.ConversationError):
            conversation.gather([source], session="missing")

    def test_partial_corrupt_compacted_and_unknown_records_are_explicit(self):
        source = self.source([codex_message("user", "kept"), {"type": "compacted"}], "partial.jsonl")
        with source.open("a") as stream:
            stream.write('{"secret invalid record"\n')
        with self.assertRaisesRegex(conversation.ConversationError, "allow-partial"):
            conversation.gather([source])
        result = conversation.gather([source], allow_partial=True)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(len(result["coverage"]["gaps"]), 2)
        self.assertTrue(result["coverage"]["partial"])
        self.assertIsNone(result["coverage"]["history_complete"])

    def test_markdown_is_inert_and_json_preserves_exact_content(self):
        payload = 'multiline\n```\n<script>bad()</script>\n[link](https://example.invalid)\n\x1b[31m'
        source = self.source([{"role": "user", "content": payload}])
        result = conversation.gather([source])
        before = copy.deepcopy(result)
        output = conversation.export(result, self.root / "export")
        saved = json.loads((output / "conversation.json").read_text())
        self.assertEqual(saved["messages"][0]["content"], payload)
        self.assertIn("````text", (output / "conversation.md").read_text())
        self.assertEqual(result, before)
        manifest = json.loads((output / "manifest.json").read_text())
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), digest)
        self.assertIn("No redaction", (output / "SENSITIVITY.md").read_text())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            self.assertTrue(all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in output.iterdir()))

    def test_refuses_existing_output_credentials_and_oversize(self):
        source = self.source([{"role": "user", "content": "hello"}])
        data = conversation.gather([source])
        with self.assertRaises(FileExistsError):
            conversation.export(data, self.root)
        for name in ("auth.json", "auth.json.gz", "credentials.json"):
            with self.subTest(name=name), self.assertRaisesRegex(conversation.ConversationError, "credential"):
                conversation.gather([self.root / name])
        with mock.patch.object(conversation, "MAX_BYTES", 8), self.assertRaisesRegex(conversation.ConversationError, "limit"):
            conversation.gather([source])

    def test_no_network_ledger_mutation_or_transcript_in_stdout(self):
        source = self.source([{"role": "user", "content": "PRIVATE MESSAGE BODY"}])
        home = self.root / "ledger-home"
        env = dict(os.environ, AI_USAGE_LEDGER_HOME=str(home))
        for action in ("list", "export"):
            command = [sys.executable, str(ROOT / "scripts/ledger.py"), "conversation", action, "--source", str(source)]
            if action == "export":
                command += ["--out", str(self.root / "cli-export")]
            result = subprocess.run(command, capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("PRIVATE MESSAGE BODY", result.stdout + result.stderr)
            self.assertIn("sessions", json.loads(result.stdout))
        self.assertFalse(home.exists())
        with mock.patch("socket.socket", side_effect=AssertionError("must stay offline")):
            conversation.gather([source])

    def test_large_session_list_remains_valid_json_and_hides_bodies(self):
        source = self.source([{"role": "user", "session_id": "session-" + str(i), "content": "PRIVATE BODY"}
                              for i in range(150)])
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(conversation.main(["list", "--source", str(source)]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["sessions"]), 150)
        self.assertNotIn("PRIVATE BODY", output.getvalue())

    def test_symlink_and_special_files_are_refused(self):
        source = self.source([{"role": "user", "content": "hello"}])
        link = self.root / "link.json"
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest("symlink creation unavailable")
        with self.assertRaises(conversation.ConversationError):
            conversation.gather([link])
        if hasattr(os, "mkfifo"):
            fifo = self.root / "fifo.jsonl"
            os.mkfifo(fifo)
            with self.assertRaisesRegex(conversation.ConversationError, "regular"):
                conversation.gather([fifo])


if __name__ == "__main__":
    unittest.main()
