#!/usr/bin/env python3
"""List or export recorded conversations from explicitly selected local JSON logs.

This command reads full message and tool content. It does not discover sources,
read credentials, contact hosts, or change prompt/archive consent preferences.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import zlib

import safety

MAX_BYTES = 128 * 1024 * 1024
ROLES = {"system", "developer", "user", "assistant", "tool", "function"}
CONTENT_FIELDS = ("tool_calls", "function_call", "toolCalls", "thoughts", "attachments",
                  "images", "local_images", "refusal", "audio", "annotations", "citations",
                  "reasoning_content", "reasoning", "reasoning_details", "thinking")
CODEX_METADATA = {"session_meta", "turn_context"}
CODEX_EVENTS = {"token_count", "task_started", "task_complete", "turn_aborted",
                "item_started", "item_completed", "context_compacted", "thread_rolled_back"}
CLAUDE_METADATA = {"summary", "file-history-snapshot", "queue-operation", "progress",
                   "system", "last-prompt", "pr-link", "custom-title", "agent-name"}
PI_METADATA = {"session", "model_change", "thinking_level_change", "label", "session_info", "custom"}


class ConversationError(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True)


def identifier(value):
    return value if isinstance(value, str) and value and len(value) <= 1024 else None


def safe_path(value):
    """Reject path aliases before source reads or output writes."""
    safety.check_path(value)
    path = Path(os.path.abspath(os.path.expanduser(str(value))))
    if str(path).startswith(("\\\\", "//")):
        raise ConversationError("remote paths are unsupported; select an approved local copy")
    for part in (path, *path.parents):
        try:
            metadata = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ConversationError("symlink and reparse-point paths are unsupported")
    return path


def read_source(value):
    path = safe_path(value)
    if safety.is_credential_file(str(path)):
        raise ConversationError("credential-like source files are refused")
    name = path.name[:-3] if path.name.endswith(".gz") else path.name
    if safety.is_credential_file(str(path.with_name(name))):
        raise ConversationError("credential-like source files are refused")
    suffix = Path(name).suffix.lower()
    if suffix not in (".json", ".jsonl", ".ndjson"):
        raise ConversationError("select a JSON, JSONL or gzip-compressed JSON source")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ConversationError("source must be a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as raw:
        after = os.fstat(raw.fileno())
        if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ConversationError("source changed while opening")
        if suffix and path.name.endswith(".gz"):
            with gzip.GzipFile(fileobj=raw) as stream:
                data = stream.read(MAX_BYTES + 1)
        else:
            data = raw.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ConversationError("source exceeds the 128 MiB decompressed limit; no content was truncated or exported")
    return path, data, suffix


def decode(data):
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("duplicate JSON key")
            obj[key] = value
        return obj

    def constant(_):
        raise ValueError("nonfinite JSON value")

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("nonfinite JSON value")
        return result
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant, parse_float=number)


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(text_content(block) for block in content)
    if isinstance(content, dict):
        for key in ("text", "thinking", "content"):
            if key in content:
                return text_content(content[key])
    return encode(content)


def message(row, role=None, content=None, kind="message"):
    role = role or row.get("role")
    if role == "gemini":
        role = "assistant"
    if role == "toolResult":
        role = "tool"
    if not isinstance(role, str) or role not in ROLES:
        return None
    if content is None:
        content = row.get("content")
    if content is None and not any(key in row for key in CONTENT_FIELDS):
        return None
    result = {"role": role, "kind": kind, "content": content}
    for key in ("id", "name", "call_id", "tool_call_id", "toolCallId", "toolName", "parentId", "parentUuid", "channel", "model"):
        if identifier(row.get(key)):
            result[key] = row[key]
    for key in CONTENT_FIELDS:
        if key in row:
            result[key] = row[key]
    return result


def parse_source(path, data, suffix, requested_tool, source_index):
    gaps, records = [], []
    if suffix in (".jsonl", ".ndjson"):
        for index, line in enumerate(data.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append((index, decode(line)))
            except (ValueError, UnicodeError, RecursionError):
                gaps.append({"source": source_index, "record": index, "code": "invalid_json_record"})
    else:
        try:
            root = decode(data)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise ConversationError("invalid JSON source; no export written") from exc
        records = [(1, root)]

    objects = [row for _, row in records if isinstance(row, dict)]
    tool = requested_tool
    if tool == "auto":
        if any(row.get("type") in ("session_meta", "response_item", "event_msg") for row in objects):
            tool = "codex"
        elif any(row.get("type") in ("user", "assistant") and isinstance(row.get("message"), dict) for row in objects):
            tool = "claude-code"
        elif any(row.get("type") == "session" for row in objects):
            tool = "pi"
        else:
            tool = "messages"
    fallback = "source-" + str(source_index) + "-" + hashlib.sha256(data).hexdigest()[:16]
    session = fallback
    if tool == "codex":
        headers = [row.get("payload", {}) for row in objects if row.get("type") == "session_meta"]
        if headers and isinstance(headers[0], dict):
            session = identifier(headers[0].get("id")) or session
    elif tool == "pi":
        session = next((row["id"] for row in objects if row.get("type") == "session" and identifier(row.get("id"))), session)
    elif objects:
        session = identifier(objects[0].get("sessionId")) or identifier(objects[0].get("session_id")) or session

    found, mirrors, ignored = [], [], Counter()

    def add(item, outer, index, sid=None, mirror=False):
        if item is None:
            gaps.append({"source": source_index, "record": index, "code": "unsupported_message"})
            return
        item["session"] = sid or session
        item["tool"] = tool
        item["source"] = source_index
        item["record"] = index
        stamp = outer.get("timestamp", outer.get("ts", outer.get("created_at", item.get("timestamp"))))
        if isinstance(stamp, (str, int, float)) and not isinstance(stamp, bool):
            item["timestamp"] = stamp
        (mirrors if mirror else found).append(item)

    for index, row in records:
        if tool in ("codex", "claude-code", "pi") and not isinstance(row, dict):
            gaps.append({"source": source_index, "record": index, "code": "unsupported_record"})
            continue
        if tool == "codex" and isinstance(row, dict):
            typ, payload = row.get("type"), row.get("payload")
            typ = typ if isinstance(typ, str) else ""
            if typ in CODEX_METADATA:
                if typ == "session_meta" and isinstance(payload, dict):
                    session = identifier(payload.get("id")) or session
                ignored[str(typ)] += 1
                continue
            if not isinstance(payload, dict):
                gaps.append({"source": source_index, "record": index, "code": "unsupported_record"})
                continue
            subtype = payload.get("type", "")
            subtype = subtype if isinstance(subtype, str) else ""
            if typ == "response_item":
                if subtype == "message":
                    add(message(payload), row, index)
                elif subtype in ("function_call", "custom_tool_call") or subtype.endswith("_call"):
                    content = {key: value for key, value in payload.items() if key not in ("id", "type")}
                    add(message(payload, "assistant", content, "tool_call"), row, index)
                elif subtype in ("function_call_output", "custom_tool_call_output") or subtype.endswith("_call_output"):
                    add(message(payload, "tool", payload.get("output", payload.get("content")), "tool_result"), row, index)
                elif subtype == "reasoning":
                    add(message(payload, "assistant", {key: payload[key] for key in ("summary", "content") if key in payload}, "recorded_reasoning"), row, index)
                else:
                    gaps.append({"source": source_index, "record": index, "code": "unsupported_response_item"})
            elif typ == "event_msg" and subtype in ("user_message", "agent_message", "agent_reasoning"):
                add(message(payload, "user" if subtype == "user_message" else "assistant", payload.get("message", payload.get("text")),
                            "recorded_reasoning_event" if subtype == "agent_reasoning" else "ui_message"), row, index, mirror=True)
            elif typ == "event_msg" and subtype in CODEX_EVENTS:
                ignored[str(subtype)] += 1
                if subtype in ("context_compacted", "thread_rolled_back"):
                    gaps.append({"source": source_index, "record": index, "code": str(subtype)})
            elif typ == "compacted":
                gaps.append({"source": source_index, "record": index, "code": "compacted_history"})
                summary = {key: payload[key] for key in ("message", "replacement_history", "summary") if key in payload}
                if summary:
                    add(message({}, "system", summary, "compaction_summary"), row, index)
            else:
                gaps.append({"source": source_index, "record": index, "code": "unsupported_record"})
        elif tool in ("claude-code", "pi") and isinstance(row, dict):
            typ = row.get("type")
            typ = typ if isinstance(typ, str) else ""
            if typ == "session":
                session = identifier(row.get("id")) or session
            session = identifier(row.get("sessionId")) or session
            nested = row.get("message")
            if isinstance(nested, dict) and (typ in ("user", "assistant", "message") or
                                            isinstance(nested.get("role"), str) and nested.get("role") in ROLES):
                item = message(nested, nested.get("role") or typ)
                if item is not None:
                    for key in ("uuid", "parentUuid", "parentId"):
                        if identifier(row.get(key)):
                            item["id" if key == "uuid" else key] = row[key]
                    item.setdefault("id", row.get("id"))
                add(item, row, index, identifier(row.get("sessionId")))
            elif typ in (CLAUDE_METADATA if tool == "claude-code" else PI_METADATA):
                ignored[str(typ)] += 1
                if typ == "summary" or "compact" in str(row.get("subtype", "")) or "compactMetadata" in row:
                    gaps.append({"source": source_index, "record": index, "code": "compacted_history"})
            else:
                gaps.append({"source": source_index, "record": index, "code": "unsupported_record"})
        else:
            nested = row.get("messages") if isinstance(row, dict) else row
            if isinstance(nested, list):
                for position, value in enumerate(nested, 1):
                    if isinstance(value, dict):
                        add(message(value, value.get("role") or value.get("type")), value, f"{index}.{position}",
                            identifier(value.get("sessionId")) or identifier(value.get("session_id")))
                    else:
                        gaps.append({"source": source_index, "record": f"{index}.{position}", "code": "unsupported_message"})
            elif isinstance(row, dict) and isinstance(row.get("role"), str) and row.get("role") in ROLES:
                add(message(row), row, index, identifier(row.get("sessionId")) or identifier(row.get("session_id")))
            else:
                gaps.append({"source": source_index, "record": index, "code": "unsupported_record"})

    # UI events may echo response items, but text/proximity/timestamps alone do
    # not prove identity. Collapse only a shared native message ID and preserve
    # the complete UI observation as provenance. Otherwise keep both records.
    for mirror in mirrors:
        match = next((n for n, item in enumerate(found) if mirror.get("id")
                      and item.get("id") == mirror["id"] and item["role"] == mirror["role"]
                      and item["session"] == mirror["session"] and item["kind"] == "message"
                      and text_content(item["content"]) == text_content(mirror["content"])), None)
        if match is None:
            found.append(mirror)
        else:
            found[match].setdefault("ui_events", []).append(mirror)
            ignored["mirrored_ui_message"] += 1
    if tool == "codex":
        found.sort(key=lambda item: item["record"])
    return found, gaps, {"path": str(path), "sha256_uncompressed": hashlib.sha256(data).hexdigest(),
                         "bytes_uncompressed": len(data),
                         "tool": tool, "session_id_origin": "source_digest" if session == fallback else "recorded",
                         "ignored_metadata": dict(ignored)}


def gather(sources, tool="auto", session=None, allow_partial=False):
    messages, gaps, evidence, seen = [], [], [], set()
    for value in sources:
        path = safe_path(value)
        key = os.path.normcase(str(path))
        if key in seen:
            continue  # Only the exact selected path, never content-only matches.
        path, data, suffix = read_source(path)
        seen.add(key)
        rows, missing, source = parse_source(path, data, suffix, tool, len(evidence))
        messages.extend(rows)
        gaps.extend(missing)
        evidence.append(source)
    if session:
        messages = [item for item in messages if item["session"] == session]
    if not messages:
        raise ConversationError("no supported conversation messages matched the selected sources/session")
    if gaps and not allow_partial:
        raise ConversationError("unreadable, unsupported or compacted records found; review with --allow-partial to export available messages")
    return {"schema": "ai-conversation/1", "messages": messages, "sources": evidence,
            "coverage": {"scope": "available recorded content in explicitly selected files",
                         "history_complete": None, "partial": bool(gaps), "gaps": gaps,
                         "ordering": "selected source order, then native record order",
                         "limitations": ["Unrecorded/deleted content and hidden reasoning cannot be reconstructed.",
                                         "External images, attachments and tool-result files are referenced, not fetched.",
                                         "UI events without a shared message ID are retained separately, including possible echoes.",
                                         "Forks and supplied source parts are not automatically merged or discovered."]}}


def markdown(document):
    parts = ["# Conversation export", "", "Private: contains recorded message and tool content.", "",
             "Coverage: selected source files only; complete historical capture is not established.", ""]
    for index, item in enumerate(document["messages"], 1):
        parts.extend([f"## {index}. {item['role']} ({item['kind']})", ""])
        metadata = {key: value for key, value in item.items() if key != "content"}
        content = item["content"]
        blocks = [(json.dumps(metadata, ensure_ascii=False, indent=2, allow_nan=False), "json"),
                  (content, "text") if isinstance(content, str) else
                  (json.dumps(content, ensure_ascii=False, indent=2, allow_nan=False), "json")]
        for body, language in blocks:
            fence = "`" * max(3, 1 + max((len(x) for x in re.findall(r"`+", body)), default=0))
            parts.extend([fence + language, body, fence, ""])
    parts.extend(["## Coverage notes", "", "```json", json.dumps(document["coverage"], indent=2), "```", ""])
    return "\n".join(parts)


def export(document, destination, output_format="both"):
    out = safe_path(destination)
    if not out.parent.is_dir():
        raise ConversationError("output parent must exist")
    files = {}
    if output_format in ("json", "both"):
        files["conversation.json"] = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if output_format in ("markdown", "both"):
        files["conversation.md"] = markdown(document)
    files["SENSITIVITY.md"] = "# Private conversation export\n\nContains full recorded messages, tool inputs and outputs; these may include secrets and personal data. No redaction or anonymization was performed. Review before sharing.\n"
    manifest = {"schema": document["schema"], "messages": len(document["messages"]),
                "sources": document["sources"], "coverage": document["coverage"],
                "files": {name: hashlib.sha256(value.encode()).hexdigest() for name, value in files.items()}}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    # Serialize everything before creating output; exclusive creation prevents
    # overwriting source files or prior exports.
    out.mkdir(mode=0o700)
    for name, content in files.items():
        fd = os.open(out / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("list", "export"):
        command = sub.add_parser(action)
        command.add_argument("--source", required=True, action="append", help="explicit local source file; repeat for ordered parts or sessions")
        command.add_argument("--tool", choices=("auto", "codex", "claude-code", "pi", "messages"), default="auto")
        command.add_argument("--session", help="include only this recorded session ID")
        command.add_argument("--allow-partial", action="store_true", help="retain available messages and report malformed/unsupported records")
        if action == "export":
            command.add_argument("--out", required=True, help="NEW private output directory; its parent must exist")
            command.add_argument("--format", choices=("both", "json", "markdown"), default="both")
    args = parser.parse_args(argv)
    try:
        document = gather(args.source, args.tool, args.session, args.allow_partial)
        counts = Counter((item["tool"], item["session"]) for item in document["messages"])
        summary = {"sessions": [{"tool": key[0], "session": key[1], "messages": count} for key, count in counts.items()],
                   "partial": document["coverage"]["partial"]}
        if args.action == "export":
            summary["out"] = str(export(document, args.out, args.format))
        # ASCII JSON escapes terminal controls and preserves arbitrarily many
        # selected sessions without truncating the machine-readable result.
        print(json.dumps(summary, ensure_ascii=True, allow_nan=False))
    except (OSError, ValueError, RecursionError, EOFError, zlib.error) as exc:
        # Do not echo raw JSON/provider exceptions, which may contain private text.
        detail = str(exc) if isinstance(exc, ConversationError) else type(exc).__name__
        print("Conversation export failed: " + safety.clean_for_terminal(detail), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
