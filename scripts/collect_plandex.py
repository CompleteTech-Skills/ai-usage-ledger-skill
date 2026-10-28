#!/usr/bin/env python3
"""Collect explicitly approved Plandex usage-export/1 files. No database/network discovery.

The export is a proposed self-hosted extension, not a stock Plandex 2.2.1 feature.
Source files are read-only. The private checkpoint is a collector-owned SQLite cache;
it is NOT the Plandex database. Replay its normalized snapshot into any ledger store.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

SCHEMA = "plandex-usage/1"
MAX_LINE = 1024 * 1024
MAX_COUNT = 2**53 - 1
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./:+-]{0,255}\Z")
STATUSES = {"completed", "error", "cancelled", "interrupted"}
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")
RAW_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens",
              "reasoning_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


class CollectionError(ValueError):
    """An unsafe or incompatible configuration, or conflicting immutable evidence."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def pseudonym(source, kind, value):
    return digest([source, kind, value]) if value else None


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def decode(raw):
    def bad_constant(_):
        raise ValueError("non-finite JSON value")
    return json.loads(raw, object_pairs_hook=_pairs, parse_float=Decimal, parse_constant=bad_constant)


def local_path(value, suffix=None):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise CollectionError("expected a plain absolute local path")
    if value.startswith(("//", "\\\\")) or "://" in value or ".." in Path(value).parts:
        raise CollectionError("remote paths and parent traversal are not supported; copy an approved export locally")
    path = Path(value)
    if not path.is_absolute() or (suffix and path.suffix != suffix):
        raise CollectionError("expected an absolute local %s path" % (suffix or "file"))
    for ancestor in [path] + list(path.parents):
        if ancestor.is_symlink():
            raise CollectionError("symlink paths are not supported")
        try:
            attributes = getattr(ancestor.lstat(), "st_file_attributes", 0)
        except FileNotFoundError:
            continue
        if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise CollectionError("reparse point paths, including junctions, are not supported")
    return path


