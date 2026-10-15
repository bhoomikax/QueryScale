"""Index candidate generation and rule-based candidate scoring.

``build_candidates`` turns query features into validated, deduplicated index
candidates with workload evidence. Column order follows MySQL/InnoDB B-tree
behavior:

* equality (and IN / IS NULL) columns first,
* then at most one range column, because columns after a range cannot be used
  for lookups,
* or, instead of a range, ORDER BY / GROUP BY columns, which lets MySQL read rows
  in index order and skip a filesort or temporary table.

Secondary InnoDB indexes implicitly end with the primary key columns, so an
existing index ``(a)`` on a table with primary key ``id`` also covers ``(a, id)``.

``generate_candidates`` keeps its original file-based contract: it reads the
candidate feature CSV, applies the rule-based scorer, and writes
``index_candidates.csv`` with every non-covered candidate.
"""

from __future__ import annotations

import csv
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from backend.config import settings
from backend.services.query_analyzer import EQUALITY_KINDS, IN_LIST, LIKE, RANGE
from backend.services.rule_scorer import RuleWeights, load_rule_weights, score_candidate
from backend.services.schema_metadata import NON_INDEXABLE_TYPES, SchemaMetadata, TableMeta, column_byte_estimate
from backend.services.stats import optional_float


INDEX_TYPE = "BTREE"
INNODB_FILL_FACTOR = 0.69
INNODB_RECORD_OVERHEAD_BYTES = 7

SCORED_EXTRA_FIELDS = [
    "rule_score",
    "priority",
    "positive_evidence",
    "costs",
    "unavailable_metrics",
    "score_components",
]


def candidate_id_for(table: str, columns: Sequence[str]) -> str:
    """Candidate ID format shared with ``index_manager.validate_candidate``."""

    return f"IDX_{table.upper()}_{'_'.join(column.upper() for column in columns)}"


def index_name_for(table: str, columns: Sequence[str]) -> str:
    return f"idx_{table.lower()}_{'_'.join(column.lower() for column in columns)}"


def _unique(values: Iterable[str]) -> List[str]:
    seen: List[str] = []
    lowered: set[str] = set()
    for value in values:
        if value and value.lower() not in lowered:
            seen.append(value)
            lowered.add(value.lower())
    return seen


def _effective_columns(index_columns: Sequence[str], primary_key: Sequence[str], is_primary: bool) -> List[str]:
    columns = list(index_columns)
    if not is_primary:
        lowered = {c.lower() for c in columns}
        columns.extend(c for c in primary_key if c.lower() not in lowered)
    return columns


def coverage(table: TableMeta, columns: Sequence[str]) -> Tuple[Optional[str], Optional[str], int]:
    """Return (covered_by, longest_prefix_index, longest_prefix_length) for a candidate."""

    wanted = [c.lower() for c in columns]
    primary_key = table.primary_key
    best_name: Optional[str] = None
    best_length = 0
    for index in table.indexes:
        effective = [c.lower() for c in _effective_columns(index.columns, primary_key, index.primary)]
        if effective[: len(wanted)] == wanted:
            return index.name, index.name, len(wanted)
        length = 0
        for left, right in zip(effective, wanted):
            if left != right:
                break
            length += 1
        if length > best_length:
            best_name, best_length = index.name, length
    return None, best_name, best_length


def _resolve_predicate_table(item: Dict[str, Any], query_tables: Sequence[str], schema: SchemaMetadata) -> Optional[str]:
    table = schema.canonical_table_name(item.get("table"))
    if table:
        return table
    owners = [
        schema.canonical_table_name(name)
        for name in query_tables
        if schema.table(name) is not None and schema.table(name).column(item.get("column", "")) is not None
    ]
    return owners[0] if len(owners) == 1 else None


