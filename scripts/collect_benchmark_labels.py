#!/usr/bin/env python3
"""Benchmark several index candidates on the isolated test database to collect measured ML labels.

Each candidate is created, measured, and dropped in turn, and every run is
appended to the benchmark history file. Lower-priority candidates are included
on purpose: indexes that do not help are informative negative examples.

Requires MySQL, an analyzed workload (``data/index_candidates.csv``), and the
test database (``--clone`` rebuilds it from the primary first).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import List, Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.config import settings
from backend.services.benchmark import run_benchmark_detailed
from backend.services.index_manager import BenchmarkError
from backend.services.test_database import clone_test_database


def load_candidates(limit: int) -> List[dict]:
    path = settings.resolve_path(settings.candidate_path)
    if not path.exists():
        raise FileNotFoundError(f"Candidate file not found: {path}. Run the analysis first.")
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return rows[:limit]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of candidates to benchmark.")
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--clone", action="store_true", help="Rebuild the test database from the primary first.")
    args = parser.parse_args(argv)

    try:
        candidates = load_candidates(args.limit)
        if args.clone:
            clone_test_database()
            print(f"Cloned {settings.db_name} to {settings.db_test_name}.")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Label collection failed: {exc}", file=sys.stderr)
        return 1

    completed = failed = 0
    for row in candidates:
        try:
            record = run_benchmark_detailed(
                candidate_id=row["candidate_id"],
                table=row["table"],
                columns=row["columns"],
                iterations=args.iterations,
                output_path=settings.resolve_path(settings.benchmark_result_path),
                repeats=args.repeats,
                candidate_features=row,
            )
        except (BenchmarkError, ValueError) as exc:
            failed += 1
            print(f"FAILED {row['candidate_id']}: {exc}", file=sys.stderr)
            continue
        completed += 1
        summary = record["summary"]
        print(
            f"{row['candidate_id']}: workload_benefit={summary['workload_benefit']:.4f} "
            f"affected={','.join(summary['affected_queries']) or '-'} "
            f"index_size={record.get('index_size_bytes')}"
        )
    print(f"Completed {completed} benchmark runs, {failed} failed. History: {settings.benchmark_history_path}")
    return 0 if completed else 1


if __name__ == "__main__":
    raise SystemExit(main())