def read_file(path):
    """Open only an existing regular, non-symlink, non-hardlinked file, never write it."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise CollectionError("source must be a regular, non-hardlinked file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or after.st_nlink != 1 or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        os.close(fd)
        raise CollectionError("source changed while opening")
    return os.fdopen(fd, "rb")


def identifier(value, model=False):
    return isinstance(value, str) and bool((MODEL_RE if model else ID_RE).fullmatch(value))


def load_config(path):
    path = local_path(str(path), ".json")
    if re.search(r"auth|credential|secret|password|^\.env", path.name, re.I):
        raise CollectionError("refusing a credential-like configuration filename")
    with read_file(path) as fh:
        raw = fh.read(MAX_LINE + 1)
    if len(raw) > MAX_LINE:
        raise CollectionError("configuration is too large")
    try:
        config = decode(raw)
    except (ValueError, UnicodeError, RecursionError) as ex:
        raise CollectionError("invalid configuration JSON") from ex
    return validate_config(config)


def validate_config(config):
    if not isinstance(config, dict) or config.get("schema") != "plandex-collector/1":
        raise CollectionError("unsupported collector configuration schema")
    if config.get("approved_project_scope") is not True:
        raise CollectionError("explicit approved_project_scope acknowledgement is required")
    if set(config) - {"schema", "approved_project_scope", "sources"}:
        raise CollectionError("unknown configuration field; database credentials/options are not supported")
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise CollectionError("configure at least one approved export source")
    source_ids, paths = set(), set()
    for source in sources:
        if not isinstance(source, dict) or set(source) - {"source_id", "files", "directories", "project_ids", "versions"}:
            raise CollectionError("unsupported source configuration; only local export files are supported")
        sid = source.get("source_id")
        if not identifier(sid) or sid in source_ids:
            raise CollectionError("source_id must be a unique opaque identifier")
        source_ids.add(sid)
        for field in ("project_ids", "versions"):
            values = source.get(field)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v for v in values):
                raise CollectionError("source requires nonempty project_ids and versions lists")
            if len(values) != len(set(values)):
                raise CollectionError("duplicate values in source configuration")
        if not all(identifier(p) for p in source["project_ids"]):
            raise CollectionError("project selection must contain explicit opaque IDs; no wildcard")
        if not source.get("files") and not source.get("directories"):
            raise CollectionError("source requires explicit files or export directories")
        for field in ("files", "directories"):
            values = source.get(field, [])
            if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values) or len(values) != len(set(values)):
                raise CollectionError("files/directories must be lists without duplicates")
            for value in values:
                local_path(value, ".jsonl" if field == "files" else None)
        for directory in source.get("directories", []):
            if not identifier(Path(directory).name):
                raise CollectionError("export directory must have a plain name")
        for filename in source.get("files", []):
            p = local_path(filename, ".jsonl")
            if re.search(r"auth|credential|secret|password|^\.env", p.name, re.I):
                raise CollectionError("refusing a credential-like source filename")
            if str(p) in paths:
                raise CollectionError("each export file may belong to only one configured source")
            paths.add(str(p))
    return config


def timestamp(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("missing_timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("missing_timezone")
    return dt.astimezone(timezone.utc).isoformat()


def count(value):
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > MAX_COUNT:
        raise ValueError("invalid_token_count")
    return value


def raw_evidence(row):
    """Preserve only nullable numeric native usage fields, never arbitrary payloads."""
    usage = row.get("raw_usage") if isinstance(row, dict) else None
    if not isinstance(usage, dict):
        return {}
    result = {}
    for key in RAW_FIELDS:
        if key in usage:
            try:
                result[key] = count(usage[key])
            except ValueError:
                result[key] = None
    return result


def normalize(row, source, evidence):
    """Return (identity, normalized event or None, safe gap codes, known token evidence).

    Missing cache details never become measured zeroes: native nullable counters and
    the assumption are retained. Unsplit input is priced at the ordinary input rate
    by existing ledger pricing; it is not a provider charge or a measured cache split.
    Missing input/output, estimates and aggregates are coverage-only.
    """
    if not isinstance(row, dict):
        return None, None, ["invalid_record"], {}
    if row.get("source_id") != source["source_id"] or row.get("project_id") not in source["project_ids"]:
        return None, None, ["outside_scope"], {}
    if row.get("schema") != SCHEMA:
        return None, None, ["unsupported_schema"], {}
    if row.get("plandex_version") not in source["versions"]:
        return None, None, ["unsupported_version"], {}
    sid = source["source_id"]
    attempt = row.get("attempt_id")
    if not identifier(attempt):
        return None, None, ["missing_attempt_id"], {}
    rid = pseudonym(sid, "attempt", attempt)
    kind = row.get("kind")
    if kind == "attempt_start":
        return rid, None, ["attempt_start"], {}
    if kind != "attempt" or row.get("granularity") != "attempt":
        return rid, None, ["non_request_record"], {}
    if not isinstance(row.get("status"), str) or row.get("status") not in STATUSES:
        return rid, None, ["invalid_execution_status"], {}
    try:
        ts = timestamp(row.get("ts"))
    except (ValueError, OverflowError):
        return rid, None, ["invalid_timestamp"], {}
    if not identifier(row.get("provider")) or not identifier(row.get("model"), model=True):
        return rid, None, ["missing_provider_or_model"], {}
    if not identifier(row.get("plan_id")):
        return rid, None, ["missing_plan_id"], {}
    usage = row.get("usage")
    if not isinstance(usage, dict):
        return rid, None, ["missing_usage"], {}
    tokens, invalid = {}, False
    for key in TOKEN_FIELDS:
        try:
            tokens[key] = count(usage.get(key))
        except ValueError:
            tokens[key], invalid = None, True
    if invalid:
        return rid, None, ["invalid_token_count"], tokens
    if row.get("usage_basis") != "provider_reported":
        code = "unverified_gateway_usage" if row.get("usage_basis") == "gateway_reported_unverified" else "unreported_or_estimated_usage"
        return rid, None, [code], tokens
    if row.get("token_semantics") != "inclusive_input_output":
        return rid, None, ["unsupported_token_semantics"], tokens
    if tokens["input_tokens"] is None or tokens["output_tokens"] is None:
        return rid, None, ["incomplete_input_output"], tokens
    missing = [k for k, v in tokens.items() if v is None]
    inp, out = tokens["input_tokens"], tokens["output_tokens"]
    read, write, reasoning = (tokens[k] or 0 for k in TOKEN_FIELDS[2:])
    if read + write > inp or reasoning > out or inp + out > MAX_COUNT:
        return rid, None, ["inconsistent_token_counts"], tokens
    gaps = ["unreported_" + k for k in missing]
    attribution = {}
    for key in ("session_id", "execution_id", "step_id", "build_id", "native_request_id"):
        value = row.get(key)
        attribution[key] = value if identifier(value) else None
        if value is not None and value != "" and not identifier(value):
            gaps.append("invalid_" + key)
    native_cost = None
    cost = row.get("native_cost")
    if cost is not None:
        try:
            amount = Decimal(str(cost["amount"]))
            currency = cost["currency"]
            provenance = cost["provenance"]
            if not amount.is_finite() or amount < 0 or amount > Decimal("1e12"):
                raise ValueError
            if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
                raise ValueError
            if provenance not in ("provider_response", "plandex_native"):
                raise ValueError
            native_cost = {"amount": str(amount), "currency": currency, "provenance": provenance}
        except (KeyError, TypeError, InvalidOperation, ValueError):
            gaps.append("invalid_native_cost")
    else:
        gaps.append("native_cost_unavailable")
    event = {
        "tool": "plandex", "host": "plandex-" + pseudonym(sid, "source", sid),
        "request_id": rid, "ts": ts, "date": ts[:10],
        "session": pseudonym(sid, "session", attribution["session_id"] or row["plan_id"]),
        "model": row["model"], "provider": row["provider"],
        "input_uncached": inp - read - write, "cache_read": read, "cache_write": write,
        "output": out, "reasoning": reasoning, "total": inp + out,
        "cwd": "plandex-project-" + pseudonym(sid, "project", row["project_id"]),
        "kind": "model-request", "version": row["plandex_version"], "src": None,
        "plandex_plan_id": pseudonym(sid, "plan", row["plan_id"]),
        "plandex_execution_id": pseudonym(sid, "execution", attribution["execution_id"]),
        "plandex_step_id": pseudonym(sid, "step", attribution["step_id"]),
        "plandex_build_id": pseudonym(sid, "build", attribution["build_id"]),
        "plandex_native_request_id": pseudonym(sid, "provider-request", attribution["native_request_id"]),
        "plandex_native_timestamp": row["ts"],
        "plandex_status": row["status"], "plandex_granularity": "attempt",
        "plandex_native_tokens": tokens, "plandex_native_cost": native_cost,
        "plandex_raw_usage": raw_evidence(row),
        "plandex_missing_fields": missing,
        "plandex_price_basis": "list-price estimate; native cost kept separately",
        "plandex_cache_assumption": "unreported cache categories priced as ordinary input" if any(k.startswith("cache_") for k in missing) else None,
        "plandex_evidence": {"schema": SCHEMA, "record_sha256": evidence},
    }
    return rid, event, gaps, tokens


def private_directory(value):
    path = local_path(str(value))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise CollectionError("output path is not a directory")
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def atomic_lines(path, lines):
    fd, name = tempfile.mkstemp(prefix=".plandex-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for line in lines:
                fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def source_files(source):
    """Only explicit files and the native exporter's fixed journal-name pattern."""
    files = set(source.get("files", []))
    unavailable = 0
    for directory in source.get("directories", []):
        try:
            root = local_path(directory)
            for entry in root.iterdir():
                if re.fullmatch(r"journal-[0-9a-f]{32}\.jsonl", entry.name):
                    files.add(str(entry))
        except (OSError, CollectionError):
            unavailable += 1
    return sorted(files), unavailable


