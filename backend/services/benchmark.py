"""Run repeatable before-and-after benchmarks against the test database."""

from __future__ import annotations

import argparse
import csv
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import mysql.connector
from mysql.connector import MySQLConnection
from mysql.connector.errors import Error as MySQLError

from backend.config import settings
from backend.services.index_manager import IndexApplication, apply_index
from scripts.workload import build_query_arguments, build_workload_plan, fetch_id_bounds


DEFAULT_OUTPUT_PATH = Path("data/benchmark_results.csv")


@dataclass(frozen=True)
class ReplayQuery:
    """One query and its fixed parameters in the benchmark replay plan."""

    query_id: str
    template: str
    params: tuple[Any, ...]


def percentile(values: Sequence[float], percentile_rank: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""

    if not values:
        raise ValueError("Cannot calculate a percentile for an empty sample")
    if not 0 <= percentile_rank <= 100:
        raise ValueError("Percentile rank must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_rank / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def compare_timings(before: Sequence[float], after: Sequence[float]) -> dict[str, float | int]:
    """Calculate before/after timing metrics from actual execution samples."""

    if not before or not after:
        raise ValueError("Before and after timing samples are required")

    before_average = statistics.fmean(before)
    after_average = statistics.fmean(after)
    speedup = before_average / after_average if after_average else 0.0
    improvement = ((before_average - after_average) / before_average * 100) if before_average else 0.0
    return {
        "before_avg_ms": round(before_average, 3),
        "before_min_ms": round(min(before), 3),
        "before_max_ms": round(max(before), 3),
        "before_p95_ms": round(percentile(before, 95), 3),
        "after_avg_ms": round(after_average, 3),
        "after_min_ms": round(min(after), 3),
        "after_max_ms": round(max(after), 3),
        "after_p95_ms": round(percentile(after, 95), 3),
        "execution_count": len(before),
        "speedup": round(speedup, 3),
        "improvement_percentage": round(improvement, 3),
    }


def _test_database_config() -> dict[str, object]:
    if settings.db_name == settings.db_test_name:
        raise ValueError("DB_TEST_NAME must differ from DB_NAME")
    config = settings.mysql_config()
    config["database"] = settings.db_test_name
    return config


def _connect_to_test_database() -> MySQLConnection:
    try:
        return mysql.connector.connect(**_test_database_config())
    except MySQLError as exc:
        raise RuntimeError(
            f"Unable to connect to test database '{settings.db_test_name}'. "
            "Run the test database setup first and check .env."
        ) from exc


def _build_replay_plan(iterations: int) -> list[ReplayQuery]:
    if iterations <= 0:
        raise ValueError("Benchmark iterations must be greater than zero")

    connection = _connect_to_test_database()
    cursor = connection.cursor()
    try:
        bounds = fetch_id_bounds(cursor)
        plan = build_workload_plan(iterations)
        return [
            ReplayQuery(
                query_id=item["query_id"],
                template=item["query"]["template"],
                params=build_query_arguments(bounds, item["query"]),
            )
            for item in plan
        ]
    finally:
        cursor.close()
        connection.close()


def _execute_replay(plan: Iterable[ReplayQuery]) -> dict[str, list[float]]:
    connection = _connect_to_test_database()
    cursor = connection.cursor()
    timings: dict[str, list[float]] = {}
    try:
        for query in plan:
            start = time.perf_counter()
            cursor.execute(query.template, query.params)
            cursor.fetchall()
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            timings.setdefault(query.query_id, []).append(elapsed_ms)
        return timings
    except MySQLError as exc:
        raise RuntimeError("Benchmark query execution failed.") from exc
    finally:
        cursor.close()
        connection.close()


def _write_results(rows: Sequence[dict[str, object]], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "candidate_id",
        "query_id",
        "before_avg_ms",
        "before_min_ms",
        "before_max_ms",
        "before_p95_ms",
        "after_avg_ms",
        "after_min_ms",
        "after_max_ms",
        "after_p95_ms",
        "speedup",
        "improvement_percentage",
        "execution_count",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run_benchmark(
    candidate_id: str,
    table: str,
    columns: str,
    iterations: int = 100,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> list[dict[str, object]]:
    """Benchmark one index candidate using the same replay before and after."""

    plan = _build_replay_plan(iterations)
    _execute_replay(plan)
    before = _execute_replay(plan)
    application: IndexApplication = apply_index(candidate_id, table, columns)
    _execute_replay(plan)
    after = _execute_replay(plan)

    rows: list[dict[str, object]] = []
    for query_id in sorted(before):
        metrics = compare_timings(before[query_id], after.get(query_id, []))
        rows.append({"candidate_id": application.candidate_id, "query_id": query_id, **metrics})

    _write_results(rows, output_path)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark a QueryScale index recommendation.")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--columns", required=True, help="Comma-separated index columns.")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--output-path", default=str(DEFAULT_OUTPUT_PATH))
    args = parser.parse_args()

    rows = run_benchmark(
        args.candidate_id,
        args.table,
        args.columns,
        args.iterations,
        args.output_path,
    )
    print(f"Benchmarked {len(rows)} query groups for {args.candidate_id}.")
    print(f"Results written to: {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())