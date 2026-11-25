"""Compare local Codex observations with explicit account-counter snapshots."""

from bisect import bisect_left
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from itertools import groupby, zip_longest
import json
from pathlib import Path
from posixpath import normpath
import re
import sqlite3
import tempfile
from uuid import UUID

import safety

SCHEMA = "ai-usage-ledger-lifetime-v1"
ACCOUNT_FIELDS = ("provider", "account", "product", "scope", "scope_id")
SNAPSHOT_FIELDS = (*ACCOUNT_FIELDS, "metric", "metric_label", "tokens", "precision", "source", "observed_at")
EVIDENCE_FIELDS = (*ACCOUNT_FIELDS, "host", "since", "until", "level", "basis")
METRICS = {"output": "output", "total_with_cache": "total"}
TOKEN_FIELDS = ("input_uncached", "cache_read", "cache_write", "output", "reasoning", "total")
VERIFIED_BASES = {"exclusive_account_window", "provider_event_match"}


def _utc(value):
    if not isinstance(value, str) or re.search(r"[.,]\d{7,}", value):
        raise ValueError("expected an ISO timestamp with an explicit offset and up to six decimal places")
    stamp = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("expected an ISO timestamp with an explicit offset")
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _text(value, field):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("expected nonblank text for " + field)
    return value.strip()


def _count(value):
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or value < 0:
        raise ValueError("expected a nonnegative integer token count")
    return value


def _stored_count(value):
    if value in (None, ""):
        return 0
    if type(value) is int:
        return _count(value)
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("invalid stored token count") from error
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError("invalid stored token count")
    return int(number)


def _keys(row, required, optional=()):
    if not isinstance(row, dict) or not set(required) <= row.keys() <= set(required) | set(optional):
        raise ValueError("invalid lifetime input fields, expected " + ", ".join(required))


def _account(row):
    return tuple(row[name] for name in ACCOUNT_FIELDS)


def _snapshot(row):
    _keys(row, SNAPSHOT_FIELDS, ("counter_started_at",))
    snapshot = {k: _text(row[k], k) for k in (*ACCOUNT_FIELDS, "metric", "metric_label", "source")}
    snapshot.update(tokens=_count(row["tokens"]), precision=None if row["precision"] is None else _count(row["precision"]),
                    observed_at=_utc(row["observed_at"]))
    if snapshot["precision"] == 0:
        raise ValueError("snapshot precision must be a positive reporting increment or null")
    if row.get("counter_started_at") is not None:
        snapshot["counter_started_at"] = _utc(row["counter_started_at"])
        if snapshot["counter_started_at"] > snapshot["observed_at"]:
            raise ValueError("counter_started_at follows the observation")
    return snapshot


def _attribution(row):
    _keys(row, EVIDENCE_FIELDS, ("source_path",))
    evidence = {k: _text(row[k], k) for k in (*ACCOUNT_FIELDS, "host", "level", "basis")}
    if evidence["provider"] != "openai" or evidence["product"] != "codex" or evidence["scope"] not in ("account", "workspace"):
        raise ValueError("account evidence currently supports OpenAI Codex account and workspace scopes")
    evidence.update(since=_utc(row["since"]), until=_utc(row["until"]))
    if evidence["since"] >= evidence["until"]:
        raise ValueError("account evidence needs a nonempty time window")
    if evidence["level"] not in ("verified", "inferred"):
        raise ValueError("account evidence level must be verified or inferred")
    if evidence["level"] == "verified" and evidence["basis"] not in VERIFIED_BASES:
        raise ValueError("verified evidence requires a provider match or exclusive account window")
    if "source_path" in row:
        evidence["source_path"] = _text(row["source_path"], "source_path")
        path = _path(evidence["source_path"])
        if not path.startswith("/") and not re.match(r"^[A-Za-z]:/", path):
            raise ValueError("source_path must be absolute")
    return evidence


def read_inputs(paths):
    snapshots, evidence = {}, {}
    for path in paths:
        if safety.is_credential_file(str(path)) or safety.is_credential_file(str(Path(path).resolve())):
            raise ValueError("select an account-counter document")
        safety.refuse_if_shared(str(path), "lifetime input")
        with Path(path).open(encoding="utf-8-sig") as fh:
            document = json.load(fh)
        _keys(document, ("schema",), ("snapshots", "account_evidence"))
        if document["schema"] != SCHEMA:
            raise ValueError("unsupported lifetime input schema")
        for name in ("snapshots", "account_evidence"):
            if not isinstance(document.get(name, []), list):
                raise ValueError(name + " must be an array")
        for row in document.get("snapshots", []):
            record = _snapshot(row)
            key = (*_account(record), record["metric"], record["observed_at"])
            if key in snapshots and snapshots[key] != record:
                raise ValueError("conflicting provider snapshots for the same account, metric and observation")
            snapshots[key] = record
        for row in document.get("account_evidence", []):
            record = _attribution(row)
            evidence[json.dumps(record, sort_keys=True)] = record
    return {"schema": SCHEMA, "snapshots": [snapshots[key] for key in sorted(snapshots)],
            "account_evidence": [evidence[key] for key in sorted(evidence)]}


