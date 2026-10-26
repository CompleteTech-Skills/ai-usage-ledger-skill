"""Synthetic durable Claude identity and conservative legacy compatibility tests."""
import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("identity_compiler", ROOT / "scripts/compile_ai_logs.py")
compiler = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compiler)
import ledger_store as store  # noqa: E402
from anonymize import Anonymizer  # noqa: E402


class ClaudeIdentityEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-identity-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.a = self.root / "projects" / "a" / "session.jsonl"
        self.b = self.root / "projects" / "b" / "session.jsonl"
        self.events = self.root / "events.jsonl"

    @staticmethod
    def row(one_hour=False, mid=None, uuid=None, request=None, tokens=100):
        message = {"model": "synthetic", "usage": {
            "input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": tokens,
            "cache_creation": {"ephemeral_5m_input_tokens": 0 if one_hour else tokens,
                               "ephemeral_1h_input_tokens": tokens if one_hour else 0}}}
        if mid is not None:
            message["id"] = mid
        result = {"type": "assistant", "message": message}
        if uuid is not None:
            result["uuid"] = uuid
        if request is not None:
            result["requestId"] = request
        return result

    @staticmethod
    def write(path, rows):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def scan(self, paths=None):
        paths = paths or [self.a, self.b]
        out = io.StringIO()
        with patch.object(compiler, "walk_files", return_value=iter(map(str, paths))):
            compiler.scan_claude(str(self.root), "synthetic-host", out, compiler.Stats(), set(), [])
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.write(self.events, rows)
        return rows

    def opened(self, kind, name="store"):
        return store.Store.open(kind, str(self.root / (name + "-" + kind)))

    def test_cross_file_growth_reverse_order_reopen_and_export(self):
        for kind in ("sqlite", "json", "csv"):
            with self.subTest(backend=kind):
                self.write(self.a, [self.row()])
                self.write(self.b, [self.row(one_hour=True)])
                self.scan()
                st = self.opened(kind)
                self.assertEqual(st.ingest_events_file(self.events), (2, 0))
                original = {r["id"] for r in st.iter_events()}
                st.close()
                st = self.opened(kind)
                try:
                    self.scan([self.b, self.a])
                    self.assertEqual(st.ingest_events_file(self.events), (0, 2))
                    self.write(self.a, [self.row(), self.row()])
                    self.scan([self.b, self.a])
                    self.assertEqual(st.ingest_events_file(self.events), (1, 2))
                    self.scan()
                    self.assertEqual(st.ingest_events_file(self.events), (0, 3))
                    rows = list(st.iter_events())
                    self.assertTrue(original <= {r["id"] for r in rows})
                    self.assertEqual(sorted((r["project"], r["cache_write_5m"], r["cache_write_1h"]) for r in rows),
                                     [("a", 100, 0), ("a", 100, 0), ("b", 0, 100)])
                    pricing = {"models": {"synthetic": {"input": 1, "output": 1, "cache_read": 1,
                                                       "cache_write_5m": 2, "cache_write_1h": 4}}}
                    self.assertAlmostEqual(sum(compiler.price_event(r, pricing)[0] for r in rows), 0.0008)
                    export = store.export_jsonl(st, str(self.root / (kind + "-export")))["events"][0]
                    self.assertEqual(st.ingest_events_file(export), (0, 3))
                    rebuilt = self.opened(kind, "copy")
                    try:
                        self.assertEqual(rebuilt.ingest_events_file(export), (3, 0))
                        self.assertEqual({r["id"] for r in rebuilt.iter_events()}, {r["id"] for r in rows})
                    finally:
                        rebuilt.close()
                finally:
                    st.close()

    def test_anonymous_and_known_message_rows_no_request_remain_distinct(self):
        self.write(self.a, [self.row(), self.row(mid="x"), self.row(), self.row(mid="y")])
        events = self.scan([self.a])
        self.assertEqual(len({store.event_id(e, 0) for e in events}), 4)
        self.assertTrue(all(e["request_id"] is None for e in events))
        for kind in ("sqlite", "json", "csv"):
            st = self.opened(kind)
            try:
                self.assertEqual(st.ingest_events_file(self.events), (4, 0))
                self.assertEqual(st.ingest_events_file(self.events), (0, 4))
            finally:
                st.close()

    def test_native_message_uuid_precedence_and_cross_file_replay(self):
        for identity in ({"mid": "x", "uuid": "ignored"}, {"uuid": "x"}):
            with self.subTest(identity=identity):
                self.write(self.a, [self.row(**identity)])
                self.write(self.b, [self.row(**identity)])
                first = self.scan([self.a, self.b])
                second = self.scan([self.b, self.a])
                self.assertEqual(len(first), 1)
                self.assertEqual(len(second), 1)
                self.assertEqual(store.event_id(first[0], 0), store.event_id(second[0], 0))
                for kind in ("sqlite", "json", "csv"):
                    st = self.opened(kind, "native-" + next(iter(identity)))
                    try:
                        self.write(self.events, first)
                        self.assertEqual(st.ingest_events_file(self.events), (1, 0))
                        self.write(self.events, second)
                        self.assertEqual(st.ingest_events_file(self.events), (0, 1))
                    finally:
                        st.close()
        self.write(self.a, [self.row(mid="x"), self.row(uuid="x")])
        self.assertEqual(len(self.scan([self.a])), 1)  # existing shared native-ID namespace

    def test_native_request_id_path_is_unchanged_even_with_legacy_history(self):
        self.write(self.a, [self.row(request="native-request")])
        events = self.scan([self.a])
        expected = store._sha("req", "claude-code", "synthetic-host", "native-request")
        self.assertEqual(store.event_id(events[0], 123), expected)
        self.assertNotIn("ledger_identity", events[0])
        for kind in ("sqlite", "json", "csv"):
            st = self.opened(kind)
            try:
                legacy = dict(events[0], id="legacy-unidentified", request_id=None, seq=0)
                st._put_events([legacy])
                self.assertEqual(st.ingest_events_file(self.events), (1, 0))
                self.assertEqual(st.ingest_events_file(self.events), (0, 1))
            finally:
                st.close()

    def test_legacy_history_refuses_without_mutation_and_stays_readable(self):
        self.write(self.a, [self.row(), self.row(mid="x"), self.row(), self.row(mid="y")])
        current = self.scan([self.a])
        # Main omitted the second anonymous row; old PR retained all four but had
        # no message IDs/line provenance in the event output. Neither can be rekeyed.
        for label, indices in (("main", (0, 1, 3)), ("old-pr", (0, 1, 2, 3)), ("one-row", (0,))):
            for kind in ("sqlite", "json", "csv"):
                with self.subTest(version=label, backend=kind):
                    st = self.opened(kind, label)
                    legacy = []
                    for seq, index in enumerate(indices):
                        e = dict(current[index])
                        e.pop("ledger_identity")
                        e.update(seq=seq, id=store._sha(*(store.event_key(e) + (seq,))))
                        legacy.append(e)
                    st._put_events(legacy)
                    before = list(st.iter_events())
                    try:
                        with self.assertRaisesRegex(ValueError, "NEW store/workdir"):
                            st.ingest_events_file(self.events)
                        self.assertEqual(list(st.iter_events()), before)
                        exported = store.export_jsonl(st, str(self.root / (label + kind + "-export")))["events"][0]
                        self.assertTrue(Path(exported).exists())
                        fresh = self.opened(kind, label + "-new")
                        try:
                            with self.assertRaisesRegex(ValueError, "Old exports cannot recover"):
                                fresh.ingest_events_file(exported)
                            self.assertEqual(fresh.counts()["events"], 0)
                            self.assertEqual(fresh.ingest_events_file(self.events), (4, 0))
                        finally:
                            fresh.close()
                    finally:
                        st.close()

    def test_late_bad_provenance_or_legacy_state_prevents_all_5001_writes(self):
        self.write(self.a, [self.row()])
        event = self.scan([self.a])[0]
        safe = [{"tool": "synthetic", "host": "test", "request_id": str(i), "total": 1} for i in range(5001)]
        for legacy_state in (False, True):
            for kind in ("sqlite", "json", "csv"):
                with self.subTest(legacy_state=legacy_state, backend=kind):
                    st = self.opened(kind, "late-" + str(legacy_state))
                    bad = dict(event)
                    bad.pop("ledger_identity")
                    bad.update(id="legacy", seq=0)
                    if legacy_state:
                        st._put_events([bad])
                    self.write(self.events, safe + [event if legacy_state else bad])
                    before = list(st.iter_events())
                    try:
                        with self.assertRaisesRegex(ValueError, "No events were imported"):
                            st.ingest_events_file(self.events)
                        self.assertEqual(list(st.iter_events()), before)
                        if kind != "sqlite":
                            self.assertEqual(st.counts()["events"], len(before))
                    finally:
                        st.close()

    def test_source_line_rewrite_and_native_conflicting_usage_refuse(self):
        for mid in (None, "native-x"):
            for kind in ("sqlite", "json", "csv"):
                with self.subTest(mid=mid, backend=kind):
                    self.write(self.a, [self.row(mid=mid)])
                    self.scan([self.a])
                    st = self.opened(kind, "rewrite-" + str(mid))
                    try:
                        st.ingest_events_file(self.events)
                        before = list(st.iter_events())
                        self.write(self.a, [self.row(one_hour=True, mid=mid)])
                        self.scan([self.a])
                        with self.assertRaisesRegex(ValueError, "Conflicting Claude"):
                            st.ingest_events_file(self.events)
                        self.assertEqual(list(st.iter_events()), before)
                    finally:
                        st.close()

    def test_provenance_validation_and_structured_hash_framing(self):
        self.write(self.a, [self.row()])
        e = self.scan([self.a])[0]
        for change in ({"src": None}, {"src": "relative.jsonl"}, {"ledger_identity": None}):
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "rescan retained authorized"):
                store.event_id(dict(e, **change), 0)
        for field, value in (("line", True), ("line", 0), ("version", 2), ("digest", "0" * 64), ("source_sha256", "0" * 64)):
            changed = copy.deepcopy(e)
            changed["ledger_identity"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                store.event_id(changed, 0)
        self.assertNotEqual(store._identity_digest(["a|b", "c"]), store._identity_digest(["a", "b|c"]))
        self.assertNotEqual(store._identity_digest([None]), store._identity_digest([""]))

    def test_precise_counts_and_private_identity_exports(self):
        self.write(self.a, [self.row(tokens=2**53 + 1)])
        events = self.scan([self.a])
        digest = events[0]["ledger_identity"]["digest"]
        for kind in ("sqlite", "json", "csv"):
            with self.subTest(backend=kind):
                st = self.opened(kind)
                try:
                    st.ingest_events_file(self.events)
                    row = next(iter(st.iter_events()))
                    self.assertEqual(row["total"], 2**53 + 1)
                    self.assertEqual(row["cache_write_5m"], 2**53 + 1)
                    self.assertEqual(row["ledger_identity"], events[0]["ledger_identity"])
                    anon = Anonymizer("synthetic-salt").event(row)
                    self.assertNotIn("ledger_identity", anon)
                    for fmt in ("json", "jsonl", "csv"):
                        target = self.root / (kind + ".anon." + fmt)
                        store.export_rows([anon], "events", fmt, str(target))
                        text = target.read_text()
                        self.assertNotIn(digest, text)
                        self.assertNotIn(str(self.a), text)
                    # Public CSV is an analysis export, not a re-ingest format: it
                    # has always omitted extras. Missing identity must fail closed.
                    target = self.root / (kind + ".public.csv")
                    store.export_rows([row], "events", "csv", str(target))
                    with target.open(newline="") as fh:
                        csv_row = next(csv.DictReader(fh))
                    with self.assertRaisesRegex(ValueError, "Old exports cannot recover"):
                        store.event_id(csv_row, 0)
                finally:
                    st.close()

    def test_scanner_scalar_evidence_replays_across_backend_representations(self):
        cases = {
            "empty-request": dict(self.row(), requestId=""),
            "empty-effort": dict(self.row(), effort=""),
            "numeric-effort": dict(self.row(), effort=1),
            "integral-float-counts": self.row(tokens=100.0),
            "empty-native-model": self.row(mid="native-empty-model"),
        }
        cases["empty-native-model"]["message"]["model"] = ""
        for label, native in cases.items():
            for kind in ("sqlite", "json", "csv"):
                with self.subTest(case=label, backend=kind):
                    self.write(self.a, [native])
                    self.scan([self.a])
                    st = self.opened(kind, label)
                    self.assertEqual(st.ingest_events_file(self.events), (1, 0))
                    st.close()
                    st = self.opened(kind, label)
                    try:
                        self.assertEqual(st.ingest_events_file(self.events), (0, 1))
                        exported = store.export_jsonl(st, str(self.root / (label + kind + "-export")))["events"][0]
                        self.assertEqual(st.ingest_events_file(exported), (0, 1))
                        for destination in ("sqlite", "json", "csv"):
                            copy_store = self.opened(destination, label + kind + "-copy")
                            try:
                                self.assertEqual(copy_store.ingest_events_file(exported), (1, 0))
                                self.assertEqual(copy_store.ingest_events_file(self.events), (0, 1))
                            finally:
                                copy_store.close()
                    finally:
                        st.close()

    def test_falsy_native_request_ids_normalize_and_replay_across_backends(self):
        absent = [None, "", False, 0, 0.0, -0.0, [], {}]
        native_ids = ["0", "False", "native-request"]
        for identity_kind in ("anonymous", "message", "uuid"):
            rows = []
            for index, request in enumerate(absent + native_ids):
                native = self.row()
                native["requestId"] = request
                if identity_kind == "message":
                    native["message"]["id"] = "message-" + str(index)
                elif identity_kind == "uuid":
                    native["uuid"] = "uuid-" + str(index)
                rows.append(native)
            rows.append(self.row())  # genuinely absent requestId
            self.write(self.a, rows)
            events = self.scan([self.a])
            for index, event in enumerate(events):
                if index < len(absent) or index == len(rows) - 1:
                    self.assertIsNone(event["request_id"])
                    self.assertIn("ledger_identity", event)
                else:
                    request = native_ids[index - len(absent)]
                    self.assertEqual(event["request_id"], request)
                    self.assertNotIn("ledger_identity", event)
                    self.assertEqual(store.event_id(event, 999),
                                     store._sha("req", "claude-code", "synthetic-host", request))
            expected_ids = {store.event_id(event, 0) for event in events}
            self.assertEqual(len(expected_ids), len(rows))
            for kind in ("sqlite", "json", "csv"):
                with self.subTest(identity_kind=identity_kind, backend=kind):
                    name = "null-request-" + identity_kind
                    st = self.opened(kind, name)
                    self.assertEqual(st.ingest_events_file(self.events), (len(rows), 0))
                    st.close()
                    st = self.opened(kind, name)
                    try:
                        self.scan([self.a])
                        self.assertEqual(st.ingest_events_file(self.events), (0, len(rows)))
                        exported = store.export_jsonl(st, str(self.root / (name + kind + "-export")))["events"][0]
                        self.assertEqual(st.ingest_events_file(exported), (0, len(rows)))
                        self.assertEqual({r["id"] for r in st.iter_events()}, expected_ids)
                        for destination in ("sqlite", "json", "csv"):
                            copy_name = name + kind + "-copy"
                            copy_store = self.opened(destination, copy_name)
                            self.assertEqual(copy_store.ingest_events_file(exported), (len(rows), 0))
                            copy_store.close()
                            copy_store = self.opened(destination, copy_name)
                            try:
                                self.assertEqual(copy_store.ingest_events_file(self.events), (0, len(rows)))
                                self.assertEqual(copy_store.ingest_events_file(exported), (0, len(rows)))
                                self.assertEqual({r["id"] for r in copy_store.iter_events()}, expected_ids)
                            finally:
                                copy_store.close()
                    finally:
                        st.close()

    def test_noncanonical_versioned_request_ids_refuse_before_any_writes(self):
        self.write(self.a, [self.row()])
        event = self.scan([self.a])[0]
        safe = [{"tool": "synthetic", "host": "test", "request_id": str(i), "total": 1} for i in range(5001)]
        # Include the text produced by older CSV stores: provenance must stop
        # those values taking the native-request branch and duplicating usage.
        for kind in ("sqlite", "json", "csv"):
            st = self.opened(kind)
            try:
                for request in ("", False, 0, 0.0, -0.0, [], {}, "False", "0", "[]", "{}"):
                    with self.subTest(backend=kind, request=request):
                        bad = dict(event, request_id=request)
                        with self.assertRaisesRegex(ValueError, "requires a null request_id"):
                            store.event_id(bad, 0)
                        self.write(self.events, safe + [bad])
                        with self.assertRaisesRegex(ValueError, "No events were imported"):
                            st.ingest_events_file(self.events)
                        self.assertEqual(list(st.iter_events()), [])
                        self.assertEqual(st.counts()["events"], 0)
                self.write(self.events, [event])
                self.assertEqual(st.ingest_events_file(self.events), (1, 0))
                self.assertEqual(st.ingest_events_file(self.events), (0, 1))
            finally:
                st.close()

    def test_older_malformed_versioned_store_and_exports_refuse_without_rewriting(self):
        self.write(self.a, [self.row()])
        event = self.scan([self.a])[0]
        for index, request in enumerate((False, 0)):
            for kind in ("sqlite", "json", "csv"):
                with self.subTest(backend=kind, request=request):
                    name = "old-malformed-" + str(index)
                    st = self.opened(kind, name)
                    # Reproduce the older candidate's persisted representation;
                    # this private insertion helper is not an ingestion API.
                    st._put_events([dict(event, request_id=request, seq=0, id=store.event_id(event, 0))])
                    st.close()
                    st = self.opened(kind, name)
                    try:
                        before = list(st.iter_events())
                        with self.assertRaisesRegex(ValueError, "requires a null request_id"):
                            st.ingest_events_file(self.events)
                        self.assertEqual(list(st.iter_events()), before)
                        exported = store.export_jsonl(st, str(self.root / (name + kind + "-export")))["events"][0]
                        for destination in ("sqlite", "json", "csv"):
                            fresh = self.opened(destination, name + kind + "-fresh")
                            try:
                                with self.assertRaisesRegex(ValueError, "requires a null request_id"):
                                    fresh.ingest_events_file(exported)
                                self.assertEqual(fresh.counts()["events"], 0)
                            finally:
                                fresh.close()
                    finally:
                        st.close()

    def test_enriched_numeric_cost_store_compatibility_and_real_conflicts(self):
        # Claude's native scanner does not emit cost_usd. These are explicitly
        # enriched event/store probes, not native provider cost acceptance.
        self.write(self.a, [self.row(tokens=2**53 + 1)])
        original = self.scan([self.a])[0]
        for cost in (0, 0.0, -0.0, 0.125):
            for kind in ("sqlite", "json", "csv"):
                with self.subTest(cost=cost, backend=kind):
                    event = dict(original, cost_usd=cost)
                    self.write(self.events, [event])
                    name = "enriched-" + repr(cost)
                    st = self.opened(kind, name)
                    self.assertEqual(st.ingest_events_file(self.events), (1, 0))
                    st.close()
                    st = self.opened(kind, name)
                    try:
                        self.assertEqual(st.ingest_events_file(self.events), (0, 1))
                        before = list(st.iter_events())
                        self.assertEqual(before[0]["total"], 2**53 + 1)
                        exported = store.export_jsonl(st, str(self.root / (name + kind + "-export")))["events"][0]
                        self.assertEqual(st.ingest_events_file(exported), (0, 1))
                        for change in ({"cost_usd": None}, {"cost_usd": cost + 1}, {"cost_usd": False},
                                       {"total": 2**53}, {"cache_write_5m": 2**53}, {"effort": "changed"}):
                            self.write(self.events, [dict(event, **change)])
                            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Conflicting Claude"):
                                st.ingest_events_file(self.events)
                            self.assertEqual(list(st.iter_events()), before)
                    finally:
                        st.close()

    def test_numeric_evidence_is_exact_beyond_decimal_context_precision(self):
        large = 10**80 + 1
        self.assertEqual(store._exact_numeric_evidence(large), store._exact_numeric_evidence(str(large)))
        self.assertNotEqual(store._exact_numeric_evidence(large), store._exact_numeric_evidence(large - 1))
        self.assertEqual(store._exact_numeric_evidence(100.0), store._exact_numeric_evidence(100))
        self.assertEqual(store._exact_numeric_evidence(0), store._exact_numeric_evidence(-0.0))
        self.assertNotEqual(store._exact_numeric_evidence(False), store._exact_numeric_evidence(0))


if __name__ == "__main__":
    unittest.main()