def _patterns_for_table(
    table: TableMeta,
    analysis: Dict[str, Any],
    schema: SchemaMetadata,
    max_columns: int,
) -> List[Tuple[List[str], str, Dict[str, Any]]]:
    query_tables = analysis.get("tables", [])

    def on_table(items: Iterable[Dict[str, Any]], column_key: str = "column") -> List[Dict[str, Any]]:
        selected = []
        for item in items:
            resolved = _resolve_predicate_table({"table": item.get("table"), "column": item.get(column_key)}, query_tables, schema)
            if resolved == table.name:
                selected.append(item)
        return selected

    predicates = [p for p in on_table(analysis.get("predicates", [])) if p.get("source") in {"where", "join"}]
    equality_items = [p for p in predicates if p["kind"] in EQUALITY_KINDS or p["kind"] == "unknown"]
    equality = _unique(p["column"] for p in equality_items)
    has_in = any(p["kind"] == IN_LIST for p in equality_items)
    ranges = _unique(p["column"] for p in predicates if p["kind"] in {RANGE, LIKE})

    # Equality on the full primary key is already an optimal point lookup.
    primary_key = [c.lower() for c in table.primary_key]
    if primary_key and set(primary_key) <= {c.lower() for c in equality}:
        return []

    # Order equality columns by known cardinality (most selective first), then by name. Any order
    # of an equality prefix serves the same lookups, so a canonical order avoids duplicate permutations.
    def cardinality_key(column: str) -> Tuple[int, str]:
        meta = table.column(column)
        known = meta.cardinality if meta is not None and meta.cardinality is not None else -1
        return (-known, column.lower())

    equality = sorted(equality, key=cardinality_key)

    join_columns: List[str] = []
    for pair in analysis.get("join_pairs", []):
        for side in ("left", "right"):
            item = {"table": pair.get(f"{side}_table"), "column": pair.get(f"{side}_column")}
            if _resolve_predicate_table(item, query_tables, schema) == table.name:
                join_columns.append(item["column"])
    join_columns = _unique(join_columns)

    order_items = analysis.get("order_by", [])
    order: List[str] = []
    if order_items and all(item.get("indexable") for item in order_items):
        resolved = on_table(order_items)
        directions = {item["direction"] for item in order_items}
        if len(resolved) == len(order_items) and len(directions) == 1:
            order = _unique(item["column"] for item in resolved)

    group_items = analysis.get("group_by", [])
    group: List[str] = []
    if group_items and all(item.get("indexable") for item in group_items):
        resolved = on_table(group_items)
        if len(resolved) == len(group_items):
            group = _unique(item["column"] for item in resolved)

    projection = analysis.get("projection") or {"star": True, "columns": []}
    projected: Optional[List[str]] = None
    if not projection.get("star"):
        projected_items = on_table(projection.get("columns", []))
        if len(projected_items) == len(projection.get("columns", [])):
            projected = _unique(item["column"] for item in projected_items)

    context = {
        "equality": equality,
        "ranges": ranges,
        "order": order,
        "group": group,
        "join": join_columns,
        "has_in": has_in,
    }
    patterns: List[Tuple[List[str], str, Dict[str, Any]]] = []

    def add(columns: List[str], rule: str) -> None:
        cleaned = _unique(columns)[:max_columns]
        if cleaned:
            patterns.append((cleaned, rule, context))

    if equality:
        add(equality, "equality")
    if ranges:
        add(equality[: max_columns - 1] + [ranges[0]], "equality+range" if equality else "range")
    if order and not ranges and not has_in and (equality or analysis.get("has_limit")):
        add(equality + order, "equality+order" if equality else "order+limit")
    if group and not ranges and not has_in:
        add(equality + group, "equality+group" if equality else "group")
    for column in join_columns:
        add([column], "join")
        if equality and column.lower() not in {c.lower() for c in equality}:
            add(equality + [column], "equality+join")
    base = equality + ranges[:1]
    if projected is not None and base:
        extra = [c for c in projected if c.lower() not in {b.lower() for b in base} and c.lower() not in primary_key]
        if extra and len(base) + len(extra) <= max_columns:
            add(base + extra, "covering")
    return patterns


