#!/usr/bin/env python3
"""Inspect and maintain the durable ML training-sample store (``ML_DATASET_PATH``).

Commands
--------
status            Counts by provenance and validity, exclusion reasons, eligibility.
sync              Idempotently ingest data/benchmark_history.jsonl (safe to repeat).
samples           Print samples (``--label-source``, ``--valid-only``, ``--limit``).
export            Write samples with the 21 features and target to a CSV file.
reset --yes       Delete every persisted sample (the history file is untouched).

Backup: the store is a single SQLite file; copy it to keep a snapshot. Retention: nothing
expires automatically. Reset only removes the structured samples; re-running ``sync``
rebuilds them from the history file.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.config import settings
from backend.services.ml.features import FEATURE_COLUMNS
from backend.services.ml.store import measured_eligibility, open_store


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", default=None, help="Store file (default: ML_DATASET_PATH).")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    sub.add_parser("sync")
    samples = sub.add_parser("samples")
    samples.add_argument("--label-source", choices=["measured_benchmark", "synthetic_demo"], default=None)
    samples.add_argument("--valid-only", action="store_true")
    samples.add_argument("--limit", type=int, default=50)
    export = sub.add_parser("export")
    export.add_argument("--output", required=True)
    export.add_argument("--label-source", choices=["measured_benchmark", "synthetic_demo"], default=None)
    reset = sub.add_parser("reset")
    reset.add_argument("--yes", action="store_true", help="Required: confirms deleting all samples.")
    args = parser.parse_args(argv)

    store = open_store(args.path)
    if args.command == "status":
        status = store.status()
        status["eligibility"] = measured_eligibility(status, settings.ml_min_training_samples, settings.ml_min_training_groups)
        print(json.dumps(status, indent=2, default=str))
        return 0
    if args.command == "sync":
        result = store.sync_history(settings.resolve_path(settings.benchmark_history_path))
        print(json.dumps(result, indent=2, default=str))
        return 0
    if args.command == "samples":
        rows = store.samples(label_source=args.label_source, valid_only=args.valid_only, with_features=False)
        for row in list(reversed(rows))[: args.limit]:
            row.pop("raw_snapshot", None)
            row.pop("features", None)
            print(json.dumps(row, default=str))
        print(f"{min(len(rows), args.limit)} of {len(rows)} samples shown", file=sys.stderr)
        return 0
    if args.command == "export":
        rows = store.samples(label_source=args.label_source)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        fields = ["sample_id", "run_id", "candidate_id", "candidate_fingerprint", "table_name", "columns", "label_source",
                  "validation_status", "exclusion_reason", "target", "baseline_workload_median_ms",
                  "indexed_workload_median_ms", "workload_signature", "dataset_hash", "measured_at"] + FEATURE_COLUMNS
        with output.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                features = row.get("features") or {}
                writer.writerow({**row, **{name: ("" if features.get(name) != features.get(name) else features.get(name)) for name in FEATURE_COLUMNS}})
        print(f"Wrote {len(rows)} samples to {output}")
        return 0
    if args.command == "reset":
        if not args.yes:
            print("Refusing to reset without --yes. This deletes every persisted training sample.", file=sys.stderr)
            return 2
        removed = store.reset(confirm=True)
        print(f"Removed {removed} samples from {store.path}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
