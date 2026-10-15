"""Model feature schema and preprocessing.

All features are derived from candidate feature rows produced before any index
is created, so the same vector is available at training time (from the
snapshot stored with each benchmark run) and at prediction time. Measured
storage overhead is intentionally excluded because it only exists after a
benchmark and would leak post-treatment information into predictions.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List

import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer

from backend.services.stats import optional_float, parse_bool


FEATURE_SCHEMA_VERSION = 1

FEATURE_COLUMNS: List[str] = [
    "column_count",
    "equality_prefix_length",
    "has_range_column",
    "covers_order_by",
    "covers_group_by",
    "is_covering",
    "supporting_query_count",
    "workload_share",
    "time_share",
    "log_avg_latency_ms",
    "log_median_latency_ms",
    "log_p95_latency_ms",
    "where_usage_ratio",
    "join_usage_ratio",
    "order_usage_ratio",
    "log_table_rows",
    "leading_selectivity",
    "log_leading_cardinality",
    "write_ratio",
    "existing_prefix_ratio",
    "log_estimated_index_mib",
]

FEATURE_DESCRIPTIONS: Dict[str, str] = {
    "column_count": "Number of index columns",
    "equality_prefix_length": "Leading index columns matched by equality/IN predicates",
    "has_range_column": "1 if the column after the equality prefix is a range predicate",
    "covers_order_by": "1 if index order satisfies ORDER BY after the equality prefix",
    "covers_group_by": "1 if index order satisfies GROUP BY",
    "is_covering": "1 if the index contains every column the supporting query reads",
    "supporting_query_count": "Distinct query templates the candidate supports",
    "workload_share": "Supporting executions / all executions in the query log",
    "time_share": "Supporting latency / total latency in the query log",
    "log_avg_latency_ms": "ln(1 + mean latency of supporting executions in ms)",
    "log_median_latency_ms": "ln(1 + execution-weighted median of supporting query medians)",
    "log_p95_latency_ms": "ln(1 + max supporting P95), missing when fewer than 20 samples",
    "where_usage_ratio": "Supporting executions using the columns in WHERE / supporting executions",
    "join_usage_ratio": "Supporting executions using the leading column as a join key / supporting executions",
    "order_usage_ratio": "Supporting executions whose ORDER BY the index satisfies / supporting executions",
    "log_table_rows": "log10(1 + table row count), missing if unavailable",
    "leading_selectivity": "Distinct leading-column values / table rows, missing if unavailable",
    "log_leading_cardinality": "log10(1 + distinct leading-column values), missing if unavailable",
    "write_ratio": "Write executions / all executions on the table in the captured workload",
    "existing_prefix_ratio": "Leading columns already indexed by an existing index / column count",
    "log_estimated_index_mib": "ln(1 + heuristic index size estimate in MiB), missing if unavailable",
}


def _log1p(value: Any) -> float:
    number = optional_float(value)
    return math.log1p(number) if number is not None and number >= 0 else np.nan


def _log10p(value: Any) -> float:
    number = optional_float(value)
    return math.log10(1 + number) if number is not None and number >= 0 else np.nan


def _value(value: Any) -> float:
    number = optional_float(value)
    return number if number is not None else np.nan


def _ratio(numerator: Any, denominator: Any) -> float:
    top = optional_float(numerator)
    bottom = optional_float(denominator)
    if top is None or bottom is None or bottom <= 0:
        return np.nan
    return top / bottom


def candidate_feature_vector(row: Dict[str, Any]) -> Dict[str, float]:
    """Convert one candidate feature row (CSV strings or typed values) to model features."""

    columns = [c for c in str(row.get("columns", "")).replace(";", ",").split(",") if c.strip()]
    column_count = optional_float(row.get("column_count")) or float(max(1, len(columns)))
    observations = row.get("supporting_observations") or row.get("frequency")
    estimated_bytes = optional_float(row.get("estimated_index_bytes"))
    query_count = optional_float(row.get("supporting_query_count"))
    if query_count is None:
        ids = row.get("supporting_query_ids")
        if isinstance(ids, list):
            query_count = float(len(ids))
        elif ids:
            query_count = float(len([i for i in str(ids).split(";") if i]))
    prefix = optional_float(row.get("existing_prefix_length"))
    return {
        "column_count": column_count,
        "equality_prefix_length": _value(row.get("equality_prefix_length")),
        "has_range_column": float(parse_bool(row.get("has_range_column"))),
        "covers_order_by": float(parse_bool(row.get("covers_order_by"))),
        "covers_group_by": float(parse_bool(row.get("covers_group_by"))),
        "is_covering": float(parse_bool(row.get("is_covering"))),
        "supporting_query_count": query_count if query_count is not None else np.nan,
        "workload_share": _value(row.get("workload_share")),
        "time_share": _value(row.get("time_share")),
        "log_avg_latency_ms": _log1p(row.get("average_execution_time")),
        "log_median_latency_ms": _log1p(row.get("median_execution_time")),
        "log_p95_latency_ms": _log1p(row.get("p95_execution_time")),
        "where_usage_ratio": _ratio(row.get("where_usage"), observations),
        "join_usage_ratio": _ratio(row.get("join_usage"), observations),
        "order_usage_ratio": _ratio(row.get("order_usage"), observations),
        "log_table_rows": _log10p(row.get("table_size")),
        "leading_selectivity": _value(row.get("leading_column_selectivity")),
        "log_leading_cardinality": _log10p(row.get("leading_column_cardinality")),
        "write_ratio": _value(row.get("write_ratio")),
        "existing_prefix_ratio": (prefix / column_count) if prefix is not None and column_count else np.nan,
        "log_estimated_index_mib": math.log1p(estimated_bytes / (1024 * 1024)) if estimated_bytes is not None else np.nan,
    }


def to_feature_frame(rows: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame([candidate_feature_vector(row) for row in rows], columns=FEATURE_COLUMNS)
    return frame.astype(float)


def build_imputer() -> SimpleImputer:
    """Median imputation with explicit missing-value indicator columns."""

    return SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)