def _candidate_flags(columns: List[str], rule: str, context: Dict[str, Any]) -> Dict[str, Any]:
    lowered = [c.lower() for c in columns]
    equality = [c.lower() for c in context["equality"]]
    eq_prefix = 0
    for column in lowered:
        if column in equality:
            eq_prefix += 1
        else:
            break
    after_prefix = lowered[eq_prefix:]
    order = [c.lower() for c in context["order"]]
    group = [c.lower() for c in context["group"]]
    return {
        "equality_prefix_length": eq_prefix,
        "has_range_column": bool(after_prefix and after_prefix[0] in {c.lower() for c in context["ranges"]}),
        "covers_order_by": bool(order) and after_prefix[: len(order)] == order and not context["has_in"],
        "covers_group_by": bool(group) and after_prefix[: len(group)] == group,
        "is_covering": rule == "covering",
        "uses_where": any(c in equality or c in {r.lower() for r in context["ranges"]} for c in lowered),
        "uses_join": any(c in {j.lower() for j in context["join"]} for c in lowered[:1]),
    }


def _estimate_index_bytes(table: TableMeta, columns: Sequence[str]) -> Optional[int]:
    if table.row_count is None:
        return None
    key_bytes = 0
    for name in list(columns) + [c for c in table.primary_key if c.lower() not in {x.lower() for x in columns}]:
        meta = table.column(name)
        key_bytes += column_byte_estimate(meta) if meta is not None else 8
    return int(table.row_count * (key_bytes + INNODB_RECORD_OVERHEAD_BYTES) / INNODB_FILL_FACTOR)


def _weighted_median(pairs: Sequence[Tuple[float, int]]) -> Optional[float]:
    valid = sorted((value, weight) for value, weight in pairs if value is not None and weight > 0)
    if not valid:
        return None
    half = sum(weight for _, weight in valid) / 2
    running = 0
    for value, weight in valid:
        running += weight
        if running >= half:
            return round(value, 3)
    return round(valid[-1][0], 3)


def _rule_sentence(rule: str, columns: List[str], context: Dict[str, Any]) -> str:
    joined = ", ".join(columns)
    sentences = {
        "equality": f"equality filter on {joined}",
        "range": f"range filter on {columns[0]}",
        "equality+range": f"equality filter followed by a range on {columns[-1]}",
        "equality+order": f"equality filter followed by ORDER BY {', '.join(context['order'])}",
        "order+limit": f"ORDER BY {', '.join(context['order'])} with LIMIT",
        "equality+group": f"filter followed by GROUP BY {', '.join(context['group'])}",
        "group": f"GROUP BY {', '.join(context['group'])}",
        "join": f"join lookup on {columns[0]}",
        "equality+join": f"equality filter combined with join column {columns[-1]}",
        "covering": f"covering index for the columns read ({joined})",
    }
    return sentences.get(rule, rule)


