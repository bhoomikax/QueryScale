"""Before/after benchmarks of one index candidate on the isolated test database.

Workflow for one run (protocol version 2):

1. Validate the candidate and take the benchmark lock (one run at a time).
2. Reset the test database to the primary schema's index set, dropping indexes
   left by earlier runs. The primary database is only read, never modified.
3. Record the server version and a dataset signature (exact row counts).
4. Build one seeded replay plan of the workload.
5. Baseline: EXPLAIN each template, sample rows examined, probe write latency,
   warm up, then time the replay ``repeats`` times.
6. Create the candidate index on the test database and measure its size.
7. Indexed: repeat step 5 with the identical plan.
8. Drop the candidate index (cleanup) and record whether cleanup succeeded.
9. Persist per-query rows to ``benchmark_results.csv`` and the full run record,
   including the candidate feature snapshot and label, to the history file.

Limitations: runs on one MySQL instance without concurrent load. Warm-up runs
reduce but do not eliminate buffer-pool and OS cache effects, so small
differences (a few percent) can be noise.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import statistics
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from mysql.connector.errors import Error as MySQLError

from backend.config import settings
from backend.services.index_manager import (
    BenchmarkError,
    apply_index,
    build_index_name,
    connect_to_test_database,
    drop_index,
    measure_index_size_bytes,
    raise_classified,
    reset_test_indexes,
    validate_candidate,
)
from backend.services.stats import (
    MIN_P95_SAMPLES,
    latency_reduction_pct,
    median_or_none,
    p95_or_none,
    percentile,
    speedup,
)
from backend.services.test_database import EXPECTED_TABLES, quote_identifier


logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_PATH = Path("data/benchmark_results.csv")
PROTOCOL_VERSION = 2
ROWS_EXAMINED_SAMPLES = 3
LOCK_MAX_AGE_SECONDS = 3 * 60 * 60

RESULT_FIELDS = [
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
    "before_median_ms",
    "after_median_ms",
    "median_speedup",
    "median_latency_reduction_pct",
    "before_rows_examined",
    "after_rows_examined",
    "before_key",
    "after_key",
    "uses_candidate_index",
    "supporting",
    "affected",
    "run_id",
]

__all__ = ["percentile", "compare_timings", "run_benchmark", "run_benchmark_detailed", "BenchmarkError"]


@dataclass(frozen=True)
class ReplayQuery:
    """One query and its fixed parameters in the benchmark replay plan."""

    query_id: str
    template: str
    params: tuple[Any, ...]


# ---------------------------------------------------------------------------
# Pure calculations
# ---------------------------------------------------------------------------

def compare_timings(before: Sequence[float], after: Sequence[float]) -> dict[str, Any]:
    """Calculate before/after timing metrics from actual execution samples.

    P95 values are ``None`` when either sample has fewer than 20 observations.
    """

    if not before or not after:
        raise ValueError("Before and after timing samples are required")

    before_average = statistics.fmean(before)
    after_average = statistics.fmean(after)
    before_median = statistics.median(before)
    after_median = statistics.median(after)
    return {
        "before_avg_ms": round(before_average, 3),
        "before_min_ms": round(min(before), 3),
        "before_max_ms": round(max(before), 3),
        "before_p95_ms": p95_or_none(before),
        "after_avg_ms": round(after_average, 3),
        "after_min_ms": round(min(after), 3),
        "after_max_ms": round(max(after), 3),
        "after_p95_ms": p95_or_none(after),
        "execution_count": len(before),
        "speedup": speedup(before_average, after_average) or 0.0,
        "improvement_percentage": latency_reduction_pct(before_average, after_average) or 0.0,
        "before_median_ms": round(before_median, 3),
        "after_median_ms": round(after_median, 3),
        "median_speedup": speedup(before_median, after_median),
        "median_latency_reduction_pct": latency_reduction_pct(before_median, after_median),
    }


def summarize_workload(
    rows: Sequence[Dict[str, Any]],
    executions_per_query: Dict[str, int],
) -> Dict[str, Any]:
    """Aggregate per-query results into workload metrics and the ML label.

    ``workload_benefit`` follows ``backend.services.ml.dataset.TARGET_DEFINITION``.
    """

    baseline_median_total = 0.0
    indexed_median_total = 0.0
    affected_gain = 0.0
    before_mean_total = 0.0
    after_mean_total = 0.0
    executions = 0
    for row in rows:
        count = executions_per_query.get(row["query_id"], 0)
        executions += count
        baseline_median_total += count * row["before_median_ms"]
        indexed_median_total += count * row["after_median_ms"]
        before_mean_total += count * row["before_avg_ms"]
        after_mean_total += count * row["after_avg_ms"]
        if row["affected"]:
            affected_gain += count * (row["before_median_ms"] - row["after_median_ms"])

    if executions <= 0 or baseline_median_total <= 0:
        raise ValueError("Benchmark produced no measurable executions")

    before_ms = before_mean_total / executions
    after_ms = after_mean_total / executions
    return {
        "execution_count": executions,
        "before_ms": round(before_ms, 3),
        "after_ms": round(after_ms, 3),
        "speedup": speedup(before_ms, after_ms),
        "improvement_percentage": latency_reduction_pct(before_ms, after_ms),
        "baseline_workload_median_ms": round(baseline_median_total, 3),
        "indexed_workload_median_ms": round(indexed_median_total, 3),
        "workload_median_speedup": speedup(baseline_median_total, indexed_median_total),
        "workload_median_latency_reduction_pct": latency_reduction_pct(baseline_median_total, indexed_median_total),
        "workload_benefit": round(affected_gain / baseline_median_total, 6),
        "affected_queries": [row["query_id"] for row in rows if row["affected"]],
    }


def workload_signature(templates: Dict[str, str], iterations: int, seed: int, repeats: int) -> str:
    payload = json.dumps(
        {"templates": templates, "iterations": iterations, "seed": seed, "repeats": repeats, "protocol": PROTOCOL_VERSION},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _workload_templates() -> Dict[str, str]:
    from scripts.workload import QUERY_LIBRARY

    return {q["query_id"]: q["template"] for q in QUERY_LIBRARY}


def label_protocol() -> Dict[str, Any]:
    """The effective benchmark protocol every entry point defaults to (API, dashboard, CLI, pipeline).

    Runs that share this protocol share one workload signature and can be combined into
    one training set. A run with other iterations, repeats, or seed is still recorded, but
    its signature differs and the trainer excludes it as ``different_workload``.
    """

    templates = _workload_templates()
    iterations, repeats, seed = settings.benchmark_iterations, settings.benchmark_repeats, settings.benchmark_seed
    return {
        "protocol_version": PROTOCOL_VERSION,
        "iterations": iterations,
        "repeats": repeats,
        "warm_up_passes": 1,
        "seed": seed,
        "query_timeout_ms": settings.benchmark_query_timeout_ms,
        "write_probe_samples": settings.benchmark_write_probe_samples,
        "query_ids": sorted(templates),
        "workload_signature": workload_signature(templates, iterations, seed, repeats),
        "test_database": settings.db_test_name,
        "settings": {"BENCHMARK_ITERATIONS": iterations, "BENCHMARK_REPEATS": repeats, "BENCHMARK_SEED": seed},
    }


def validate_protocol_values(iterations: int, repeats: int) -> None:
    if not isinstance(iterations, int) or iterations < 1 or iterations > 10000:
        raise ValueError(f"Benchmark iterations must be an integer between 1 and 10000, got {iterations!r}")
    if not isinstance(repeats, int) or repeats < 1 or repeats > 10:
        raise ValueError(f"Benchmark repeats must be an integer between 1 and 10, got {repeats!r}")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Locking (duplicate-run protection within and across processes)
# ---------------------------------------------------------------------------

_PROCESS_LOCK = threading.Lock()


def _lock_path() -> Path:
    return settings.resolve_path("runtime/benchmark.lock")


@contextmanager
def benchmark_lock(candidate_id: str) -> Iterator[None]:
    if not _PROCESS_LOCK.acquire(blocking=False):
        raise BenchmarkError("benchmark_in_progress", "Another benchmark is already running in this process.")
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and time.time() - path.stat().st_mtime > LOCK_MAX_AGE_SECONDS:
            logger.warning("Removing stale benchmark lock %s", path)
            path.unlink(missing_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            holder = path.read_text(encoding="utf-8", errors="replace").strip()
            raise BenchmarkError("benchmark_in_progress", f"Another benchmark is running ({holder}).") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} candidate={candidate_id} started={_now()}")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
    finally:
        _PROCESS_LOCK.release()


# ---------------------------------------------------------------------------
# Database steps (test database only)
# ---------------------------------------------------------------------------

def _reference_indexes() -> Dict[str, set[str]]:
    """Index names that exist in the primary database (read-only query), else in schema.sql."""

    from backend.services.schema_metadata import _connect_primary_read_only, parse_schema_sql

    try:
        connection = _connect_primary_read_only()
        try:
            cursor = connection.cursor()
            cursor.execute(
                "SELECT TABLE_NAME, INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS WHERE TABLE_SCHEMA = %s",
                (settings.db_name,),
            )
            reference: Dict[str, set[str]] = {}
            for table, index in cursor.fetchall():
                reference.setdefault(str(table).lower(), set()).add(str(index))
            cursor.close()
        finally:
            connection.close()
        if reference:
            return reference
    except Exception as exc:
        logger.info("Primary index list unavailable (%s); using schema.sql as the reference.", exc)
    metadata = parse_schema_sql()
    return {name.lower(): {index.name for index in table.indexes} for name, table in metadata.tables.items()}


def _build_replay_plan(cursor: Any, iterations: int, seed: int) -> List[ReplayQuery]:
    from scripts.workload import build_query_arguments, build_workload_plan, fetch_id_bounds

    if iterations <= 0:
        raise ValueError("Benchmark iterations must be greater than zero")
    rng = random.Random(seed)
    bounds = fetch_id_bounds(cursor)
    return [
        ReplayQuery(item["query_id"], item["query"]["template"], build_query_arguments(bounds, item["query"], rng))
        for item in build_workload_plan(iterations, rng)
    ]


def _dataset_signature(cursor: Any) -> Dict[str, Any]:
    counts: Dict[str, int] = {}
    for table in EXPECTED_TABLES:
        cursor.execute(f"SELECT COUNT(*) FROM {quote_identifier(table)}")
        counts[table] = int(cursor.fetchone()[0])
    digest = hashlib.sha256(json.dumps(counts, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {"row_counts": counts, "hash": digest}


def _explain(cursor: Any, plan: Sequence[ReplayQuery]) -> Dict[str, Optional[List[Dict[str, Any]]]]:
    plans: Dict[str, Optional[List[Dict[str, Any]]]] = {}
    for query in plan:
        if query.query_id in plans:
            continue
        try:
            cursor.execute("EXPLAIN " + query.template.rstrip().rstrip(";"), query.params)
            columns = [d[0] for d in cursor.description]
            plans[query.query_id] = [
                {key: row[columns.index(key)] for key in ("table", "type", "possible_keys", "key", "rows", "Extra") if key in columns}
                for row in cursor.fetchall()
            ]
        except MySQLError as exc:
            logger.info("EXPLAIN unavailable for %s: %s", query.query_id, exc)
            plans[query.query_id] = None
    return plans


def _rows_examined(connection: Any, cursor: Any, plan: Sequence[ReplayQuery]) -> Dict[str, Optional[float]]:
    from scripts.workload import execute_query

    samples: Dict[str, List[int]] = {}
    unavailable: set[str] = set()
    for query in plan:
        if query.query_id in unavailable or len(samples.get(query.query_id, [])) >= ROWS_EXAMINED_SAMPLES:
            continue
        outcome = execute_query(connection, cursor, query.template, query.params, capture_rows_examined=True)
        if outcome["rows_examined"] is None:
            unavailable.add(query.query_id)
        else:
            samples.setdefault(query.query_id, []).append(outcome["rows_examined"])
    return {qid: round(statistics.fmean(values), 1) for qid, values in samples.items()}


def _execute_replay(connection: Any, cursor: Any, plan: Iterable[ReplayQuery]) -> Dict[str, List[float]]:
    from scripts.workload import execute_query

    timings: Dict[str, List[float]] = {}
    for query in plan:
        outcome = execute_query(connection, cursor, query.template, query.params)
        timings.setdefault(query.query_id, []).append(outcome["elapsed_ms"])
    return timings


def _write_probe(connection: Any, cursor: Any, table: str, columns: Sequence[str], samples: int, seed: int) -> Dict[str, Any]:
    """Time single-row INSERT, UPDATE (of the index columns), and DELETE inside a rolled-back transaction."""

    result: Dict[str, Any] = {"samples": 0, "insert_avg_ms": None, "update_avg_ms": None, "delete_avg_ms": None, "errors": []}
    if samples <= 0:
        return result
    cursor.execute(
        "SELECT COLUMN_NAME, EXTRA, COLUMN_KEY FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = %s "
        "AND LOWER(TABLE_NAME) = LOWER(%s) ORDER BY ORDINAL_POSITION",
        (settings.db_test_name, table),
    )
    column_rows = cursor.fetchall()
    primary = [name for name, _, key in column_rows if key == "PRI"]
    if len(primary) != 1:
        result["errors"].append("Write probe needs a single-column primary key")
        return result
    pk = primary[0]
    data_columns = [name for name, extra, _ in column_rows if "auto_increment" not in str(extra).lower()]
    quoted_table = quote_identifier(table)
    column_list = ", ".join(quote_identifier(c) for c in data_columns)
    cursor.execute(f"SELECT {quote_identifier(pk)} FROM {quoted_table} ORDER BY {quote_identifier(pk)}")
    ids = [row[0] for row in cursor.fetchall()]
    if len(ids) < 2:
        result["errors"].append("Not enough rows for a write probe")
        return result
    rng = random.Random(seed)
    targets = [rng.choice(ids) for _ in range(samples)]
    donors = [rng.choice(ids) for _ in range(samples)]
    update_columns = [c for c in columns if c.lower() != pk.lower()]

    timings: Dict[str, List[float]] = {"insert": [], "update": [], "delete": []}
    inserted: List[int] = []
    connection.start_transaction()
    try:
        for source_id in targets:
            start = time.perf_counter()
            try:
                cursor.execute(
                    f"INSERT INTO {quoted_table} ({column_list}) SELECT {column_list} FROM {quoted_table} "
                    f"WHERE {quote_identifier(pk)} = %s",
                    (source_id,),
                )
            except MySQLError as exc:
                result["errors"].append(f"INSERT probe unavailable: {exc.msg if hasattr(exc, 'msg') else exc}")
                break
            timings["insert"].append((time.perf_counter() - start) * 1000.0)
            inserted.append(cursor.lastrowid)
        if update_columns:
            select_list = ", ".join(quote_identifier(c) for c in update_columns)
            set_list = ", ".join(f"{quote_identifier(c)} = %s" for c in update_columns)
            for target_id, donor_id in zip(targets, donors):
                cursor.execute(f"SELECT {select_list} FROM {quoted_table} WHERE {quote_identifier(pk)} = %s", (donor_id,))
                values = cursor.fetchone()
                if values is None:
                    continue
                start = time.perf_counter()
                cursor.execute(
                    f"UPDATE {quoted_table} SET {set_list} WHERE {quote_identifier(pk)} = %s",
                    tuple(values) + (target_id,),
                )
                timings["update"].append((time.perf_counter() - start) * 1000.0)
        for row_id in inserted:
            start = time.perf_counter()
            cursor.execute(f"DELETE FROM {quoted_table} WHERE {quote_identifier(pk)} = %s", (row_id,))
            timings["delete"].append((time.perf_counter() - start) * 1000.0)
    finally:
        connection.rollback()

    for operation, values in timings.items():
        result[f"{operation}_avg_ms"] = round(statistics.fmean(values), 4) if values else None
        result[f"{operation}_median_ms"] = median_or_none(values)
    result["samples"] = max((len(v) for v in timings.values()), default=0)
    return result


def _write_overhead(before: Dict[str, Any], after: Dict[str, Any]) -> Dict[str, Any]:
    overhead: Dict[str, Any] = {"before": before, "after": after}
    for operation in ("insert", "update", "delete"):
        base = before.get(f"{operation}_median_ms")
        indexed = after.get(f"{operation}_median_ms")
        reduction = latency_reduction_pct(base, indexed)
        overhead[f"{operation}_overhead_pct"] = None if reduction is None else round(-reduction, 3)
    return overhead


def _measure_phase(connection: Any, cursor: Any, plan: Sequence[ReplayQuery], repeats: int, table: str, columns: Sequence[str], probe_samples: int, seed: int) -> Dict[str, Any]:
    explain = _explain(cursor, plan)
    rows_examined = _rows_examined(connection, cursor, plan)
    write_probe = _write_probe(connection, cursor, table, columns, probe_samples, seed)
    _execute_replay(connection, cursor, plan)  # warm-up, discarded
    timings: Dict[str, List[float]] = {}
    for _ in range(repeats):
        for query_id, values in _execute_replay(connection, cursor, plan).items():
            timings.setdefault(query_id, []).extend(values)
    return {"explain": explain, "rows_examined": rows_examined, "write_probe": write_probe, "timings": timings}


def _keys(plan_rows: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if plan_rows is None:
        return None
    return ";".join(str(row.get("key")) for row in plan_rows if row.get("key")) or "NONE"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _write_results(rows: Sequence[dict[str, object]], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RESULT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: ("" if row.get(key) is None else row.get(key)) for key in RESULT_FIELDS})


def append_history(record: Dict[str, Any], history_path: str | Path | None = None) -> Path:
    path = Path(history_path) if history_path else settings.resolve_path(settings.benchmark_history_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")
    return path


def load_candidate_snapshot(candidate_id: str) -> Dict[str, Any]:
    """Candidate features and rule score as they were before benchmarking (from the scored candidate file)."""

    for configured in (settings.candidate_path, settings.feature_path):
        path = settings.resolve_path(configured)
        if not path.exists():
            continue
        with path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("candidate_id") == candidate_id:
                    return dict(row)
    return {}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_benchmark_detailed(
    candidate_id: str,
    table: str,
    columns: str | Sequence[str],
    iterations: int | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
    repeats: int | None = None,
    seed: int | None = None,
    history_path: str | Path | None = None,
    candidate_features: Dict[str, Any] | None = None,
    supporting_query_ids: Sequence[str] | None = None,
    write_probe_samples: int | None = None,
) -> Dict[str, Any]:
    """Run one complete benchmark and return the persisted run record.

    ``iterations``, ``repeats`` and ``seed`` default to the configured label
    protocol (``BENCHMARK_ITERATIONS``, ``BENCHMARK_REPEATS``, ``BENCHMARK_SEED``),
    so runs started from the API, the dashboard, the CLI and the pipeline are
    comparable unless a caller asks for something else on purpose.

    Raises ``ValueError`` for invalid candidates or protocol values and
    ``BenchmarkError`` (a ``RuntimeError``) for database failures. Failed runs
    are also written to the history file with ``status = FAILED``.
    """

    normalized = validate_candidate(candidate_id, table, columns)
    index_name = build_index_name(table, normalized)
    protocol = label_protocol()
    iterations = protocol["iterations"] if iterations is None else int(iterations)
    repeats = protocol["repeats"] if repeats is None else int(repeats)
    seed = protocol["seed"] if seed is None else int(seed)
    validate_protocol_values(iterations, repeats)
    probe_samples = settings.benchmark_write_probe_samples if write_probe_samples is None else write_probe_samples
    snapshot = dict(candidate_features) if candidate_features else load_candidate_snapshot(candidate_id)
    if supporting_query_ids is None:
        supporting_query_ids = [q for q in str(snapshot.get("supporting_query_ids", "")).split(";") if q]

    templates = _workload_templates()
    signature = workload_signature(templates, iterations, seed, repeats)
    record: Dict[str, Any] = {
        "run_id": f"BR-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}",
        "protocol_version": PROTOCOL_VERSION,
        "status": "RUNNING",
        "started_at": _now(),
        "candidate_id": candidate_id,
        "table": table,
        "columns": list(normalized),
        "index_name": index_name,
        "test_database": settings.db_test_name,
        "label_source": "measured_benchmark",
        "workload": {
            "iterations": iterations,
            "seed": seed,
            "repeats": repeats,
            "warm_up_passes": 1,
            "query_ids": sorted(templates),
            "signature": signature,
            "label_protocol_signature": protocol["workload_signature"],
            "matches_label_protocol": signature == protocol["workload_signature"],
        },
        "supporting_query_ids": list(supporting_query_ids),
        "candidate_features": snapshot,
        "rule_score": snapshot.get("rule_score") or snapshot.get("score") or None,
        "cleanup": None,
        "error": None,
    }

    with benchmark_lock(candidate_id):
        index_created = False
        try:
            dropped = reset_test_indexes(_reference_indexes())
            record["reset_dropped_indexes"] = dropped
            connection = connect_to_test_database(raise_on_warnings=False)
            cursor = connection.cursor()
            try:
                cursor.execute("SET SESSION MAX_EXECUTION_TIME = %s", (int(settings.benchmark_query_timeout_ms),))
                cursor.execute("SELECT VERSION()")
                record["server_version"] = str(cursor.fetchone()[0])
                record["dataset_signature"] = _dataset_signature(cursor)
                plan = _build_replay_plan(cursor, iterations, seed)
                executions = {}
                for query in plan:
                    executions[query.query_id] = executions.get(query.query_id, 0) + 1

                before = _measure_phase(connection, cursor, plan, repeats, table, normalized, probe_samples, seed)
                application = apply_index(candidate_id, table, normalized)
                index_created = True
                record["index_sql"] = application.sql
                record["index_size_bytes"] = measure_index_size_bytes(cursor, table, index_name)
                record["index_size_source"] = "measured" if record["index_size_bytes"] is not None else "unavailable"
                after = _measure_phase(connection, cursor, plan, repeats, table, normalized, probe_samples, seed)
            except MySQLError as exc:
                raise_classified(exc, "Benchmark execution")
            finally:
                cursor.close()
                connection.close()

            explain_available = all(after["explain"].get(qid) is not None for qid in executions)
            rows: List[Dict[str, Any]] = []
            for query_id in sorted(before["timings"]):
                metrics = compare_timings(before["timings"][query_id], after["timings"].get(query_id, []))
                after_plan = after["explain"].get(query_id)
                uses_index = None if after_plan is None else any(str(r.get("key")) == index_name for r in after_plan)
                supporting = query_id in supporting_query_ids
                affected = bool(uses_index) if explain_available else supporting
                rows.append({
                    "candidate_id": candidate_id,
                    "query_id": query_id,
                    **metrics,
                    "before_rows_examined": before["rows_examined"].get(query_id),
                    "after_rows_examined": after["rows_examined"].get(query_id),
                    "before_key": _keys(before["explain"].get(query_id)),
                    "after_key": _keys(after_plan),
                    "uses_candidate_index": uses_index,
                    "supporting": supporting,
                    "affected": affected,
                    "run_id": record["run_id"],
                })

            summary = summarize_workload(rows, {qid: len(before["timings"][qid]) for qid in before["timings"]})
            summary["affected_set_source"] = "explain" if explain_available else "supporting_queries"
            summary["p95_min_samples"] = MIN_P95_SAMPLES
            record.update({
                "status": "COMPLETED",
                "summary": summary,
                "queries": rows,
                "write_overhead": _write_overhead(before["write_probe"], after["write_probe"]),
                "explain_before": before["explain"],
                "explain_after": after["explain"],
            })
            _write_results(rows, output_path)
        except (BenchmarkError, ValueError) as exc:
            record["status"] = "FAILED"
            record["error"] = {"code": getattr(exc, "code", "invalid_request"), "message": str(exc)}
            raise
        except Exception as exc:
            record["status"] = "FAILED"
            record["error"] = {"code": "unexpected_error", "message": f"{type(exc).__name__}: {exc}"}
            raise BenchmarkError("unexpected_error", str(exc)) from exc
        finally:
            if index_created:
                try:
                    drop_index(index_name, table)
                    record["cleanup"] = {"status": "dropped", "index_name": index_name}
                except Exception as exc:  # next run's reset step removes leftovers
                    record["cleanup"] = {"status": "failed", "index_name": index_name, "error": str(exc)}
                    logger.error("Cleanup failed for %s: %s", index_name, exc)
            record["finished_at"] = _now()
            append_history(record, history_path)
            record_training_sample(record)
    return record


def record_training_sample(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Persist the run as a training sample in the durable dataset store (never raises)."""

    try:
        from backend.services.ml.store import open_store

        sample = open_store().record_benchmark(record)
        logger.info(
            "Recorded training sample %s for %s (%s%s)", sample["sample_id"], record.get("candidate_id"),
            sample["validation_status"], f": {sample['exclusion_reason']}" if sample.get("exclusion_reason") else "",
            extra={"run_id": record.get("run_id")},
        )
        return sample
    except Exception as exc:  # the benchmark result is already in the history file
        logger.warning("Training sample for run %s could not be recorded: %s", record.get("run_id"), exc)
        return None


