"""Count native records in explicitly selected archives and match saved usage."""

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import PurePosixPath
import sqlite3
import tarfile
import zipfile

import census_sources as formats
import compile_ai_logs
import source_streams


TOKEN_FIELDS = ("input_uncached", "cache_read", "cache_write", "output", "reasoning")
COLUMNS = ",".join(TOKEN_FIELDS)


def timestamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    return stamp.astimezone(timezone.utc).isoformat() if stamp.tzinfo else None


class CodexUsage:
    def __init__(self, connection):
        self.connection = connection
        self.signatures = set()
        self.totals = Counter()
        self.table_hint = ""
        if connection and connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='events_ts' AND tbl_name='events'").fetchone():
            self.table_hint = " INDEXED BY events_ts"

    def write(self, line):
        row = json.loads(line)
        when = timestamp(row["ts"])
        tokens = row["total"]
        if not when or tokens <= 0:
            return
        values = tuple(int(row[name] or 0) for name in TOKEN_FIELDS)
        key = when, *values
        if key in self.signatures:
            return
        self.signatures.add(key)
        self.totals["parsed_calls"] += 1
        self.totals["parsed_tokens"] += tokens
        if self.connection:
            sql = "SELECT " + COLUMNS + " FROM events" + self.table_hint + " WHERE ts = ? AND tool = 'codex'"
            if any(tuple(int(value or 0) for value in r) == values for r in self.connection.execute(sql, (when,))):
                self.totals["matched_calls"] += 1
            else:
                self.totals["unmatched_calls"] += 1
                self.totals["unmatched_tokens"] += tokens

    def summary(self):
        keys = ("parsed_calls", "parsed_tokens")
        if self.connection:
            keys += ("matched_calls", "unmatched_calls", "unmatched_tokens")
        return {key: self.totals[key] for key in keys}


@contextmanager
def member_stream(reader, entry, name):
    with reader(entry) as source:
        if name.endswith(".gz"):
            with gzip.GzipFile(fileobj=source) as inflated:
                yield inflated
        elif name.endswith((".zst", ".zstd")):
            if source_streams.zstd is None:
                raise OSError("Zstandard archive members need Python 3.14+")
            try:
                with source_streams.zstd.open(source, "rb") as inflated:
                    yield inflated
            except source_streams.zstd.ZstdError as error:
                raise OSError("invalid Zstandard archive member") from error
        else:
            yield source


def codex_in_archive(members, reader, connection):
    root = "archive"
    folder = os.path.join(root, "sessions")
    paths, sizes = {}, {}
    for name, entry in members:
        leaf = PurePosixPath(name.replace("\\", "/")).name
        match = formats.archive_member(name)
        if (not match or match[0] != "codex" or not leaf.startswith("rollout-")
                or not source_streams.uncompressed_name(leaf).endswith(".jsonl")):
            continue
        path = os.path.join(folder, str(len(paths)), leaf)
        sizes[path] = entry.file_size if isinstance(entry, zipfile.ZipInfo) else entry.size
        paths[path] = (entry, leaf)

    def read(path):
        entry, name = paths[path]
        return member_stream(reader, entry, name)

    stats = compile_ai_logs.Stats()
    sessions = []
    calls = CodexUsage(connection)
    compile_ai_logs.scan_codex(root, "retained-archive", calls, stats, sessions, {},
                               rollout_paths={folder: paths}, open_rollout=read, rollout_sizes=sizes)
    ids = {row["session"] for row in sessions if formats.UUID.fullmatch(row["session"])}
    result = dict(calls.summary(), rollout_members=len(paths), distinct_session_ids=len(ids),
                  parse_warnings=stats.errors)
    if connection:
        matched = set()
        for session in ids:
            if connection.execute("SELECT 1 FROM sessions WHERE tool = 'codex' AND session = ? LIMIT 1",
                                  (session,)).fetchone():
                matched.add(session)
        result["existing_session_ids"] = len(matched)
        result["unmatched_session_ids"] = len(ids - matched)
    return result


def compare_archive(path, connection):
    """Read archive directories and replay-safe Codex counters without extraction."""
    report = {"path": str(path)}
    try:
        if problem := formats.readable_file(path):
            return dict(report, status=problem)
        with formats.open_archive(path) as (members, reader):
            families = Counter(hit[0] for name, _ in members if (hit := formats.archive_member(name)))
            report.update(members=len(members), session_candidates_by_harness=dict(families),
                          codex=codex_in_archive(members, reader, connection))
        status = "partial transcript read" if report["codex"]["parse_warnings"] else "read"
        return dict(report, status=status)
    except (OSError, EOFError, ValueError, sqlite3.Error, tarfile.TarError, zipfile.BadZipFile, RuntimeError) as error:
        return dict(report, status=type(error).__name__)