def build_candidates(
    query_features: Sequence[Dict[str, Any]],
    workload: Dict[str, Any],
    schema: SchemaMetadata,
    max_candidates: int | None = None,
    max_per_table: int | None = None,
    max_columns: int | None = None,
) -> List[Dict[str, Any]]:
    """Generate validated, deduplicated, bounded index candidates with workload evidence."""

    max_candidates = max_candidates or settings.max_index_candidates
    max_per_table = max_per_table or settings.max_candidates_per_table
    max_columns = max_columns or settings.max_index_columns
    total_executions = workload.get("total_executions") or 0
    total_latency = workload.get("total_latency_ms") or 0.0
    table_activity = workload.get("table_activity", {})

    aggregates: "OrderedDict[Tuple[str, Tuple[str, ...]], Dict[str, Any]]" = OrderedDict()
    for feature in query_features:
        analysis = feature.get("analysis") or {}
        if feature.get("parse_status") == "error" or feature.get("query_type") not in {"SELECT", "UPDATE", "DELETE"}:
            continue
        count = int(feature.get("execution_count") or 0)
        if count <= 0:
            continue
        for raw_table in analysis.get("tables", []):
            table = schema.table(raw_table)
            if table is None:
                continue
            for columns, rule, context in _patterns_for_table(table, analysis, schema, max_columns):
                metas = [table.column(c) for c in columns]
                if any(m is None or m.data_type in NON_INDEXABLE_TYPES for m in metas):
                    continue
                canonical = [m.name for m in metas]
                key = (table.name, tuple(c.lower() for c in canonical))
                flags = _candidate_flags(canonical, rule, context)
                entry = aggregates.setdefault(key, {
                    "table_meta": table,
                    "columns": canonical,
                    "rules": [],
                    "sentences": [],
                    "query_ids": [],
                    "observations": 0,
                    "time_ms": 0.0,
                    "where_usage": 0,
                    "join_usage": 0,
                    "order_usage": 0,
                    "group_usage": 0,
                    "medians": [],
                    "p95": [],
                    "flags": {k: False for k in ("has_range_column", "covers_order_by", "covers_group_by", "is_covering")},
                    "equality_prefix_length": 0,
                })
                if rule not in entry["rules"]:
                    entry["rules"].append(rule)
                    entry["sentences"].append(_rule_sentence(rule, canonical, context))
                if feature["query_id"] not in entry["query_ids"]:
                    entry["query_ids"].append(feature["query_id"])
                    entry["observations"] += count
                    entry["time_ms"] += float(feature.get("total_latency_ms") or 0.0)
                    entry["where_usage"] += count if flags["uses_where"] else 0
                    entry["join_usage"] += count if flags["uses_join"] else 0
                    entry["order_usage"] += count if flags["covers_order_by"] else 0
                    entry["group_usage"] += count if flags["covers_group_by"] else 0
                    entry["medians"].append((feature.get("median_latency_ms"), count))
                    if feature.get("p95_latency_ms") is not None:
                        entry["p95"].append(feature["p95_latency_ms"])
                for flag in entry["flags"]:
                    entry["flags"][flag] = entry["flags"][flag] or flags[flag]
                entry["equality_prefix_length"] = max(entry["equality_prefix_length"], flags["equality_prefix_length"])

    rows: List[Dict[str, Any]] = []
    for (table_name, _), entry in aggregates.items():
        table: TableMeta = entry["table_meta"]
        columns = entry["columns"]
        index_name = index_name_for(table_name, columns)
        if len(index_name) > 64:
            continue
        covered_by, prefix_index, prefix_length = coverage(table, columns)
        leading = table.column(columns[0])
        rows_count = table.row_count
        cardinality = leading.cardinality if leading is not None else None
        activity = table_activity.get(table_name) or next(
            (value for name, value in table_activity.items() if name.lower() == table_name.lower()), {}
        )
        estimated_bytes = _estimate_index_bytes(table, columns)
        observations = entry["observations"]
        drawbacks: List[str] = []
        if activity.get("write_ratio"):
            drawbacks.append(f"Adds maintenance cost to writes on {table_name}")
        if prefix_length and not covered_by:
            drawbacks.append(f"Overlaps existing index {prefix_index}; consider replacing it instead of keeping both")
        if len(columns) >= 3:
            drawbacks.append("Wide composite index increases storage and is only useful when queries match its column order")
        if cardinality is not None and rows_count and cardinality / rows_count < 0.001:
            drawbacks.append(f"Leading column {columns[0]} has low cardinality ({cardinality} distinct values)")
        if estimated_bytes is not None:
            drawbacks.append(f"Estimated additional storage about {estimated_bytes / (1024 * 1024):.1f} MiB")

        explanation = (
            f"Generated from {', '.join(entry['sentences'])} in {', '.join(entry['query_ids'])} "
            f"({observations} observed executions)."
        )
        if covered_by:
            explanation += f" Already covered by existing index {covered_by}."

        rows.append({
            "candidate_id": candidate_id_for(table_name, columns),
            "table": table_name,
            "columns": ",".join(columns),
            "frequency": observations,
            "average_execution_time": round(entry["time_ms"] / observations, 3) if observations else None,
            "where_usage": entry["where_usage"],
            "join_usage": entry["join_usage"],
            "order_usage": entry["order_usage"],
            "table_size": rows_count,
            "existing_index": covered_by is not None,
            "database": schema.database,
            "index_name": index_name,
            "index_type": INDEX_TYPE,
            "column_count": len(columns),
            "generation_rules": entry["rules"],
            "supporting_query_ids": entry["query_ids"],
            "supporting_query_count": len(entry["query_ids"]),
            "supporting_observations": observations,
            "workload_share": round(observations / total_executions, 6) if total_executions else None,
            "time_share": round(entry["time_ms"] / total_latency, 6) if total_latency else None,
            "median_execution_time": _weighted_median(entry["medians"]),
            "p95_execution_time": max(entry["p95"]) if entry["p95"] else None,
            "group_usage": entry["group_usage"],
            "equality_prefix_length": entry["equality_prefix_length"],
            **entry["flags"],
            "leading_column_cardinality": cardinality,
            "leading_column_selectivity": round(cardinality / rows_count, 6) if cardinality is not None and rows_count else None,
            "cardinality_source": leading.cardinality_source if leading is not None else None,
            "table_row_count_source": table.row_count_source,
            "table_reads": activity.get("reads"),
            "table_writes": activity.get("writes"),
            "write_ratio": activity.get("write_ratio"),
            "covered_by": covered_by,
            "existing_prefix_index": prefix_index if prefix_length and not covered_by else None,
            "existing_prefix_length": prefix_length if not covered_by else len(columns),
            "estimated_index_bytes": estimated_bytes,
            "storage_source": "estimated" if estimated_bytes is not None else "unavailable",
            "drawbacks": json.dumps(drawbacks),
            "explanation": explanation,
            "metadata_source": schema.source,
        })

    # Bound the candidate set deterministically by supporting workload time, per table and overall.
    ranked = sorted(
        (row for row in rows if not row["existing_index"]),
        key=lambda r: (-(r["time_share"] or 0.0), -r["supporting_observations"], r["column_count"], r["candidate_id"]),
    )
    kept_ids: set[str] = set()
    per_table: Dict[str, int] = {}
    for row in ranked:
        if len(kept_ids) >= max_candidates or per_table.get(row["table"], 0) >= max_per_table:
            continue
        kept_ids.add(row["candidate_id"])
        per_table[row["table"]] = per_table.get(row["table"], 0) + 1
    return [row for row in rows if row["existing_index"] or row["candidate_id"] in kept_ids]


