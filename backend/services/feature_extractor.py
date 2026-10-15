"""Workload and candidate feature extraction for QueryScale.

Two levels of features are produced:

* Query features (``data/query_features.csv``): one row per query template with
  parsed structure, execution counts, latency statistics, and row metrics.
* Candidate features (``data/candidate_features.csv``): one row per index
  candidate with aggregated workload evidence and schema statistics. The legacy
  columns are kept first so older consumers continue to work.

Unavailable metrics are written as empty CSV cells (``None`` in Python, NaN in
pandas). They are never replaced with zero.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.services.query_analyzer import analyze_query, normalize_sql
from backend.services.schema_metadata import SchemaMetadata, load_metadata
from backend.services.stats import mean_or_none, median_or_none, optional_float, p95_or_none


WRITE_QUERY_TYPES = {"INSERT", "UPDATE", "DELETE"}

QUERY_FEATURE_FIELDS = [
    "query_id",
    "normalized_sql",
    "query_type",
    "parse_status",
    "parse_error",
    "tables",
    "where_columns",
    "join_columns",
    "order_columns",
    "group_columns",
    "predicates",
    "frequency_label",
    "execution_count",
    "workload_share",
    "time_share",
    "total_latency_ms",
    "avg_latency_ms",
    "median_latency_ms",
    "p95_latency_ms",
    "min_latency_ms",
    "max_latency_ms",
    "rows_returned_avg",
    "rows_examined_avg",
    "first_seen",
    "last_seen",
    "observation_window_seconds",
]

LEGACY_CANDIDATE_FIELDS = [
    "candidate_id",
    "table",
    "columns",
    "frequency",
    "average_execution_time",
    "where_usage",
    "join_usage",
    "order_usage",
    "table_size",
    "existing_index",
]

CANDIDATE_FEATURE_FIELDS = LEGACY_CANDIDATE_FIELDS + [
    "database",
    "index_name",
    "index_type",
    "column_count",
    "generation_rules",
    "supporting_query_ids",
    "supporting_query_count",
    "supporting_observations",
    "workload_share",
    "time_share",
    "median_execution_time",
    "p95_execution_time",
    "group_usage",
    "equality_prefix_length",
    "has_range_column",
    "covers_order_by",
    "covers_group_by",
    "is_covering",
    "leading_column_cardinality",
    "leading_column_selectivity",
    "cardinality_source",
    "table_row_count_source",
    "table_reads",
    "table_writes",
    "write_ratio",
    "covered_by",
    "existing_prefix_index",
    "existing_prefix_length",
    "estimated_index_bytes",
    "storage_source",
    "drawbacks",
    "explanation",
    "metadata_source",
]


# ---------------------------------------------------------------------------
# Query log loading
# ---------------------------------------------------------------------------

def _read_query_logs(path: str | Path) -> List[Dict[str, str]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Query log file not found: {csv_path}")
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _parse_timestamp(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _fallback_query_id(normalized: str) -> str:
    return "QH_" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10].upper()


# ---------------------------------------------------------------------------
# Query-level features
# ---------------------------------------------------------------------------

def extract_query_features(
    log_rows: Sequence[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Group log rows by query and compute per-query features plus a workload summary.

    Returns ``(query_features, workload)``. Each query feature includes an
    ``analysis`` entry with the full parser output for candidate generation.
    """

    groups: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    for row in log_rows:
        template = row.get("query_template") or ""
        normalized = normalize_sql(template)
        query_id = (row.get("query_id") or "").strip() or _fallback_query_id(normalized)
        group = groups.setdefault(query_id, {
            "template": template,
            "normalized": normalized,
            "frequency_label": row.get("frequency", ""),
            "latencies": [],
            "rows_returned": [],
            "rows_examined": [],
            "timestamps": [],
        })
        latency = optional_float(row.get("execution_time_ms"))
        if latency is not None and latency >= 0:
            group["latencies"].append(latency)
        for source, target in (("rows_returned", "rows_returned"), ("rows_examined", "rows_examined")):
            value = optional_float(row.get(source))
            if value is not None:
                group[target].append(value)
        timestamp = _parse_timestamp(row.get("timestamp"))
        if timestamp is not None:
            group["timestamps"].append(timestamp)

    total_executions = sum(len(g["latencies"]) for g in groups.values())
    total_latency = sum(sum(g["latencies"]) for g in groups.values())
    table_activity: Dict[str, Dict[str, int]] = {}
    all_timestamps: List[datetime] = []
    features: List[Dict[str, Any]] = []

    for query_id, group in groups.items():
        analysis = analyze_query(group["template"])
        latencies = group["latencies"]
        count = len(latencies)
        timestamps = sorted(group["timestamps"])
        all_timestamps.extend(timestamps)
        is_write = analysis["query_type"] in WRITE_QUERY_TYPES
        for table in analysis["tables"]:
            activity = table_activity.setdefault(table, {"reads": 0, "writes": 0})
            activity["writes" if is_write else "reads"] += count

        features.append({
            "query_id": query_id,
            "normalized_sql": analysis["normalized_sql"],
            "query_type": analysis["query_type"],
            "parse_status": analysis["parse_status"],
            "parse_error": analysis["parse_error"],
            "tables": analysis["tables"],
            "where_columns": analysis["where_columns"],
            "join_columns": analysis["join_columns"],
            "order_columns": analysis["order_columns"],
            "group_columns": analysis["group_columns"],
            "predicates": analysis["predicates"],
            "frequency_label": group["frequency_label"],
            "execution_count": count,
            "workload_share": round(count / total_executions, 6) if total_executions else None,
            "time_share": round(sum(latencies) / total_latency, 6) if total_latency else None,
            "total_latency_ms": round(sum(latencies), 3),
            "avg_latency_ms": mean_or_none(latencies),
            "median_latency_ms": median_or_none(latencies),
            "p95_latency_ms": p95_or_none(latencies),
            "min_latency_ms": round(min(latencies), 3) if latencies else None,
            "max_latency_ms": round(max(latencies), 3) if latencies else None,
            "rows_returned_avg": mean_or_none(group["rows_returned"]),
            "rows_examined_avg": mean_or_none(group["rows_examined"]),
            "first_seen": timestamps[0].isoformat() if timestamps else None,
            "last_seen": timestamps[-1].isoformat() if timestamps else None,
            "observation_window_seconds": (
                (timestamps[-1] - timestamps[0]).total_seconds() if len(timestamps) >= 2 else None
            ),
            "analysis": analysis,
        })

    for activity in table_activity.values():
        total = activity["reads"] + activity["writes"]
        activity["write_ratio"] = round(activity["writes"] / total, 6) if total else None

    all_timestamps.sort()
    workload = {
        "total_executions": total_executions,
        "total_latency_ms": round(total_latency, 3),
        "query_count": len(features),
        "table_activity": table_activity,
        "observation_start": all_timestamps[0].isoformat() if all_timestamps else None,
        "observation_end": all_timestamps[-1].isoformat() if all_timestamps else None,
        "unparsed_queries": [f["query_id"] for f in features if f["parse_status"] == "error"],
    }
    return features, workload


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (list, tuple)):
        if value and isinstance(value[0], dict):
            return json.dumps(list(value), sort_keys=True)
        return ";".join(str(item) for item in value)
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return value


