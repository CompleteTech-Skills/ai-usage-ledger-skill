# Extended local usage sources

The scanner reads Codex `rollout-*.jsonl` files from every configured Codex
home, including `.jsonl.gz`, `.jsonl.zst` and `.jsonl.zstd`. Gzip uses the
standard library. Zstandard uses Python 3.14+ or a `zstd` executable on the
scanning host. The inventory reports compressed paths and stream errors.
Identical calls in copies of the same rollout are counted once per host.
List alternate Codex homes explicitly in `codex_roots`, including alternate
homes inside WSL.

OpenCode reads legacy `message` records and native assistant records from
`session_message`. Native IDs present in the legacy table are collected once.
The scan receipt gives separate counts for the two layouts.

Set the following fields on an explicitly approved manifest host. Detection
does not automatically add these roots to an existing manifest.

| Field | Source | Usage treatment |
|---|---|---|
| `claude_desktop_roots` | Claude Desktop agent-mode/Cowork transcript directories | Records with Anthropic assistant usage are collected as Claude calls and share message identity with Claude Code on that host. |
| `dsh_roots` | DeepSeek Harness v0 session journal directories | Completed assistant steps with token fields enter the ledger. Streaming chunks are ignored because the completed step carries the same usage. |
| `native_counter_dbs` | `copilot-cli=/path/session-store.db` or `hermes=/path/state.db` | Native counters appear in the scan inventory. Copilot request rows can overlap event streams, and Hermes rows total usage by session and model. |
| `paseo_roots` | Directories of local `.tar.gz` transcript exports | The archive is read in place. Member counts and Claude usage evidence appear in the inventory. Archive copies carry no extra ledger charges. |
| `ssh_config` | Existing absolute, trusted OpenSSH config file for a named SSH host | This file applies to scan and archive SSH/SCP connections. The scheduled-run consent hash includes its contents. |

Configure WSL with native Linux paths. A read-only native SQLite connection
uses the database and its live WAL together. A Windows-share fallback may be
unable to read a native counter database. For unsupported formats, missing
usage fields and copies outside the configured roots, use the session
inventory alongside the scan report. The scan inventory and the usage ledger
can have different totals because one counts files and the other records
observed usage.