def run_benchmark(
    candidate_id: str,
    table: str,
    columns: str,
    iterations: int | None = None,
    output_path: str | Path = DEFAULT_OUTPUT_PATH,
) -> list[dict[str, object]]:
    """Benchmark one index candidate and return per-query rows (legacy contract)."""

    return run_benchmark_detailed(candidate_id, table, columns, iterations, output_path)["queries"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark a QueryScale index recommendation on the test database.")
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--table", required=True)
    parser.add_argument("--columns", required=True, help="Comma-separated index columns.")
    parser.add_argument("--iterations", type=int, default=None, help=f"Default: BENCHMARK_ITERATIONS ({settings.benchmark_iterations}).")
    parser.add_argument("--repeats", type=int, default=None, help=f"Default: BENCHMARK_REPEATS ({settings.benchmark_repeats}).")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--output-path", default=str(DEFAULT_OUTPUT_PATH))
    args = parser.parse_args()

    record = run_benchmark_detailed(
        args.candidate_id, args.table, args.columns, args.iterations, args.output_path,
        repeats=args.repeats, seed=args.seed,
    )
    summary = record["summary"]
    workload = record["workload"]
    print(f"Run {record['run_id']}: {len(record['queries'])} query groups for {args.candidate_id}.")
    print(
        f"Protocol: {workload['iterations']} iterations x {workload['repeats']} repeats, seed {workload['seed']}; "
        f"{'matches' if workload['matches_label_protocol'] else 'DOES NOT match'} the configured label protocol."
    )
    print(f"Workload benefit (label): {summary['workload_benefit']:.4f}; affected: {', '.join(summary['affected_queries']) or 'none'}")
    print(f"Results written to: {args.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
