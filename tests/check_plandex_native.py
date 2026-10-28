"""Check fresh exports from the patched Plandex model-client contract test.

Explicit invocation only: --exports contains synthetic Go-test artifacts, not
personal Plandex state. Uses temporary ledger stores and never contacts providers.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import collect_plandex as collector  # noqa: E402
import ledger_store  # noqa: E402
import scoped_report  # noqa: E402

REVISION = "e2d772072efadbe41d2946d97d79be55532dbab5"
VERSION = REVISION + "+usage-export.2"


def verify(exports: Path, evidence: Path):
    inputs = {name: (exports / name).read_bytes() for name in (
        "native-export.jsonl", "native-grown.jsonl", "native-gateway.jsonl")}
    rows = [json.loads(line) for line in inputs["native-export.jsonl"].splitlines()]
    assert len(rows) == 4 and all(row["plandex_version"] == VERSION for row in rows)
    dates = sorted(row["ts"][:10] for row in rows)
    results = {}
    with tempfile.TemporaryDirectory(prefix="plandex-native-ledger-") as temporary:
        root = Path(temporary)
        for kind in ("sqlite", "json", "csv"):
            directory = root / kind
            directory.mkdir()
            source = directory / "native.jsonl"
            source.write_bytes(inputs["native-export.jsonl"])
            config = {"schema": "plandex-collector/1", "approved_project_scope": True,
                      "sources": [{"source_id": "synthetic-server", "project_ids": ["project-a", "project-b"],
                                   "versions": [VERSION], "files": [str(source)]}]}
            checkpoint, store_path = directory / "checkpoint", directory / "store"
            first = collector.collect(config, checkpoint, kind, store_path)
            assert first["ledger_ingest"] == {"added": 2, "skipped": 0}, first
            replay = collector.collect(config, checkpoint, kind, store_path)
            assert replay["ledger_ingest"] == {"added": 0, "skipped": 2}, replay
            source.write_bytes(inputs["native-grown.jsonl"])
            growth = collector.collect(config, checkpoint, kind, store_path)
            assert growth["ledger_ingest"] == {"added": 1, "skipped": 2}, growth
            source.write_bytes(inputs["native-gateway.jsonl"])
            gateway = collector.collect(config, checkpoint, kind, store_path)
            assert gateway["ledger_ingest"] == {"added": 0, "skipped": 3}, gateway
            assert gateway["coverage_gaps"]["unverified_gateway_usage"] == 1, gateway
            store = ledger_store.Store.open(kind, str(store_path))
            try:
                events = list(store.iter_events())
                assert len(events) == 3 and sum(event["total"] for event in events) == 3300
                assert sum(event["input_uncached"] for event in events) == 2250
                assert sum(event["cache_read"] for event in events) == 600
                assert sum(event["cache_write"] for event in events) == 150
                assert sum(event["reasoning"] for event in events) == 90
                assert store.counts()["prompts"] == 0
                assert all(event.get("cost_usd") in (None, "") for event in events)
                assert all(event["plandex_native_cost"]["amount"] == "0.00123" for event in events)
            finally:
                store.close()
            prices = directory / "prices.json"
            prices.write_text(json.dumps({"_as_of": dates[0], "models": {
                model: {"input": 2, "cache_read": .2, "cache_write_5m": 2.5, "cache_write_1h": 4,
                        "output": 10, "source": "synthetic-native-contract", "service_tier": "standard"}
                for model in ("gpt-4.1", "gpt-4.1-mini")}}))
            report_config = {"store": {"kind": kind, "path": str(store_path)}, "timezone": "UTC",
                             "workdir": str(directory / "reports"), "pricing_path": str(prices)}
            output, report = scoped_report.generate_report(
                report_config, dates[0], until=dates[-1], now=datetime.now(timezone.utc))
            assert report["totals"]["calls"] == 3 and report["totals"]["total"] == 3300
            assert abs(report["totals"]["api_equivalent_usd"] - .007995) < 1e-12
            assert "plandex" in (output / "report.html").read_text().lower()
            results[kind] = {"first_added": 2, "replay_added": 0, "growth_added": 1,
                             "gateway_added": 0, "calls": 3, "tokens": 3300,
                             "synthetic_api_equivalent_usd": report["totals"]["api_equivalent_usd"]}
    for name, original in inputs.items():
        assert (exports / name).read_bytes() == original, "producer evidence was mutated"
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps({"upstream_revision": REVISION, "export_version": VERSION,
                                   "source_sha256": {name: hashlib.sha256(value).hexdigest()
                                                     for name, value in inputs.items()},
                                   "backends": results, "live_provider_calls": False}, indent=2) + "\n")
    print("Plandex native producer/consumer contracts OK: 3 backends, replay, growth, gateway exclusion")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exports", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    with patch("socket.socket", side_effect=AssertionError("native ledger verification must stay offline")):
        verify(args.exports.resolve(), args.evidence.resolve())