def _path(value):
    path = value.replace("\\", "/")
    if path[:8].casefold() == "//?/unc/":
        path = "//" + path[8:]
    elif path.startswith("//?/"):
        path = path[4:]
    if re.match(r"^[A-Za-z]:/", path) or path.startswith("//"):
        path = path.casefold()
    return normpath(path) if path and not re.fullmatch(r"[a-z]:/", path) else path


def _matches(record, stamp, source_path):
    if not record["since"] <= stamp < record["until"]:
        return False
    prefix = record.get("source_path")
    if prefix is None:
        return True
    prefix = _path(prefix).rstrip("/")
    target = _path(source_path)
    return target == prefix or target.startswith(prefix + "/")


def _assignment(observations, stamp, by_host):
    verified, inferred = set(), set()
    matched = {}
    for _, host, path, *_ in observations:
        for coverage in by_host.get(host, ()):
            record = coverage["evidence"]
            if _matches(record, stamp, path):
                matched[id(coverage)] = coverage
                (verified if record["level"] == "verified" else inferred).add(_account(record))
    for coverage in matched.values():
        coverage["first"] = min(stamp, coverage["first"] or stamp)
        coverage["last"] = max(stamp, coverage["last"] or stamp)
        coverage["matched_calls"] += 1
    primary = verified or inferred
    identities = {key[:3] for key in primary}
    if len(identities) != 1:
        return {}, primary
    identity = identities.pop()
    assignments, conflicts = {}, set()
    for scope in ("account", "workspace"):
        strong = {key for key in verified if key[:4] == (*identity, scope)}
        weak = {key for key in inferred if key[:4] == (*identity, scope)}
        choices = strong or weak
        if len(choices) == 1:
            assignments[next(iter(choices))] = "confirmed" if strong else "inferred"
        else:
            conflicts.update(choices)
    return assignments, conflicts


def _stage(events, cutoff, inclusive, source_intervals, notes):
    for index, event in enumerate(events):
        if event.get("tool") != "codex":
            continue
        try:
            stamp = _utc(event.get("ts"))
            if stamp > cutoff or (stamp == cutoff and not inclusive):
                continue
            tokens = tuple(_stored_count(event.get(key)) for key in TOKEN_FIELDS)
            uncached, cache_read, cache_write, output, reasoning, total = tokens
            if event.get("total") in (None, "") or total != uncached + cache_read + cache_write + output or reasoning > output:
                raise ValueError("invalid recorded token breakdown")
        except (ValueError, OverflowError):
            notes["unusable_rows"] += 1
            continue
        host, path = str(event.get("host") or "(unknown)"), str(event.get("src") or "")
        row = source_intervals.setdefault(host, {"host": host, "harness": "codex", "status": "recorded",
                                                "first": stamp, "last": stamp, "events": 0})
        row["first"], row["last"] = min(row["first"], stamp), max(row["last"], stamp)
        row["events"] += 1
        try:
            session = str(UUID(str(event.get("session"))))
            if not path:
                raise ValueError("unknown source")
            identity = sha256(json.dumps([session, stamp, *tokens]).encode()).hexdigest()
        except ValueError:
            identity = f"unkeyed-{index}"
            notes["rows_without_copy_key"] += 1
        yield identity, host, path, stamp, str(output), str(total), str(event.get("model") or "")


def _coalesce(db, duplicates):
    # Each source can contain multiple equal observations. Pair copies by ordinal
    # across sources so repeated calls in one source retain their multiplicity.
    rows = db.execute("SELECT identity, host, path, stamp, output, total, model FROM calls ORDER BY identity, host, path, rowid")
    for _, same_call in groupby(rows, key=lambda row: row[0]):
        group = list(same_call)
        known_models = {row[-1] for row in group if row[-1]}
        batches = defaultdict(list)
        for row in group:
            batches[row[-1] if len(known_models) > 1 else ""].append(row)
        for batch in batches.values():
            occurrences = [list(rows) for _, rows in groupby(batch, key=lambda row: row[1:3])]
            for copies in zip_longest(*occurrences):
                present = [row for row in copies if row is not None]
                if len(present) > 1:
                    paths = tuple(row[1:3] for row in present)
                    duplicates[paths] += 1
                yield present


