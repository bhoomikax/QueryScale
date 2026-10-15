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
from backend.services.benchmark import run_benchmark_detailed
from backend.services.pipeline import run_pipeline
from backend.services.schema_metadata import load_metadata
from backend.services.test_database import assert_distinct_databases, clone_test_database, quote_identifier
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


def schema_statements(database: str, schema_path: Path | None = None) -> list[str]:
    """Statements from database/schema.sql retargeted at ``database``.

    schema.sql begins with ``CREATE DATABASE`` and ``USE`` for `queryscale`. Those
    two statements are replaced so initialization always targets the configured
    DB_NAME. Any other database-level statement is rejected.
    """

    path = schema_path or ROOT_DIR / "database" / "schema.sql"
    quoted = quote_identifier(database)
    statements = [
        f"CREATE DATABASE IF NOT EXISTS {quoted} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
        f"USE {quoted}",
    ]
    for raw in path.read_text(encoding="utf-8").split(";"):
        statement = raw.strip()
        upper = " ".join(statement.upper().split())
        if not statement or upper.startswith(("CREATE DATABASE", "USE ")):
            continue
        if upper.startswith(("DROP DATABASE", "ALTER DATABASE")):
            raise ValueError(f"Refusing database-level statement in schema.sql: {statement[:60]}")
        statements.append(statement)
    return statements


def initialize_schema() -> None:
    """Drop and recreate the five QueryScale tables in the configured DB_NAME (destructive)."""

    statements = schema_statements(settings.db_name)
    try:
        connection = mysql.connector.connect(**_server_config())
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
            connection.close()


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
        connection.close()


def run_workload(iterations: int, log_path: Path, seed: int | None = None) -> None:
    """Execute the real workload once and replace any stale query log."""

    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists():
        log_path.unlink()
    connection = make_workload_connection()
    try:
        execute_batch(connection, iterations, str(log_path), seed)
    except MySQLError as exc:
        raise RuntimeError("Unable to execute the QueryScale workload.") from exc
    finally:
        connection.close()


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
    seed: int | None = None,
    collect_labels: int = 0,
    train: bool = False,
) -> tuple[dict[str, Any], list[dict[str, object]]]:
    """Execute all local data, recommendation, test DB, and benchmark stages."""

    # Validate names before any destructive step (seeding truncates DB_NAME, cloning drops DB_TEST_NAME).
    assert_distinct_databases()
    if initialize:
        print("[1/8] Initializing source schema")
        initialize_schema()
    print("[2/8] Seeding source database")
    seed_source_database(seed_config)
    print("[3/8] Executing measured workload (writes are rolled back)")
    run_workload(iterations, log_path, seed)
    print("[4/8] Collecting schema statistics (read-only)")
    metadata = load_metadata(source="mysql")
    print(f"      {len(metadata.tables)} tables, server {metadata.server_version}")
    print("[5/8] Extracting features and ranking recommendations")
    recommendations = run_pipeline(
        log_path, feature_path, candidate_path, recommendation_path,
        metadata=metadata, query_feature_path=feature_path.with_name("query_features.csv"),
    )
    recommendation = _select_recommendation(recommendations, candidate_id)
    print(f"      {len(recommendations)} recommendations ({recommendations[0]['ranking_method']} ranking); selected {recommendation['candidate_id']}")
    print("[6/8] Rebuilding isolated test database")
    clone_test_database()
    if collect_labels:
        from scripts.collect_benchmark_labels import main as collect_main

        print(f"      Collecting measured labels for up to {collect_labels} candidates")
        collect_main(["--limit", str(collect_labels), "--iterations", str(iterations)])
    if train:
        from backend.services.analysis import run_training

        print("      Training from measured benchmark history")
        outcome = run_training({"rerank": True})
        print(f"      Training status: {outcome['status']} ({outcome['samples']} samples)")
    print("[7/8] Running before/after benchmark on the test database")
    record = run_benchmark_detailed(
        candidate_id=str(recommendation["candidate_id"]),
        table=str(recommendation["table"]),
        columns=str(recommendation["columns"]),
        iterations=iterations,
        output_path=benchmark_path,
        supporting_query_ids=[q for q in str(recommendation.get("supporting_query_ids", "")).split(";") if q] or None,
    )
    summary = record["summary"]
    print(
        f"      Weighted mean {summary['before_ms']} ms -> {summary['after_ms']} ms; "
        f"workload benefit {summary['workload_benefit']:.4f}; cleanup {record['cleanup']['status'] if record.get('cleanup') else 'n/a'}"
    )
    print("[8/8] API and dashboard artifacts ready")
    return recommendation, record["queries"]


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
    parser.add_argument("--seed", type=int, default=None, help="Seed for a reproducible workload log.")
    parser.add_argument("--collect-labels", type=int, default=0, help="Benchmark up to N candidates to collect ML labels.")
    parser.add_argument("--train", action="store_true", help="Train the ML model from measured benchmark history.")
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
            seed=args.seed,
            collect_labels=args.collect_labels,
            train=args.train,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Local pipeline failed: {exc}", file=sys.stderr)
        return 1

    print(f"Recommendation: {recommendation['candidate_id']}")
    print(f"Benchmark groups: {len(results)}")
    print(f"Dashboard: http://127.0.0.1:8000/dashboard/")
    if args.serve:
        import uvicorn

        uvicorn.run("backend.app:app", host="127.0.0.1", port=8000, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())