"""Model feature schema and preprocessing.

All features are derived from candidate feature rows produced before any index
is created, so the same vector is available at training time (from the
snapshot stored with each benchmark run) and at prediction time. Measured
storage overhead is intentionally excluded because it only exists after a
benchmark and would leak post-treatment information into predictions.

Schema contract
---------------
``FEATURE_COLUMNS`` is the single authoritative list: 21 names in a fixed
order. Every training frame and every prediction frame must have exactly these
columns, in this order, as floats. ``validate_feature_frame`` enforces that and
raises ``FeatureSchemaError`` with the missing, unexpected, or misordered names
instead of letting a mismatched frame reach the estimator.

Missing-value policy: a value that is unavailable, non-numeric, or non-finite
(``inf``, ``-inf``, ``nan``) is represented as NaN. NaN is a first-class value
for the pipeline: the imputer fills it with the training median *and* emits an
indicator column, so "unknown" is visible to the model rather than disguised as
a plausible number. No measured value is ever replaced by an invented one.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional

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

FEATURE_COUNT = len(FEATURE_COLUMNS)

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

# Raw candidate-row columns each feature is derived from (for documentation and the API).
FEATURE_SOURCES: Dict[str, List[str]] = {
    "column_count": ["column_count", "columns"],
    "equality_prefix_length": ["equality_prefix_length"],
    "has_range_column": ["has_range_column"],
    "covers_order_by": ["covers_order_by"],
    "covers_group_by": ["covers_group_by"],
    "is_covering": ["is_covering"],
    "supporting_query_count": ["supporting_query_count", "supporting_query_ids"],
    "workload_share": ["workload_share"],
    "time_share": ["time_share"],
    "log_avg_latency_ms": ["average_execution_time"],
    "log_median_latency_ms": ["median_execution_time"],
    "log_p95_latency_ms": ["p95_execution_time"],
    "where_usage_ratio": ["where_usage", "supporting_observations", "frequency"],
    "join_usage_ratio": ["join_usage", "supporting_observations", "frequency"],
    "order_usage_ratio": ["order_usage", "supporting_observations", "frequency"],
    "log_table_rows": ["table_size"],
    "leading_selectivity": ["leading_column_selectivity"],
    "log_leading_cardinality": ["leading_column_cardinality"],
    "write_ratio": ["write_ratio"],
    "existing_prefix_ratio": ["existing_prefix_length", "column_count"],
    "log_estimated_index_mib": ["estimated_index_bytes"],
}

assert set(FEATURE_DESCRIPTIONS) == set(FEATURE_COLUMNS) == set(FEATURE_SOURCES)


class FeatureSchemaError(ValueError):
    """A frame or row does not match ``FEATURE_COLUMNS`` exactly."""


def _finite(value: Optional[float]) -> float:
    """Return the value, or NaN when it is missing or non-finite."""

    if value is None or not math.isfinite(value):
        return np.nan
    return float(value)


def _log1p(value: Any) -> float:
    number = optional_float(value)
    if number is None or not math.isfinite(number) or number < 0:
        return np.nan
    return math.log1p(number)


def _log10p(value: Any) -> float:
    number = optional_float(value)
    if number is None or not math.isfinite(number) or number < 0:
        return np.nan
    return math.log10(1 + number)


def _value(value: Any) -> float:
    return _finite(optional_float(value))


def _ratio(numerator: Any, denominator: Any) -> float:
    top = optional_float(numerator)
    bottom = optional_float(denominator)
    if top is None or bottom is None or bottom <= 0 or not math.isfinite(top) or not math.isfinite(bottom):
        return np.nan
    return top / bottom


def candidate_feature_vector(row: Mapping[str, Any]) -> Dict[str, float]:
    """Convert one candidate feature row (CSV strings or typed values) to the 21 model features.

    The result always has exactly ``FEATURE_COLUMNS`` as keys, in order, with
    float or NaN values. Unknown keys in ``row`` are ignored; nothing is
    invented for keys that are absent.
    """

    if not isinstance(row, Mapping):
        raise FeatureSchemaError(f"A candidate feature row must be a mapping, got {type(row).__name__}")

    columns = [c for c in str(row.get("columns", "")).replace(";", ",").split(",") if c.strip()]
    column_count = optional_float(row.get("column_count"))
    if column_count is None or not math.isfinite(column_count) or column_count <= 0:
        column_count = float(max(1, len(columns)))
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
    vector = {
        "column_count": column_count,
        "equality_prefix_length": _value(row.get("equality_prefix_length")),
        "has_range_column": float(parse_bool(row.get("has_range_column"))),
        "covers_order_by": float(parse_bool(row.get("covers_order_by"))),
        "covers_group_by": float(parse_bool(row.get("covers_group_by"))),
        "is_covering": float(parse_bool(row.get("is_covering"))),
        "supporting_query_count": _finite(query_count),
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
        "existing_prefix_ratio": (
            _finite(prefix / column_count) if prefix is not None and math.isfinite(prefix) and column_count else np.nan
        ),
        "log_estimated_index_mib": (
            math.log1p(estimated_bytes / (1024 * 1024))
            if estimated_bytes is not None and math.isfinite(estimated_bytes) and estimated_bytes >= 0
            else np.nan
        ),
    }
    return {name: vector[name] for name in FEATURE_COLUMNS}


def validate_feature_frame(frame: pd.DataFrame, context: str = "feature frame") -> pd.DataFrame:
    """Check a frame against the exact 21-feature schema and return it as floats.

    Raises ``FeatureSchemaError`` naming missing, unexpected, or misordered
    columns, or columns that cannot be interpreted as numbers. Non-finite
    values are converted to NaN (the documented "unavailable" marker).
    """

    if not isinstance(frame, pd.DataFrame):
        raise FeatureSchemaError(f"{context}: expected a pandas DataFrame, got {type(frame).__name__}")
    present = [str(c) for c in frame.columns]
    missing = [c for c in FEATURE_COLUMNS if c not in present]
    unexpected = [c for c in present if c not in FEATURE_COLUMNS]
    if missing or unexpected:
        parts = []
        if missing:
            parts.append(f"missing {missing}")
        if unexpected:
            parts.append(f"unexpected {unexpected}")
        raise FeatureSchemaError(
            f"{context}: columns do not match the {FEATURE_COUNT}-feature schema "
            f"(version {FEATURE_SCHEMA_VERSION}): " + "; ".join(parts)
        )
    if present != FEATURE_COLUMNS:
        raise FeatureSchemaError(
            f"{context}: columns are in the wrong order; expected {FEATURE_COLUMNS}, got {present}"
        )
    try:
        numeric = frame.apply(pd.to_numeric, errors="raise").astype(float)
    except (TypeError, ValueError) as exc:
        bad = [c for c in present if not pd.api.types.is_numeric_dtype(frame[c])]
        raise FeatureSchemaError(f"{context}: non-numeric values in columns {bad or present}: {exc}") from exc
    return numeric.replace([np.inf, -np.inf], np.nan)


def to_feature_frame(rows: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
    """Build a validated feature frame (exactly ``FEATURE_COLUMNS``, floats, NaN for unavailable)."""

    frame = pd.DataFrame([candidate_feature_vector(row) for row in rows], columns=FEATURE_COLUMNS)
    return validate_feature_frame(frame, context="candidate features")


def feature_schema() -> Dict[str, Any]:
    """Machine-readable description of the feature contract (served by the API)."""

    return {
        "version": FEATURE_SCHEMA_VERSION,
        "count": FEATURE_COUNT,
        "names": list(FEATURE_COLUMNS),
        "features": [
            {"name": name, "description": FEATURE_DESCRIPTIONS[name], "derived_from": FEATURE_SOURCES[name]}
            for name in FEATURE_COLUMNS
        ],
        "missing_value_policy": "Unavailable or non-finite inputs become NaN; the imputer fills NaN with the training "
                                "median and adds an indicator column per feature that was ever missing.",
    }


def build_imputer() -> SimpleImputer:
    """Median imputation with explicit missing-value indicator columns."""

    return SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)
