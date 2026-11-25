"""Read native usage counters without creating additional ledger events."""

import contextlib
import json
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile


COUNTER_TABLES = {
    "copilot-cli": ("assistant_usage_events", "request", None, {
        "input_tokens": "input_tokens", "output_tokens": "output_tokens", "cache_read_tokens": "cache_read_tokens",
        "cache_write_tokens": "cache_write_tokens", "reasoning_tokens": "reasoning_tokens",
        "total_nano_aiu": "total_nano_aiu",
    }),
    "hermes": ("session_model_usage", "session-model", "api_call_count", {
        "input_tokens": "input_tokens", "output_tokens": "output_tokens", "cache_read_tokens": "cache_read_tokens",
        "cache_write_tokens": "cache_write_tokens", "reasoning_tokens": "reasoning_tokens",
        "estimated_cost_usd": "estimated_cost_usd", "actual_cost_usd": "actual_cost_usd",
    }),
}


def open_database(path):
    connection = sqlite3.connect(Path(path).absolute().as_uri() + "?mode=ro", uri=True, timeout=10)
    connection.execute("PRAGMA query_only = ON")
    return connection


def columns(connection, table):
    return {row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)}


def native_counters(tool, path):
    table, granularity, calls_column, fields = COUNTER_TABLES[tool]
    with contextlib.closing(open_database(path)) as database:
        database.execute("BEGIN")
        available = columns(database, table)
        if not {"session_id", "model"} <= available:
            return {"status": "unsupported schema", "table": table, "granularity": granularity}
        totals = {key: column for key, column in fields.items() if column in available}
        select = ["COUNT(*) AS rows", "COUNT(DISTINCT session_id) AS sessions"]
        if calls_column in available:
            select.append("SUM(%s) AS api_calls" % calls_column)
        select.extend("SUM(%s) AS %s" % (column, key) for key, column in totals.items())
        projection = ", ".join(select)
        database.row_factory = sqlite3.Row
        total = dict(database.execute("SELECT %s FROM %s" % (projection, table)).fetchone())
        per_model = [dict(row) for row in database.execute(
            "SELECT model, %s FROM %s GROUP BY model ORDER BY model" % (projection, table)
        )]
        return {"status": "read", "table": table, "granularity": granularity, "totals": total, "models": per_model}


def paseo_archive_counters(path):
    """Report usage evidence from local transcript copies separately from the live ledger."""
    fields = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    totals = {field: 0 for field in fields}
    transcripts = measured = malformed = repeated = rows = 0
    seen = set()
    with tarfile.open(path, "r|gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if not member.isfile() or len(parts) < 3 or parts[0] != "transcripts" or ".." in parts:
                continue
            if not parts[-1].endswith(".jsonl"):
                continue
            transcripts += 1
            have_usage = False
            with archive.extractfile(member) as stream:
                for line in stream:
                    if b'"usage"' not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except (UnicodeError, ValueError):
                        malformed += 1
                        continue
                    if not isinstance(record, dict) or record.get("type") != "assistant":
                        continue
                    message = record.get("message")
                    if not isinstance(message, dict):
                        continue
                    usage = message.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    counts = [usage.get(field) for field in fields]
                    if any(value is not None and (type(value) is not int or value < 0) for value in counts):
                        malformed += 1
                        continue
                    if counts[0] is None or counts[1] is None:
                        continue
                    have_usage = True
                    identity = message.get("id") or record.get("uuid")
                    if isinstance(identity, str) and identity:
                        if identity in seen:
                            repeated += 1
                            continue
                        seen.add(identity)
                    rows += 1
                    for field, value in zip(fields, counts):
                        totals[field] += value or 0
            measured += have_usage
    return {"status": "read", "granularity": "archived message evidence",
            "transcript_members": transcripts, "transcripts_with_usage": measured,
            "dedup_skipped": repeated, "malformed": malformed, "totals": {"rows": rows, **totals}}
