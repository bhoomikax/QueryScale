#!/usr/bin/env python3
"""Run the complete QueryScale local pipeline from database to dashboard data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

import mysql.connector
from mysql.connector.errors import Error as MySQLError


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.config import settings
from backend.database import close_connection, open_connection
from backend.services.benchmark import run_benchmark
from backend.services.pipeline import run_pipeline
from backend.services.test_database import clone_test_database
from scripts.run_optimization import _select_recommendation
from scripts.seed import (
    DEFAULT_CONFIG,
    generate_dataset,
    insert_dataset,
    make_connection as make_source_connection,
    reset_tables,
)
from scripts.workload import execute_batch, make_connection as make_workload_connection


def _server_config() -> dict[str, object]:
    config = settings.mysql_config()
    config.pop("database", None)
    config["raise_on_warnings"] = False
    return config


def initialize_schema() -> None:
    """Recreate the local source schema from database/schema.sql."""

    schema_path = ROOT_DIR / "database" / "schema.sql"
    statements = [statement.strip() for statement in schema_path.read_text(encoding="utf-8").split(";")]
    try:
        connection = open_connection(_server_config())
        cursor = connection.cursor()
        for statement in statements:
            if statement:
                cursor.execute(statement)
        connection.commit()
    except MySQLError as exc:
        raise RuntimeError(f"Unable to initialize the QueryScale source schema: {exc}") from exc
    finally:
        if "cursor" in locals():
            cursor.close()
        if "connection" in locals():
            close_connection(connection)


def seed_source_database(config: dict[str, int]) -> None:
    """Reset and populate the configured source database with synthetic data."""

    connection = make_source_connection()
    try:
        cursor = connection.cursor()
        reset_tables(cursor)
        insert_dataset(cursor, generate_dataset(config))
        connection.commit()
    except MySQLError as exc:
        raise RuntimeError("Unable to seed the QueryScale source database.") from exc
    finally:
        if "cursor" in locals():
            cursor.close()
        close_connection(connection)


def run_workload(iterations: int, log_path: Path) -> None:
    """Execute the real workload once and replace any stale query log."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        log_path.unlink()
    connection = make_workload_connection()
    try:
        execute_batch(connection, iterations, str(log_path))
    except MySQLError as exc:
        raise RuntimeError("Unable to execute the QueryScale workload.") from exc
    finally:
        close_connection(connection)


def run_local_pipeline(
    iterations: int,
    initialize: bool,
    seed_config: dict[str, int],
    log_path: Path,
    feature_path: Path,
    candidate_path: Path,
    recommendation_path: Path,
    benchmark_path: Path,
    candidate_id: str | None,
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    """Execute all local data, recommendation, test DB, and benchmark stages."""

    if initialize:
        print("[1/7] Initializing source schema")
        initialize_schema()
    print("[2/7] Seeding source database")
    seed_source_database(seed_config)
    print("[3/7] Executing measured workload")
    run_workload(iterations, log_path)
    print("[4/7] Generating recommendations")
    recommendations = run_pipeline(log_path, feature_path, candidate_path, recommendation_path)
    recommendation = _select_recommendation(recommendations, candidate_id)
    print(f"      Selected {recommendation['candidate_id']}")
    print("[5/7] Rebuilding isolated test database")
    clone_test_database()
    print("[6/7] Running before/after benchmark")
    results = run_benchmark(
        candidate_id=str(recommendation["candidate_id"]),
        table=str(recommendation["table"]),
        columns=str(recommendation["columns"]),
        iterations=iterations,
        output_path=benchmark_path,
    )
    print("[7/7] API and dashboard artifacts ready")
    return recommendation, results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the complete QueryScale local pipeline.")
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument("--initialize-schema", action="store_true", help="Recreate the source schema before seeding.")
    parser.add_argument("--candidate-id", help="Candidate to benchmark; defaults to the highest-ranked recommendation.")
    parser.add_argument("--serve", action="store_true", help="Start the API and dashboard after the pipeline completes.")
    parser.add_argument("--branches", type=int, default=DEFAULT_CONFIG["branches"])
    parser.add_argument("--customers", type=int, default=DEFAULT_CONFIG["customers"])
    parser.add_argument("--accounts", type=int, default=DEFAULT_CONFIG["accounts"])
    parser.add_argument("--transactions", type=int, default=DEFAULT_CONFIG["transactions"])
    parser.add_argument("--loans", type=int, default=DEFAULT_CONFIG["loans"])
    args = parser.parse_args(argv)

    paths = {
        "log": ROOT_DIR / "data" / "query_logs.csv",
        "feature": ROOT_DIR / "data" / "candidate_features.csv",
        "candidate": ROOT_DIR / "data" / "index_candidates.csv",
        "recommendation": ROOT_DIR / "data" / "recommendations.csv",
        "benchmark": ROOT_DIR / "data" / "benchmark_results.csv",
    }
    config = {
        "branches": args.branches,
        "customers": args.customers,
        "accounts": args.accounts,
        "transactions": args.transactions,
        "loans": args.loans,
    }

    try:
        recommendation, results = run_local_pipeline(
            iterations=args.iterations,
            initialize=args.initialize_schema,
            seed_config=config,
            log_path=paths["log"],
            feature_path=paths["feature"],
            candidate_path=paths["candidate"],
            recommendation_path=paths["recommendation"],
            benchmark_path=paths["benchmark"],
            candidate_id=args.candidate_id,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Local pipeline failed: {exc}", file=sys.stderr)
        return 1

    print(f"Recommendation: {recommendation['candidate_id']}")
    print(f"Benchmark groups: {len(results)}")
    print(f"Dashboard: http://127.0.0.1:{settings.api_port}/dashboard/")
    if args.serve:
        import uvicorn

        uvicorn.run("backend.app:app", host=settings.api_host, port=settings.api_port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