def write_rows(rows: Iterable[Dict[str, Any]], path: str | Path, fieldnames: Sequence[str]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fieldnames})
    return output


def write_query_features(rows: Sequence[Dict[str, Any]], path: str | Path) -> Path:
    return write_rows(rows, path, QUERY_FEATURE_FIELDS)


# ---------------------------------------------------------------------------
# Candidate-level features (legacy entry point)
# ---------------------------------------------------------------------------

def extract_features(
    log_path: str | Path,
    output_path: str | Path,
    metadata: SchemaMetadata | None = None,
    query_feature_path: str | Path | None = None,
    max_candidates: int | None = None,
    max_per_table: int | None = None,
    max_columns: int | None = None,
) -> List[Dict[str, Any]]:
    """Extract query features, generate index candidates, and write candidate features.

    When ``metadata`` is not provided this function does not connect to MySQL:
    it uses the saved metadata snapshot or, failing that, ``database/schema.sql``.
    Callers that want live statistics should load metadata explicitly.
    """

    from backend.services.candidate_generator import build_candidates

    log_rows = _read_query_logs(log_path)
    schema = metadata if metadata is not None else load_metadata(source="snapshot_or_schema", save=False)
    query_features, workload = extract_query_features(log_rows)
    if query_feature_path is not None:
        write_query_features(query_features, query_feature_path)

    candidates = build_candidates(
        query_features,
        workload,
        schema,
        max_candidates=max_candidates,
        max_per_table=max_per_table,
        max_columns=max_columns,
    )
    write_rows(candidates, output_path, CANDIDATE_FEATURE_FIELDS)
    return candidates


if __name__ == "__main__":
    extract_features("data/query_logs.csv", "data/candidate_features.csv", query_feature_path="data/query_features.csv")
