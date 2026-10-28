# Full-conversation export

`ledger.py conversation` gathers the recorded user, assistant, system/developer
messages, tool calls and tool results from explicitly selected transcript files.
It produces a readable Markdown transcript and structured JSON, separate from
the usage ledger. It preserves available content, rather than exporting only
token counts or user prompts.

## Select files, inspect sessions, export

```bash
python scripts/ledger.py conversation list --source /absolute/selected-session.jsonl
python scripts/ledger.py conversation export \
  --source /absolute/selected-session.jsonl \
  --out /absolute/new-conversation-export
```

`--source` is repeatable for ordered parts or separate sessions. Only the named
files are read. No home-directory discovery, database lookup, SSH transfer or
automatic related-thread collection occurs. The output directory must be new
and its parent must exist. Relative source/output paths are also accepted.

Use `--session <recorded-id>` to select a session from the list. Without it, all
sessions present in the selected files are included. Files with no recorded
session ID receive a clearly identified source-digest fallback. Repeating a
message does not remove it; selecting the exact same source path twice reads it
once. Distinct files are retained even when their content matches, since they
could be independent conversations. Distinct source parts are preserved in the order
supplied. Branch/parent IDs remain metadata; the command does not silently
combine different branches or infer chronological ordering across files.

## Supported inputs

| Input | Recorded content retained |
|---|---|
| Codex rollout JSONL | `response_item` messages, tool calls/results, available reasoning summaries and UI message events. Only echoes with a shared native message ID are folded into a message's `ui_events` provenance; ambiguous echoes remain separate records. |
| Claude Code JSONL | User/assistant messages and all stored content blocks, including tool use/results and recorded thinking; message/parent IDs remain available. |
| pi / OpenClaw message JSONL | Session headers and message content, including `toolResult` messages and tool-call IDs. |
| JSON message arrays or a `messages` envelope | Standard role/content messages, system/developer messages, tool/function calls and results. Includes Gemini-style user/`gemini` entries and Cline API message histories. |
| Gzip copies of the above | Decompressed in memory without modifying the archive or its index. Select an existing `.json.gz` or `.jsonl.gz` file directly. |

`--tool auto` detects the above shapes. An explicit `--tool codex|claude-code|pi|messages`
selects the parser. This does not promise support for every harness's raw format.
Usage-only records and a prompt-only history cannot reconstruct full conversations.
Unknown records, malformed JSONL or compaction markers stop a normal export. Use
`--allow-partial` only when a partial result is wanted; the manifest enumerates
the gaps. No output is silently truncated. Inputs over 128 MiB after decompression
are refused. Arbitrary JSON objects and usage-only databases are not traversed.

## Consent, privacy and files

This is an explicit, one-off **full-content read** of the selected files. Both
`list` and `export` parse those files. Before an agent invokes it on real data,
the operator must have authorized the source files and full-content export.
Adding/installing this feature does not authorize reading personal transcripts.
The command does not change `prompts.capture`, `archive.raw_logs`, scheduling or
any ledger setting; those opt-ins continue to govern their existing features.

The export can contain personal information and secrets already present in
messages or tool output. Content is not silently redacted or anonymized. Known
credential files, symlink/junction paths and nonregular files are refused. No
embedded command is executed, and no image URL or attachment is fetched.

The new directory contains:

- `conversation.json`: full structured messages, source/record provenance and coverage.
- `conversation.md`: ordered messages with literal, safely fenced content and metadata.
- `manifest.json`: hashes/sizes of uncompressed source content, message counts, gap notes and output hashes.
- `SENSITIVITY.md`: the full-content privacy notice.

`--format json|markdown|both` controls transcript formats; the manifest and
sensitivity notice are always written. Files use 0600 and the directory 0700 on
POSIX; on Windows they inherit the selected parent's ACL. Existing outputs are
never overwritten. Neither the source files nor the append-only ledger change.
The terminal prints session IDs/counts and the destination, never message bodies.

Coverage means **all supported recorded content in the selected inputs**.
`history_complete` remains unknown: deleted turns, omitted history, hidden
reasoning and external attachment bytes cannot be reconstructed. Review the
manifest and content before sharing. Normal publication approval still applies.
