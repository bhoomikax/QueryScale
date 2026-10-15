"""Query performance endpoints backed by Person A's measured query logs."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from backend.config import ROOT_DIR, settings
from backend.services.stats import optional_float


router = APIRouter(prefix="/queries", tags=["queries"])


def _configured_log_path() -> Path:
    path = Path(settings.query_log_path)
    return path if path.is_absolute() else ROOT_DIR / path


def _split_field(value: str) -> list[str]:
    return [item.strip() for item in value.split(";") if item.strip()]


def read_query_logs(log_path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read measured query executions from the configured CSV log."""

    path = Path(log_path) if log_path is not None else _configured_log_path()
    if not path.exists():
        raise FileNotFoundError(f"Query log file not found: {path}")

    rows: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            rows.append({
                "query_id": row.get("query_id", ""),
                "query_template": row.get("query_template", ""),
                "timestamp": row.get("timestamp", ""),
                "execution_time_ms": float(row.get("execution_time_ms", "0") or 0),
                "frequency": row.get("frequency", ""),
                "tables": _split_field(row.get("tables", "")),
                "where_columns": _split_field(row.get("where_columns", "")),
                "join_columns": _split_field(row.get("join_columns", "")),
                "order_columns": _split_field(row.get("order_columns", "")),
                "rows_returned": optional_float(row.get("rows_returned")),
                "rows_examined": optional_float(row.get("rows_examined")),
            })
    return rows


def _load_or_raise() -> list[dict[str, Any]]:
    try:
        return read_query_logs()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("")
def get_queries(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
    """Return measured query executions from the query log."""

    return _load_or_raise()[:limit]


@router.get("/slow")
def get_slow_queries(
    threshold_ms: float = Query(default=100.0, ge=0),
    limit: int = Query(default=100, ge=1, le=1000),
) -> list[dict[str, Any]]:
    """Return measured executions at or above the requested latency threshold."""

    rows = [row for row in _load_or_raise() if row["execution_time_ms"] >= threshold_ms]
    rows.sort(key=lambda row: row["execution_time_ms"], reverse=True)
    return rows[:limit]

@router.get("/summary")
def get_query_summary() -> list[dict[str, Any]]:
    """Per-query workload features: counts, latency statistics, and parsed structure.

    P95 is null when a query has fewer than 20 observations. Rows examined is
    null when performance_schema data was not captured.
    """

    from backend.services.feature_extractor import extract_query_features, _read_query_logs

    try:
        log_rows = _read_query_logs(_configured_log_path())
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    features, _ = extract_query_features(log_rows)
    summary = []
    for feature in features:
        item = {key: value for key, value in feature.items() if key != "analysis"}
        summary.append(item)
    summary.sort(key=lambda row: -(row.get("total_latency_ms") or 0))
    return summary
