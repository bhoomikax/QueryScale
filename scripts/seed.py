#!/usr/bin/env python3
"""Generate and load synthetic banking data into the QueryScale MySQL database."""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, List, Sequence, Tuple

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

DEFAULT_CONFIG = {
    "branches": 50,
    "customers": 10_000,
    "accounts": 15_000,
    "transactions": 100_000,
    "loans": 5_000,
}

BRANCH_NAMES = [
    "Central", "North", "South", "East", "West", "River", "Hill", "Market",
    "Civic", "Heritage", "Metro", "Urban", "Prime", "Oak", "Garden", "Summit",
    "Lakeside", "Sunrise", "Fort", "Harbor"
]
CITY_NAMES = [
    "Mumbai", "Delhi", "Bengaluru", "Hyderabad", "Chennai", "Kolkata",
    "Pune", "Jaipur", "Lucknow", "Ahmedabad", "Patna", "Nagpur", "Surat",
    "Indore", "Kanpur", "Coimbatore", "Visakhapatnam", "Bhopal", "Varanasi"
]
ACCOUNT_TYPES = ["Savings", "Current", "Salary", "Fixed Deposit", "Business"]
TRANSACTION_TYPES = ["CREDIT", "DEBIT", "TRANSFER", "ATM", "UPI"]
LOAN_TYPES = ["Home", "Education", "Vehicle", "Personal", "Business"]
LOAN_STATUS = ["ACTIVE", "PAID", "PENDING", "DEFAULTED"]
KYC_STATUS = ["VERIFIED", "PENDING", "REJECTED", "EXPIRED"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic banking dataset for QueryScale.")
    parser.add_argument("--branches", type=int, default=DEFAULT_CONFIG["branches"], help="Number of branch rows to generate.")
    parser.add_argument("--customers", type=int, default=DEFAULT_CONFIG["customers"], help="Number of customer rows to generate.")
    parser.add_argument("--accounts", type=int, default=DEFAULT_CONFIG["accounts"], help="Number of account rows to generate.")
    parser.add_argument("--transactions", type=int, default=DEFAULT_CONFIG["transactions"], help="Number of transaction rows to generate.")
    parser.add_argument("--loans", type=int, default=DEFAULT_CONFIG["loans"], help="Number of loan rows to generate.")
    parser.add_argument("--no-reset", action="store_true", help="Do not clear existing data before insertion.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned dataset without writing to MySQL.")
    return parser.parse_args()


def db_config() -> dict:
    return settings.mysql_config()


def make_connection() -> mysql.connector.MySQLConnection:
    config = db_config()
    try:
        connection = open_connection(config)
        return connection
    except MySQLError as exc:
        raise RuntimeError(
            f"Unable to connect to MySQL at {config['host']}:{config['port']} using database '{config['database']}'. "
            "Check your .env configuration and ensure MySQL is running."
        ) from exc


def reset_tables(cursor) -> None:
    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
    for table in ["Transactions", "Accounts", "Loans", "Customers", "Branches"]:
        cursor.execute(f"TRUNCATE TABLE `{table}`")
    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")


def generate_branches(branch_count: int) -> List[Tuple[str, str, str]]:
    rows: List[Tuple[str, str, str]] = []
    for idx in range(branch_count):
        name = f"{BRANCH_NAMES[idx % len(BRANCH_NAMES)]} Branch"
        city = CITY_NAMES[idx % len(CITY_NAMES)]
        ifsc = f"QSC{idx + 1:05d}"
        rows.append((name, ifsc, city))
    return rows


def generate_customers(customer_count: int, branch_count: int) -> List[Tuple[str, str, str, int]]:
    rows: List[Tuple[str, str, str, int]] = []
    start_date = datetime(1970, 1, 1)
    end_date = datetime(2008, 12, 31)

    for idx in range(customer_count):
        dob = start_date + timedelta(days=random.randint(0, int((end_date - start_date).days)))
        name = f"Customer {idx + 1:06d}"
        kyc = KYC_STATUS[idx % len(KYC_STATUS)]
        branch_id = random.randint(1, branch_count)
        rows.append((name, dob.strftime("%Y-%m-%d"), kyc, branch_id))
    return rows


def generate_accounts(account_count: int, customer_count: int, branch_count: int) -> List[Tuple[int, str, float, int]]:
    rows: List[Tuple[int, str, float, int]] = []
    for idx in range(account_count):
        customer_id = random.randint(1, customer_count)
        account_type = ACCOUNT_TYPES[idx % len(ACCOUNT_TYPES)]
        balance = round(random.uniform(1000.0, 250000.0), 2)
        branch_id = random.randint(1, branch_count)
        rows.append((customer_id, account_type, float(balance), branch_id))
    return rows


def generate_transactions(transaction_count: int, account_count: int) -> List[Tuple[int, float, str, str]]:
    rows: List[Tuple[int, float, str, str]] = []
    start_date = datetime(2024, 1, 1)
    end_date = datetime(2025, 12, 31)

    for idx in range(transaction_count):
        account_id = random.randint(1, account_count)
        amount = round(random.uniform(25.0, 25000.0), 2)
        txn_type = TRANSACTION_TYPES[idx % len(TRANSACTION_TYPES)]
        delta_days = random.randint(0, (end_date - start_date).days)
        txn_date = (start_date + timedelta(days=delta_days)).strftime("%Y-%m-%d")
        rows.append((account_id, float(amount), txn_type, txn_date))
    return rows


def generate_loans(loan_count: int, customer_count: int) -> List[Tuple[int, str, float, str]]:
    rows: List[Tuple[int, str, float, str]] = []
    for idx in range(loan_count):
        customer_id = random.randint(1, customer_count)
        loan_type = LOAN_TYPES[idx % len(LOAN_TYPES)]
        principal = round(random.uniform(5000.0, 1500000.0), 2)
        status = LOAN_STATUS[idx % len(LOAN_STATUS)]
        rows.append((customer_id, loan_type, float(principal), status))
    return rows


def generate_dataset(config: dict) -> dict:
    return {
        "branches": generate_branches(config["branches"]),
        "customers": generate_customers(config["customers"], config["branches"]),
        "accounts": generate_accounts(config["accounts"], config["customers"], config["branches"]),
        "transactions": generate_transactions(config["transactions"], config["accounts"]),
        "loans": generate_loans(config["loans"], config["customers"]),
    }


def insert_dataset(cursor, dataset: dict) -> None:
    cursor.executemany(
        "INSERT INTO `Branches` (`branch_name`, `ifsc_code`, `city`) VALUES (%s, %s, %s)",
        dataset["branches"],
    )
    cursor.executemany(
        "INSERT INTO `Customers` (`name`, `dob`, `kyc_status`, `branch_id`) VALUES (%s, %s, %s, %s)",
        dataset["customers"],
    )
    cursor.executemany(
        "INSERT INTO `Accounts` (`customer_id`, `account_type`, `balance`, `branch_id`) VALUES (%s, %s, %s, %s)",
        dataset["accounts"],
    )
    cursor.executemany(
        "INSERT INTO `Transactions` (`account_id`, `amount`, `txn_type`, `txn_date`) VALUES (%s, %s, %s, %s)",
        dataset["transactions"],
    )
    cursor.executemany(
        "INSERT INTO `Loans` (`customer_id`, `loan_type`, `principal`, `status`) VALUES (%s, %s, %s, %s)",
        dataset["loans"],
    )


def main() -> None:
    args = parse_args()

    config = {
        "branches": args.branches,
        "customers": args.customers,
        "accounts": args.accounts,
        "transactions": args.transactions,
        "loans": args.loans,
    }

    if args.dry_run:
        print("QueryScale synthetic dataset plan")
        print("=" * 32)
        for key, value in config.items():
            print(f"{key.title():>12}: {value}")
        return

    try:
        connection = make_connection()
        cursor = connection.cursor()

        if not args.no_reset:
            reset_tables(cursor)

        dataset = generate_dataset(config)
        insert_dataset(cursor, dataset)
        connection.commit()

        print("Synthetic banking data generated successfully.")
        print(f"Branches: {config['branches']}")
        print(f"Customers: {config['customers']}")
        print(f"Accounts: {config['accounts']}")
        print(f"Transactions: {config['transactions']}")
        print(f"Loans: {config['loans']}")

    except Exception as exc:  # pragma: no cover - runtime database failure path
        raise RuntimeError(f"Synthetic data generation failed: {exc}") from exc
    finally:
        if "connection" in locals():
            close_connection(connection)


if __name__ == "__main__":
    main()
