#!/usr/bin/env python3
"""Generate and execute the baseline QueryScale banking workload against MySQL."""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple

try:
    import mysql.connector
    from mysql.connector import Error as MySQLError
except ImportError as exc:  # pragma: no cover - dependency guidance
    raise SystemExit(
        "Missing database dependencies. Install them with: pip install -r requirements.txt"
    ) from exc

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.config import settings
from backend.database import close_connection, open_connection

QUERY_LIBRARY: List[Dict[str, Any]] = [
    {
        "query_id": "Q001",
        "template": "SELECT * FROM Transactions WHERE account_id = %s;",
        "frequency": "HIGH",
        "tables": ["Transactions"],
        "where_columns": ["account_id"],
        "join_columns": [],
        "order_columns": [],
        "description": "Fetch all transactions for a single account.",
    },
    {
        "query_id": "Q002",
        "template": "SELECT * FROM Transactions WHERE txn_date BETWEEN %s AND %s;",
        "frequency": "HIGH",
        "tables": ["Transactions"],
        "where_columns": ["txn_date"],
        "join_columns": [],
        "order_columns": [],
        "description": "Return a date-window transaction range.",
    },
    {
        "query_id": "Q003",
        "template": "SELECT * FROM Transactions WHERE account_id = %s ORDER BY txn_date DESC;",
        "frequency": "MEDIUM",
        "tables": ["Transactions"],
        "where_columns": ["account_id"],
        "join_columns": [],
        "order_columns": ["txn_date"],
        "description": "Fetch recent activity for a specific account.",
    },
    {
        "query_id": "Q004",
        "template": (
            "SELECT c.name, a.account_id "
            "FROM Customers c JOIN Accounts a ON c.customer_id = a.customer_id "
            "WHERE c.customer_id = %s;"
        ),
        "frequency": "MEDIUM",
        "tables": ["Customers", "Accounts"],
        "where_columns": ["customer_id"],
        "join_columns": ["customer_id"],
        "order_columns": [],
        "description": "Look up a customer account summary.",
    },
    {
        "query_id": "Q005",
        "template": "SELECT * FROM Loans WHERE customer_id = %s;",
        "frequency": "MEDIUM",
        "tables": ["Loans"],
        "where_columns": ["customer_id"],
        "join_columns": [],
        "order_columns": [],
        "description": "Load all loan records for a customer.",
    },
    {
        "query_id": "Q006",
        "template": "SELECT * FROM Customers WHERE branch_id = %s;",
        "frequency": "LOW",
        "tables": ["Customers"],
        "where_columns": ["branch_id"],
        "join_columns": [],
        "order_columns": [],
        "description": "Review customers for a branch.",
    },
    {
        "query_id": "Q007",
        "template": (
            "SELECT DATE_FORMAT(txn_date, '%Y-%m') AS month, COUNT(*) AS txn_count, "
            "SUM(amount) AS txn_total FROM Transactions WHERE txn_date BETWEEN %s AND %s "
            "GROUP BY DATE_FORMAT(txn_date, '%Y-%m') ORDER BY month;"
        ),
        "frequency": "LOW",
        "tables": ["Transactions"],
        "where_columns": ["txn_date"],
        "join_columns": [],
        "order_columns": ["month"],
        "description": "Monthly reporting for transaction totals.",
    },
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute the QueryScale banking workload.")
    parser.add_argument("--iterations", type=int, default=100, help="Number of queries to execute in the workload batch.")
    parser.add_argument("--dry-run", action="store_true", help="Display the planned workload without executing database queries.")
    parser.add_argument("--list-queries", action="store_true", help="List the available query templates and exit.")
    parser.add_argument("--log-path", default="data/query_logs.csv", help="Path for the query performance log CSV.")
    return parser.parse_args()


def db_config() -> dict:
    return settings.mysql_config()


def make_connection() -> mysql.connector.MySQLConnection:
    config = db_config()
    try:
        return open_connection(config)
    except MySQLError as exc:
        raise RuntimeError(
            f"Unable to connect to MySQL at {config['host']}:{config['port']} using database '{config['database']}'. "
            "Check .env and ensure MySQL is running."
        ) from exc


def fetch_id_bounds(cursor) -> Dict[str, int]:
    queries = {
        "Accounts": "SELECT COALESCE(MAX(account_id), 0) FROM Accounts;",
        "Customers": "SELECT COALESCE(MAX(customer_id), 0) FROM Customers;",
        "Branches": "SELECT COALESCE(MAX(branch_id), 0) FROM Branches;",
        "Transactions": "SELECT COALESCE(MAX(txn_id), 0) FROM Transactions;",
        "Loans": "SELECT COALESCE(MAX(loan_id), 0) FROM Loans;",
    }
    bounds: Dict[str, int] = {}
    for table_name, sql in queries.items():
        cursor.execute(sql)
        row = cursor.fetchone()
        bounds[table_name] = int(row[0]) if row and row[0] is not None else 0
    return bounds


def build_query_arguments(bounds: Dict[str, int], query: Dict[str, Any]) -> Tuple[Any, ...]:
    if query["query_id"] == "Q001":
        return (random.randint(1, max(1, bounds["Accounts"])),)
    if query["query_id"] == "Q002":
        start_date = datetime(2024, 1, 1)
        end_date = datetime(2025, 12, 31)
        delta = (end_date - start_date).days
        d1 = start_date + timedelta(days=random.randint(0, delta - 30))
        d2 = d1 + timedelta(days=random.randint(1, 30))
        return (d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d"))
    if query["query_id"] == "Q003":
        return (random.randint(1, max(1, bounds["Accounts"])),)
    if query["query_id"] == "Q004":
        return (random.randint(1, max(1, bounds["Customers"])),)
    if query["query_id"] == "Q005":
        return (random.randint(1, max(1, bounds["Customers"])),)
    if query["query_id"] == "Q006":
        return (random.randint(1, max(1, bounds["Branches"])),)
    if query["query_id"] == "Q007":
        start_date = datetime(2024, 1, 1)
        end_date = datetime(2025, 12, 31)
        delta = (end_date - start_date).days
        d1 = start_date + timedelta(days=random.randint(0, delta - 60))
        d2 = d1 + timedelta(days=random.randint(10, 90))
        return (d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d"))
    raise ValueError(f"Unsupported query id: {query['query_id']}")


def build_workload_plan(iterations: int) -> List[Dict[str, Any]]:
    queue: List[Dict[str, Any]] = []
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for query in QUERY_LIBRARY:
        if query["frequency"] == "HIGH":
            counts["HIGH"] += 1
        elif query["frequency"] == "MEDIUM":
            counts["MEDIUM"] += 1
        else:
            counts["LOW"] += 1

    weights = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}
    total_weight = sum(weights[f] * counts[f] for f in weights)
    for _ in range(iterations):
        roll = random.randint(1, total_weight)
        bucket = "HIGH"
        running = 0
        for level, weight in weights.items():
            running += weight * counts[level]
            if roll <= running:
                bucket = level
                break

        candidates = [q for q in QUERY_LIBRARY if q["frequency"] == bucket]
        query = random.choice(candidates)
        queue.append({"query": query, "query_id": query["query_id"]})
    return queue


def write_log_rows(log_rows: Sequence[Dict[str, Any]], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "query_id",
        "query_template",
        "timestamp",
        "execution_time_ms",
        "frequency",
        "tables",
        "where_columns",
        "join_columns",
        "order_columns",
    ]

    file_exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        if not file_exists:
            writer.writeheader()
        writer.writerows(log_rows)


def execute_batch(connection, iterations: int, log_path: str) -> List[Dict[str, Any]]:
    cursor = connection.cursor()
    bounds = fetch_id_bounds(cursor)
    workload = build_workload_plan(iterations)
    results = []
    log_rows = []

    for item in workload:
        query = item["query"]
        params = build_query_arguments(bounds, query)
        start_ts = time.perf_counter()
        cursor.execute(query["template"], params)
        rows = cursor.fetchall()
        execution_ms = (time.perf_counter() - start_ts) * 1000.0

        results.append({
            "query_id": query["query_id"],
            "frequency": query["frequency"],
            "rows_returned": len(rows),
        })
        log_rows.append({
            "query_id": query["query_id"],
            "query_template": query["template"],
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "execution_time_ms": round(execution_ms, 3),
            "frequency": query["frequency"],
            "tables": ";".join(query["tables"]),
            "where_columns": ";".join(query["where_columns"]),
            "join_columns": ";".join(query["join_columns"]),
            "order_columns": ";".join(query["order_columns"]),
        })

    write_log_rows(log_rows, log_path)
    return results


def print_workload_summary(plan: Sequence[Dict[str, Any]]) -> None:
    print("QueryScale workload plan")
    print("=" * 26)
    counts: Dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for item in plan:
        counts[item["query"]["frequency"]] += 1
    for level in ["HIGH", "MEDIUM", "LOW"]:
        print(f"{level:<6}: {counts[level]}")
    print("\nQuery definitions:")
    for entry in QUERY_LIBRARY:
        print(f"- {entry['query_id']} [{entry['frequency']}]: {entry['description']}")


def main() -> None:
    args = parse_args()

    if args.list_queries:
        for entry in QUERY_LIBRARY:
            print(f"{entry['query_id']} | {entry['frequency']} | {entry['description']}")
        return

    if args.dry_run:
        plan = build_workload_plan(args.iterations)
        print_workload_summary(plan)
        return

    try:
        connection = make_connection()
        try:
            summary = execute_batch(connection, args.iterations, args.log_path)
        finally:
            close_connection(connection)

        print(f"Executed workload batch: {args.iterations} queries")
        print(f"Query log written to: {args.log_path}")
        freq_counts: Dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        for row in summary:
            freq_counts[row["frequency"]] += 1
        for level in ["HIGH", "MEDIUM", "LOW"]:
            print(f"{level}: {freq_counts[level]}")
    except Exception as exc:  # pragma: no cover - runtime database failure path
        raise RuntimeError(f"Query workload execution failed: {exc}") from exc


if __name__ == "__main__":
    main()