def _status(account, metric, snapshot, evidence, since, end):
    if snapshot is None:
        return "missing_baseline"
    if metric not in METRICS:
        return "incompatible_metric"
    provider, _, product, scope, _ = account
    if provider != "openai" or product != "codex" or scope not in ("account", "workspace"):
        return "incompatible_scope"
    relevant = [row for row in evidence if _account(row)[:3] == account[:3]]
    exact = [row for row in relevant if _account(row) == account]
    if relevant and not exact:
        return "incompatible_scope"
    if not any(row["level"] == "verified" and row["since"] <= end and
               (since is None or row["until"] > since) for row in exact):
        return "unverified_account"
    return "comparable"


def _range(snapshot):
    precision = snapshot["precision"]
    if precision is None:
        return None
    # A reporting increment without a rounding rule permits an error of
    # less than one increment on either side of the displayed integer.
    radius = precision - 1
    return [max(0, snapshot["tokens"] - radius), snapshot["tokens"] + radius]


def _counter_change(epoch):
    uncertain = False
    for before, after in zip(epoch, epoch[1:]):
        if after["tokens"] >= before["tokens"]:
            continue
        old, new = _range(before), _range(after)
        if old is not None and new is not None and new[1] < old[0]:
            return "counter_reset"
        uncertain = True
    return "counter_change_uncertain" if uncertain else "comparable"


def _window(start, end, provider_tokens, *, start_inclusive, bounds=None):
    return {"from": start, "through": end, "start_inclusive": start_inclusive, "provider_tokens": provider_tokens,
            "provider_token_range": bounds, "confirmed_tokens": 0, "inferred_tokens": 0, "unattributed_tokens": 0}


def _comparisons(inputs, cutoff, inclusive):
    by_series = defaultdict(list)
    evidence = inputs["account_evidence"]
    for snapshot in inputs["snapshots"]:
        history = by_series[(*_account(snapshot), snapshot["metric"])]
        if snapshot["observed_at"] < cutoff or (inclusive and snapshot["observed_at"] == cutoff):
            history.append(snapshot)
    represented = {key[:-1] for key in by_series}
    for row in evidence:
        if _account(row) not in represented:
            by_series[(*_account(row), "total_with_cache")]
    for key, history in sorted(by_series.items()):
        history.sort(key=lambda row: row["observed_at"])
        account, metric = key[:-1], key[-1]
        snapshot = history[-1] if history else None
        start = snapshot.get("counter_started_at") if snapshot else None
        end = snapshot["observed_at"] if snapshot else cutoff
        status = _status(account, metric, snapshot, evidence, start, end)
        epoch = []
        for row in reversed(history):
            if row.get("counter_started_at") != start:
                break
            epoch.append(row)
        epoch.reverse()
        entry = {**dict(zip(ACCOUNT_FIELDS, account)), "metric": metric, "snapshot": snapshot,
                 "status": _counter_change(epoch) if status == "comparable" else status,
                 "lifetime": _window(start, end, snapshot["tokens"] if snapshot else None,
                                     start_inclusive=True, bounds=_range(snapshot) if snapshot else None),
                 "intervals": []}
        for previous, current in zip(epoch, epoch[1:]):
            after, through = previous["observed_at"], current["observed_at"]
            period_status = _status(account, metric, current, evidence, after, through)
            if period_status == "comparable":
                period_status = _counter_change([previous, current])
            prev_range, next_range = _range(previous), _range(current)
            bounds = None
            if period_status == "comparable" and prev_range and next_range:
                bounds = [max(0, next_range[0] - prev_range[1]), next_range[1] - prev_range[0]]
            period = _window(after, through, current["tokens"] - previous["tokens"], start_inclusive=False, bounds=bounds)
            period["status"] = period_status
            entry["intervals"].append(period)
        yield entry


def _accumulate(window, stamp, amount, level):
    start = window["from"]
    if stamp <= window["through"] and (start is None or stamp > start or (stamp == start and window["start_inclusive"])):
        window[level + "_tokens"] += amount


