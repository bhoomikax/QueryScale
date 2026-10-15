#!/usr/bin/env python3
"""Generate and execute the baseline QueryScale banking workload against MySQL.

Write statements (UPDATE/INSERT) run inside a transaction that is always
rolled back, so executing the workload never changes stored data.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from dotenv import load_dotenv

try:
    import mysql.connector
    from mysql.connector import Error as MySQLError
except ImportError as exc:  # pragma: no cover - dependency guidance
    raise SystemExit(
        "Missing database dependencies. Install them with: pip install -r requirements.txt"
    ) from exc

ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")

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
    {
        "query_id": "Q008",
        "template": "SELECT account_id, balance FROM Accounts WHERE branch_id = %s AND account_type = %s;",
        "frequency": "MEDIUM",
        "tables": ["Accounts"],
        "where_columns": ["branch_id", "account_type"],
        "join_columns": [],
        "order_columns": [],
        "description": "List accounts of one product type at a branch.",
    },
    {
        "query_id": "Q009",
        "template": "SELECT loan_id, customer_id, principal FROM Loans WHERE status = %s ORDER BY principal DESC LIMIT 50;",
        "frequency": "MEDIUM",
        "tables": ["Loans"],
        "where_columns": ["status"],
        "join_columns": [],
        "order_columns": ["principal"],
        "description": "Largest loans in a given status for risk review.",
    },
    {
        "query_id": "Q010",
        "template": "SELECT customer_id, name FROM Customers WHERE branch_id = %s AND kyc_status = %s;",
        "frequency": "LOW",
        "tables": ["Customers"],
        "where_columns": ["branch_id", "kyc_status"],
        "join_columns": [],
        "order_columns": [],
        "description": "Customers at a branch with a given KYC status.",
    },
    {
        "query_id": "Q011",
        "template": "SELECT txn_id, account_id, amount FROM Transactions WHERE txn_type = %s AND amount > %s;",
        "frequency": "MEDIUM",
        "tables": ["Transactions"],
        "where_columns": ["txn_type", "amount"],
        "join_columns": [],
        "order_columns": [],
        "description": "High-value transactions of one type for monitoring.",
    },
    {
        "query_id": "Q012",
        "template": "SELECT account_id, customer_id FROM Accounts WHERE balance BETWEEN %s AND %s;",
        "frequency": "LOW",
        "tables": ["Accounts"],
        "where_columns": ["balance"],
        "join_columns": [],
        "order_columns": [],
        "description": "Accounts within a balance band.",
    },
    {
        "query_id": "Q013",
        "template": "UPDATE Accounts SET balance = balance + %s WHERE account_id = %s;",
        "frequency": "LOW",
        "tables": ["Accounts"],
        "where_columns": ["account_id"],
        "join_columns": [],
        "order_columns": [],
        "description": "Post a balance adjustment (always rolled back).",
    },
    {
        "query_id": "Q014",
        "template": "INSERT INTO Transactions (account_id, amount, txn_type, txn_date) VALUES (%s, %s, %s, %s);",
        "frequency": "LOW",
        "tables": ["Transactions"],
        "where_columns": [],
        "join_columns": [],
        "order_columns": [],
        "description": "Record a transaction (always rolled back).",
    },
]

ACCOUNT_TYPES = ["Savings", "Current", "Salary", "Fixed Deposit", "Business"]
TRANSACTION_TYPES = ["CREDIT", "DEBIT", "TRANSFER", "ATM", "UPI"]
LOAN_STATUS = ["ACTIVE", "PAID", "PENDING", "DEFAULTED"]
KYC_STATUS = ["VERIFIED", "PENDING", "REJECTED", "EXPIRED"]
WRITE_PREFIXES = ("INSERT", "UPDATE", "DELETE", "REPLACE")
LOG_FIELDS = [
    "query_id",
    "query_template",
    "timestamp",
    "execution_time_ms",
    "frequency",
    "tables",
    "where_columns",
    "join_columns",
    "order_columns",
    "rows_returned",
    "rows_examined",
]


def is_write_query(template: str) -> bool:
    return template.lstrip().upper().startswith(WRITE_PREFIXES)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Execute the QueryScale banking workload.")
    parser.add_argument("--iterations", type=int, default=100, help="Number of queries to execute in the workload batch.")
    parser.add_argument("--dry-run", action="store_true", help="Display the planned workload without executing database queries.")
    parser.add_argument("--list-queries", action="store_true", help="List the available query templates and exit.")
    parser.add_argument("--log-path", default="data/query_logs.csv", help="Path for the query performance log CSV.")
    parser.add_argument("--seed", type=int, default=None, help="Seed for a reproducible workload plan.")
    return parser.parse_args()


def db_config() -> dict:
    return {
        "host": os.getenv("DB_HOST", "127.0.0.1"),
        "port": int(os.getenv("DB_PORT", "3306")),
        "user": os.getenv("DB_USER", "root"),
        "password": os.getenv("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", "queryscale"),
        "charset": os.getenv("DB_CHARSET", "utf8mb4"),
        "autocommit": True,
        "raise_on_warnings": True,
    }


def make_connection() -> mysql.connector.MySQLConnection:
    config = db_config()
    try:
        return mysql.connector.connect(**config)
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


def _date_window(rng: Any, max_offset: int, min_days: int, max_days: int) -> Tuple[str, str]:
    start_date = datetime(2024, 1, 1)
    d1 = start_date + timedelta(days=rng.randint(0, max_offset))
    d2 = d1 + timedelta(days=rng.randint(min_days, max_days))
    return (d1.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d"))


def build_query_arguments(bounds: Dict[str, int], query: Dict[str, Any], rng: Any = random) -> Tuple[Any, ...]:
    """Build parameters for one query. Pass a seeded ``random.Random`` for reproducible plans."""

    span = (datetime(2025, 12, 31) - datetime(2024, 1, 1)).days
    query_id = query["query_id"]
    if query_id in {"Q001", "Q003"}:
        return (rng.randint(1, max(1, bounds["Accounts"])),)
    if query_id == "Q002":
        return _date_window(rng, span - 30, 1, 30)
    if query_id in {"Q004", "Q005"}:
        return (rng.randint(1, max(1, bounds["Customers"])),)
    if query_id == "Q006":
        return (rng.randint(1, max(1, bounds["Branches"])),)
    if query_id == "Q007":
        return _date_window(rng, span - 60, 10, 90)
    if query_id == "Q008":
        return (rng.randint(1, max(1, bounds["Branches"])), rng.choice(ACCOUNT_TYPES))
    if query_id == "Q009":
        return (rng.choice(LOAN_STATUS),)
    if query_id == "Q010":
        return (rng.randint(1, max(1, bounds["Branches"])), rng.choice(KYC_STATUS))
    if query_id == "Q011":
        return (rng.choice(TRANSACTION_TYPES), round(rng.uniform(15000.0, 24900.0), 2))
    if query_id == "Q012":
        low = round(rng.uniform(1000.0, 240000.0), 2)
        return (low, round(low + rng.uniform(500.0, 5000.0), 2))
    if query_id == "Q013":
        return (round(rng.uniform(-500.0, 500.0), 2), rng.randint(1, max(1, bounds["Accounts"])))
    if query_id == "Q014":
        txn_date = datetime(2024, 1, 1) + timedelta(days=rng.randint(0, span))
        return (
            rng.randint(1, max(1, bounds["Accounts"])),
            round(rng.uniform(25.0, 25000.0), 2),
            rng.choice(TRANSACTION_TYPES),
            txn_date.strftime("%Y-%m-%d"),
        )
    raise ValueError(f"Unsupported query id: {query_id}")


def build_workload_plan(iterations: int, rng: Any = random) -> List[Dict[str, Any]]:
    queue: List[Dict[str, Any]] = []
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for query in QUERY_LIBRARY:
        counts[query["frequency"] if query["frequency"] in counts else "LOW"] += 1

    weights = {"HIGH": 5, "MEDIUM": 3, "LOW": 1}
    total_weight = sum(weights[f] * counts[f] for f in weights)
    for _ in range(iterations):
        roll = rng.randint(1, total_weight)
        bucket = "HIGH"
        running = 0
        for level, weight in weights.items():
            running += weight * counts[level]
            if roll <= running:
                bucket = level
                break

        candidates = [q for q in QUERY_LIBRARY if q["frequency"] == bucket]
        query = rng.choice(candidates)
        queue.append({"query": query, "query_id": query["query_id"]})
    return queue


def write_log_rows(log_rows: Sequence[Dict[str, Any]], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(LOG_FIELDS)
    file_exists = path.exists() and path.stat().st_size > 0
    if file_exists:
        # Keep an older log's header so appended rows stay aligned with it.
        with path.open("r", newline="", encoding="utf-8") as handle:
            existing = next(csv.reader(handle), None)
        if existing:
            fieldnames = existing
    with path.open("a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames, extrasaction="ignore")
        if not file_exists:
            writer.writeheader()
        writer.writerows(log_rows)


def fetch_rows_examined(cursor) -> Tuple[Any, Any]:
    """Rows examined/sent for this session's last completed statement (performance_schema).

    Returns ``(None, None)`` when performance_schema, the statement history
    consumer, or the required privilege is unavailable.
    """

    try:
        cursor.execute(
            "SELECT ROWS_EXAMINED, ROWS_SENT FROM performance_schema.events_statements_history "
            "WHERE THREAD_ID = PS_CURRENT_THREAD_ID() ORDER BY EVENT_ID DESC LIMIT 1"
        )
        row = cursor.fetchone()
    except MySQLError:
        return None, None
    if not row:
        return None, None
    return (
        int(row[0]) if row[0] is not None else None,
        int(row[1]) if row[1] is not None else None,
    )


def execute_query(connection, cursor, template: str, params: Sequence[Any], capture_rows_examined: bool = False) -> Dict[str, Any]:
    """Execute and time one workload statement.

    Writes run inside a transaction that is always rolled back, so stored data
    never changes. Their timing therefore excludes commit cost.
    """

    write = is_write_query(template)
    if write:
        connection.start_transaction()
    try:
        start = time.perf_counter()
        cursor.execute(template, tuple(params))
        if write:
            rows_returned = 0
            affected = cursor.rowcount
        else:
            rows_returned = len(cursor.fetchall())
            affected = None
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        examined = fetch_rows_examined(cursor)[0] if capture_rows_examined else None
    finally:
        if write:
            connection.rollback()
    return {"elapsed_ms": elapsed_ms, "rows_returned": rows_returned, "rows_affected": affected, "rows_examined": examined}


def execute_batch(connection, iterations: int, log_path: str, seed: int | None = None) -> List[Dict[str, Any]]:
    rng = random.Random(seed) if seed is not None else random
    cursor = connection.cursor()
    bounds = fetch_id_bounds(cursor)
    workload = build_workload_plan(iterations, rng)
    results = []
    log_rows = []
    capture = True

    for item in workload:
        query = item["query"]
        params = build_query_arguments(bounds, query, rng)
        outcome = execute_query(connection, cursor, query["template"], params, capture_rows_examined=capture)
        if capture and outcome["rows_examined"] is None:
            capture = False  # performance_schema unavailable; stop asking for this batch
        write = is_write_query(query["template"])

        results.append({
            "query_id": query["query_id"],
            "frequency": query["frequency"],
            "rows_returned": outcome["rows_returned"],
        })
        log_rows.append({
            "query_id": query["query_id"],
            "query_template": query["template"],
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "execution_time_ms": round(outcome["elapsed_ms"], 3),
            "frequency": query["frequency"],
            "tables": ";".join(query["tables"]),
            "where_columns": ";".join(query["where_columns"]),
            "join_columns": ";".join(query["join_columns"]),
            "order_columns": ";".join(query["order_columns"]),
            "rows_returned": "" if write else outcome["rows_returned"],
            "rows_examined": "" if outcome["rows_examined"] is None else outcome["rows_examined"],
        })

    cursor.close()
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
        plan = build_workload_plan(args.iterations, random.Random(args.seed) if args.seed is not None else random)
        print_workload_summary(plan)
        return

    try:
        connection = make_connection()
        summary = execute_batch(connection, args.iterations, args.log_path, args.seed)
        connection.close()

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