# ---------------------------------------------------------------------------
# Rule-based scoring of the candidate feature file (legacy entry point)
# ---------------------------------------------------------------------------

def _read_feature_rows(path: str | Path) -> List[Dict[str, str]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Feature file not found: {csv_path}")
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _create_index_sql(table: str, columns: Sequence[str], index_name: str | None) -> str:
    from backend.services.index_manager import build_create_index_sql

    name = index_name or index_name_for(table, columns)
    return build_create_index_sql(name, table, columns) + ";"


def generate_candidates(
    feature_path: str | Path,
    output_path: str | Path,
    weights: RuleWeights | None = None,
) -> List[Dict[str, Any]]:
    """Score candidate features with the rule engine and write non-covered candidates."""

    rule_weights = weights or load_rule_weights(settings.rule_weights_path or None)
    feature_rows = _read_feature_rows(feature_path)
    scored: List[Dict[str, Any]] = []
    fieldnames: List[str] = []

    for row in feature_rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
        result = score_candidate(row, rule_weights)
        if result["rule_score"] <= 0:
            continue
        columns = [c.strip() for c in str(row.get("columns", "")).split(",") if c.strip()]
        try:
            statement = _create_index_sql(row.get("table", ""), columns, row.get("index_name") or None)
        except ValueError:
            continue
        scored.append({
            **row,
            "columns": ", ".join(columns),
            "score": result["rule_score"],
            "rule_score": result["rule_score"],
            "priority": result["priority"],
            "recommended_action": statement,
            "reason": result["reason"],
            "positive_evidence": json.dumps(result["positive_evidence"]),
            "costs": json.dumps(result["costs"]),
            "unavailable_metrics": json.dumps(result["unavailable"]),
            "score_components": json.dumps(result["components"], sort_keys=True),
        })

    scored.sort(key=lambda item: (-float(item["score"]), item.get("candidate_id", "")))
    output_fields = ["candidate_id", "table", "columns", "score", "recommended_action", "reason"]
    output_fields += [f for f in SCORED_EXTRA_FIELDS if f not in output_fields]
    output_fields += [f for f in fieldnames if f not in output_fields]

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(scored)
    return scored


def parse_json_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [item for item in str(value).split(";") if item]
    return parsed if isinstance(parsed, list) else [parsed]


def numeric(value: Any) -> Optional[float]:
    return optional_float(value)


if __name__ == "__main__":
    generate_candidates("data/candidate_features.csv", "data/index_candidates.csv")