def discover(config):
    """Metadata-only discovery of explicit exports; no home traversal or DB probing."""
    validate_config(config)
    result = []
    for source in config["sources"]:
        available = 0
        files, unavailable = source_files(source)
        for filename in files:
            try:
                metadata = local_path(filename, ".jsonl").lstat()
                available += int(stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1)
            except (OSError, CollectionError):
                pass
        result.append({"source": pseudonym(source["source_id"], "source", source["source_id"]),
                       "matched_files": len(files), "available_files": available,
                       "unavailable_directories": unavailable,
                       "status": "export_configured" if available else "export_unavailable",
                       "stock_plandex_backfill": "not_supported"})
    return result


def collect(config, out_dir, store_kind=None, store_path=None):
    """Incremental parsing with append-prefix verification and replay-safe checkpoints.

    All normalized records remain in the private cache. Every run emits the entire
    cached snapshot, so a crash between scanning and downstream ingestion cannot
    strand events behind a committed offset. The ledger's request IDs deduplicate it.
    """
    validate_config(config)
    if bool(store_kind) != bool(store_path):
        raise CollectionError("store kind and store path must be supplied together")
    if store_kind and store_kind not in ("sqlite", "json", "csv"):
        raise CollectionError("unsupported ledger store")
    output = local_path(str(out_dir))
    paths = {str(Path(p)) for source in config["sources"] for p in source.get("files", [])}
    directories = [Path(d) for source in config["sources"] for d in source.get("directories", [])]
    if any(output in Path(p).parents for p in paths):
        raise CollectionError("source exports must be outside the collector output directory")
    if any(output == d or output in d.parents or d in output.parents for d in directories):
        raise CollectionError("export and output directories must be disjoint")
    cache = output / "checkpoint.sqlite"
    if store_path:
        destination = local_path(str(store_path))
        if destination == output or output in destination.parents:
            raise CollectionError("ledger store must be outside the collector output directory")
        if store_kind != "sqlite" and destination in output.parents:
            raise CollectionError("collector output must be outside the ledger store directory")
        if str(destination) in paths or destination == cache:
            raise CollectionError("ledger store, checkpoint and source files must be separate")
        if any(destination == d or d in destination.parents or destination in d.parents for d in directories):
            raise CollectionError("ledger store must be outside export directories")
        if store_kind != "sqlite" and any(destination in Path(p).parents for p in paths):
            raise CollectionError("ledger store directory must not contain source files")
    output = private_directory(output)
    local_path(str(cache))
    if cache.exists():
        metadata = cache.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise CollectionError("checkpoint must be a regular, non-hardlinked file")
    fd = os.open(cache, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    if os.name != "nt":
        os.chmod(cache, 0o600)
    db = sqlite3.connect(cache, timeout=1)
    try:
        db.executescript("""
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS offsets (path TEXT PRIMARY KEY, pos INTEGER NOT NULL, prefix TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events (id TEXT PRIMARY KEY, digest TEXT NOT NULL, payload TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS ends (id TEXT PRIMARY KEY, digest TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS starts (id TEXT PRIMARY KEY);
          CREATE TABLE IF NOT EXISTS gaps (id TEXT PRIMARY KEY, code TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        db.execute("BEGIN IMMEDIATE")
        fmt = db.execute("SELECT value FROM meta WHERE key='format'").fetchone()
        if fmt and fmt[0] != "plandex-checkpoint/1":
            raise CollectionError("unsupported checkpoint format")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('format', 'plandex-checkpoint/1')")
        fingerprint = digest(config)
        previous = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        if previous and previous[0] != fingerprint:
            raise CollectionError("approved scope/configuration changed; use a new checkpoint directory, keeping the same ledger store")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('config', ?)", (fingerprint,))
        counters = Counter()
        for source in config["sources"]:
            files, unavailable = source_files(source)
            counters["unavailable_directories"] += unavailable
            for filename in files:
                path_id = digest([source["source_id"], filename])
                saved = db.execute("SELECT pos, prefix FROM offsets WHERE path=?", (path_id,)).fetchone()
                pos, prior_hash = saved if saved else (0, hashlib.sha256(b"").hexdigest())
                prefix = hashlib.sha256()
                try:
                    fh = read_file(local_path(filename, ".jsonl"))
                except (OSError, CollectionError):
                    counters["unavailable_files"] += 1
                    continue
                with fh:
                    remaining = pos
                    while remaining:
                        chunk = fh.read(min(65536, remaining))
                        if not chunk:
                            break
                        prefix.update(chunk)
                        remaining -= len(chunk)
                    if remaining or prefix.hexdigest() != prior_hash:
                        # Rotation/copy-truncation/rewrite: revalidate each immutable identity.
                        counters["restarted_files"] += 1
                        fh.seek(0)
                        pos, prefix = 0, hashlib.sha256()
                    while True:
                        raw = fh.readline(MAX_LINE + 1)
                        if not raw:
                            break
                        if len(raw) > MAX_LINE:
                            raise CollectionError("export line exceeds safety limit")
                        if not raw.endswith(b"\n"):
                            counters["partial_tail_files"] += 1
                            break
                        counters["parsed_lines"] += 1
                        prefix.update(raw)
                        pos += len(raw)
                        evidence = hashlib.sha256(raw).hexdigest()
                        try:
                            row = decode(raw)
                        except (ValueError, UnicodeError, RecursionError):
                            row = None
                        rid, event, gaps, tokens = normalize(row, source, evidence)
                        if gaps == ["outside_scope"]:
                            counters["outside_scope_records"] += 1
                            continue
                        if gaps == ["attempt_start"]:
                            db.execute("INSERT OR IGNORE INTO starts VALUES (?)", (rid,))
                            continue
                        if rid and isinstance(row, dict) and row.get("kind") == "attempt":
                            # Hash only validated, privacy-filtered evidence, never arbitrary
                            # source fields. A terminal attempt is immutable, even when incomplete.
                            clean_event = dict(event) if event else None
                            if clean_event:
                                clean_event.pop("plandex_evidence")
                            final_digest = digest([clean_event, gaps, tokens, raw_evidence(row)])
                            existing = db.execute("SELECT digest FROM ends WHERE id=?", (rid,)).fetchone()
                            if existing and existing[0] != final_digest:
                                raise CollectionError("conflicting immutable Plandex attempt: " + rid)
                            db.execute("INSERT OR IGNORE INTO ends VALUES (?, ?)", (rid, final_digest))
                        for code in gaps:
                            gap_id = digest([source["source_id"], rid or evidence, code])
                            safe = {"code": code, "attempt": rid, "known_tokens": tokens,
                                    "known_raw_usage": raw_evidence(row) if rid else {}, "record_sha256": evidence}
                            db.execute("INSERT OR IGNORE INTO gaps VALUES (?, ?, ?)", (gap_id, code, canonical(safe)))
                        if event:
                            clean = dict(event)
                            clean.pop("plandex_evidence")
                            ed = digest(clean)
                            old = db.execute("SELECT digest FROM events WHERE id=?", (rid,)).fetchone()
                            if old and old[0] != ed:
                                raise CollectionError("conflicting normalized Plandex attempt: " + rid)
                            cur = db.execute("INSERT OR IGNORE INTO events VALUES (?, ?, ?)", (rid, ed, canonical(event)))
                            counters["new_cached_events"] += cur.rowcount
                    db.execute("INSERT OR REPLACE INTO offsets VALUES (?, ?, ?)", (path_id, pos, prefix.hexdigest()))
        events_path = output / "events.plandex.jsonl"
        with closing(db.execute("SELECT payload FROM events ORDER BY id")) as cursor:
            atomic_lines(events_path, (r[0] for r in cursor))
        with closing(db.execute("SELECT payload FROM gaps ORDER BY id")) as cursor:
            atomic_lines(output / "coverage-records.jsonl", (r[0] for r in cursor))
        receipt = {"schema": "plandex-coverage/1", "scope_sha256": fingerprint,
                   "events": db.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                   "unfinished_attempts": db.execute("SELECT COUNT(*) FROM starts WHERE id NOT IN (SELECT id FROM ends)").fetchone()[0],
                   "coverage_gaps": dict(db.execute("SELECT code, COUNT(*) FROM gaps GROUP BY code")),
                   "scan": dict(counters), "native_cost_is_invoice": False,
                   "cache_pricing_note": "Unreported cache categories are priced as ordinary input, not measured as zero.",
                   "historical_coverage": "Only configured exports; no stock database/client-state backfill."}
        if store_kind:
            import ledger_store
            store = ledger_store.Store.open(store_kind, str(store_path))
            try:
                added, skipped = store.ingest_events_file(str(events_path))
                receipt["ledger_ingest"] = {"added": added, "skipped": skipped}
            finally:
                store.close()
        atomic_lines(output / "coverage.json", [canonical(receipt)])
        db.commit()
        return receipt
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="approved plandex-collector/1 JSON config; absolute local path")
    parser.add_argument("--discover", action="store_true", help="check only configured file metadata")
    parser.add_argument("--out-dir", help="private absolute collector output/checkpoint directory")
    parser.add_argument("--store-kind", choices=("sqlite", "json", "csv"))
    parser.add_argument("--store-path", help="explicit existing ledger destination; not a Plandex database")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.discover:
            print(canonical(discover(config)))
            return 0
        if not args.out_dir:
            raise CollectionError("--out-dir is required for collection")
        receipt = collect(config, args.out_dir, args.store_kind, args.store_path)
        print(canonical(receipt))
        return 1 if (receipt["scan"].get("unavailable_files") or receipt["scan"].get("unavailable_directories")) else 0
    except (CollectionError, OSError, sqlite3.Error) as ex:
        # Never echo source contents, DB credentials, provider errors or raw config.
        print("Plandex collection failed (%s); no source files were modified." % type(ex).__name__, file=sys.stderr)
        if isinstance(ex, CollectionError):
            print(str(ex), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