def _finish(window, comparable):
    provider = window["provider_tokens"]
    confirmed = window["confirmed_tokens"]
    window.update(difference_tokens=None, difference_range_tokens=None, coverage_percent=None)
    if not comparable:
        return
    window["difference_tokens"] = provider - confirmed
    if bounds := window["provider_token_range"]:
        window["difference_range_tokens"] = [bound - confirmed for bound in bounds]
    if provider:
        # Round to a hundredth of a percent without losing integer precision.
        rounded = (20_000 * confirmed + provider) // (2 * provider)
        window["coverage_percent"] = f"{rounded // 100}.{rounded % 100:02d}"


def _difference_status(window):
    bounds = window["difference_range_tokens"]
    if bounds is None:
        return "unknown_counter_precision"
    lower, upper = bounds
    if lower > 0:
        return "coverage_gap"
    if upper < 0:
        return "ledger_exceeds_counter"
    return "matched" if lower == upper == 0 else "within_reported_precision"


def reconcile(events, archive_events, inputs, end, inclusive, installation_coverage):
    cutoff = _utc(end.isoformat())
    comparisons = list(_comparisons(inputs, cutoff, inclusive))
    interval_ends = [[period["through"] for period in row["intervals"]] for row in comparisons]
    by_host = defaultdict(list)
    evidence_coverage = []
    for record in inputs["account_evidence"]:
        coverage = {"evidence": record, "harness": "codex", "first": None, "last": None, "matched_calls": 0}
        evidence_coverage.append(coverage)
        by_host[record["host"]].append(coverage)
    intervals = {}
    duplicates = defaultdict(int)
    notes = {"unusable_rows": 0, "rows_without_copy_key": 0, "conflicting_attribution_calls": 0}
    observed = {"calls": 0, "output": 0, "total": 0, "unattributed_output": 0, "unattributed_total": 0}
    with tempfile.TemporaryDirectory(prefix="ledger-lifetime-") as tmp:
        path = Path(tmp) / "calls.sqlite"
        with closing(sqlite3.connect(path)) as db:
            db.execute("CREATE TABLE calls (identity TEXT, host TEXT, path TEXT, stamp TEXT, output TEXT, total TEXT, model TEXT)")
            db.executemany("INSERT INTO calls VALUES (?,?,?,?,?,?,?)",
                           _stage((row for source in (events, archive_events) for row in source), cutoff, inclusive, intervals, notes))
            db.execute("CREATE INDEX calls_identity ON calls(identity, host, path)")
            for copies in _coalesce(db, duplicates):
                _, _, _, stamp, out, total, _ = copies[0]
                assignments, conflicts = _assignment(copies, stamp, by_host)
                amounts = {"output": int(out), "total": int(total)}
                observed["calls"] += 1
                observed["output"] += amounts["output"]
                observed["total"] += amounts["total"]
                if conflicts:
                    notes["conflicting_attribution_calls"] += 1
                if not assignments:
                    for name, amount in amounts.items():
                        observed["unattributed_" + name] += amount
                for row, ends in zip(comparisons, interval_ends):
                    field = METRICS.get(row["metric"])
                    key = _account(row)
                    if field is None or (assignments and key not in assignments and key not in conflicts):
                        continue
                    bucket = assignments.get(key, "unattributed")
                    _accumulate(row["lifetime"], stamp, amounts[field], bucket)
                    position = bisect_left(ends, stamp)
                    if position < len(ends):
                        _accumulate(row["intervals"][position], stamp, amounts[field], bucket)
    for row in comparisons:
        _finish(row["lifetime"], row["status"] == "comparable")
        difference = row["lifetime"]["difference_tokens"]
        row["unresolved_difference_tokens"] = difference
        localized = []
        for period in row["intervals"]:
            _finish(period, period["status"] == "comparable")
            if period["difference_tokens"] is not None:
                localized.append(period["difference_tokens"])
                period["status"] = _difference_status(period)
        if difference is not None:
            row["unresolved_difference_tokens"] = difference - sum(localized)
            if localized and len(localized) == len(row["intervals"]):
                row["unresolved_through"] = row["intervals"][0]["from"]
            row["status"] = _difference_status(row["lifetime"])
    for coverage in evidence_coverage:
        coverage["status"] = "observed" if coverage["matched_calls"] else "no_recorded_calls"
    return {"inputs": inputs, "through": cutoff, "end_inclusive": inclusive, "accounts": comparisons, "observed": observed,
            "recorded_sources": list(intervals.values()), "installation_sources": installation_coverage,
            "evidence_coverage": evidence_coverage, "notes": notes,
            "duplicate_sources": [{"paths": [{"host": host, "path": path} for host, path in paths],
                                   "matched_calls": count, "copies_removed": count * (len(paths) - 1)}
                                  for paths, count in sorted(duplicates.items())]}
