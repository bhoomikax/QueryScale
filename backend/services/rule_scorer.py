"""Explainable rule-based scoring for index candidates.

The score is a heuristic priority on a 0-100 scale. It summarizes workload
evidence and estimated costs. It is not a measured speedup, a probability, or
a guarantee of improvement.

Score definition::

    components  c_i in [0, 1]   (time share, frequency, predicate strength,
                                 table size, leading-column selectivity)
    raw         = sum(w_i * c_i) / sum(w_i)
    raw        *= 1 - existing_prefix_discount * (existing_prefix_length / column_count)
    raw        -= write_penalty * write_ratio + storage_penalty * min(1, est_bytes / 512 MiB)
    score       = 100 * clip(raw, 0, 1)

Unavailable inputs use a neutral component value and are listed in
``unavailable`` so the explanation never implies data that does not exist.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.stats import optional_float, optional_int, parse_bool


@dataclass(frozen=True)
class RuleWeights:
    time_share: float = 0.30
    frequency: float = 0.15
    predicate: float = 0.20
    table_size: float = 0.15
    selectivity: float = 0.20
    write_penalty: float = 0.30
    storage_penalty: float = 0.05
    existing_prefix_discount: float = 0.50
    neutral_unknown: float = 0.50
    high_threshold: float = 50.0
    medium_threshold: float = 25.0


DEFAULT_WEIGHTS = RuleWeights()
_STORAGE_REFERENCE_BYTES = 512 * 1024 * 1024


def load_rule_weights(path: str | Path | None = None) -> RuleWeights:
    """Load weight overrides from a JSON object; unknown keys raise ValueError."""

    if not path:
        return DEFAULT_WEIGHTS
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    allowed = {f.name for f in fields(RuleWeights)}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unknown rule weight keys: {', '.join(sorted(unknown))}")
    return replace(DEFAULT_WEIGHTS, **{key: float(value) for key, value in payload.items()})


def priority_for(score: float, weights: RuleWeights = DEFAULT_WEIGHTS) -> str:
    if score >= weights.high_threshold:
        return "high"
    if score >= weights.medium_threshold:
        return "medium"
    return "low"


def _clip(value: float) -> float:
    return max(0.0, min(1.0, value))


def _split(value: Any) -> List[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v)]
    return [item.strip() for item in str(value or "").replace(";", ",").split(",") if item.strip()]


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _bytes(value: float) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MiB"
    return f"{value / 1024:.1f} KiB"


def score_candidate(candidate: Dict[str, Any], weights: RuleWeights = DEFAULT_WEIGHTS) -> Dict[str, Any]:
    """Score one candidate row (dict or CSV row). Returns score, priority, components, and evidence."""

    table = candidate.get("table", "")
    columns = _split(candidate.get("columns"))
    column_count = optional_int(candidate.get("column_count")) or max(1, len(columns))
    query_ids = _split(candidate.get("supporting_query_ids"))
    observations = optional_int(candidate.get("supporting_observations"))
    if observations is None:
        observations = optional_int(candidate.get("frequency"))

    positive: List[str] = []
    costs: List[str] = []
    unavailable: List[str] = []
    components: Dict[str, float] = {}

    if parse_bool(candidate.get("existing_index")):
        covered_by = candidate.get("covered_by") or "an existing index"
        return {
            "rule_score": 0.0,
            "priority": "low",
            "components": {},
            "positive_evidence": [],
            "costs": [],
            "unavailable": [],
            "reason": f"Not recommended: {table} ({', '.join(columns)}) is already covered by {covered_by}.",
        }

    # Workload time share.
    time_share = optional_float(candidate.get("time_share"))
    if time_share is not None:
        components["time_share"] = math.sqrt(_clip(time_share))
        positive.append(
            f"Supporting queries account for {_fmt_pct(time_share)} of measured workload time"
            + (f" ({', '.join(query_ids)})" if query_ids else "")
        )
    else:
        components["time_share"] = weights.neutral_unknown
        unavailable.append("workload time share")

    workload_share = optional_float(candidate.get("workload_share"))
    if workload_share is not None:
        components["frequency"] = math.sqrt(_clip(workload_share))
        positive.append(f"Observed {observations or 0} supporting executions ({_fmt_pct(workload_share)} of the workload)")
    else:
        components["frequency"] = weights.neutral_unknown
        if observations:
            positive.append(f"Observed {observations} supporting executions")
        unavailable.append("workload frequency share")

    # Predicate strength from how supporting queries use the leading column(s).
    eq_prefix = optional_int(candidate.get("equality_prefix_length")) or 0
    join_usage = optional_float(candidate.get("join_usage")) or 0
    where_usage = optional_float(candidate.get("where_usage")) or 0
    has_range = parse_bool(candidate.get("has_range_column"))
    covers_order = parse_bool(candidate.get("covers_order_by"))
    covers_group = parse_bool(candidate.get("covers_group_by"))
    is_covering = parse_bool(candidate.get("is_covering"))
    if eq_prefix > 0:
        predicate = 1.0
        positive.append(f"Equality predicate on leading column{'s' if eq_prefix > 1 else ''} {', '.join(columns[:eq_prefix])}")
    elif join_usage > 0:
        predicate = 0.85
        positive.append(f"Join lookup on {columns[0]}")
    elif has_range:
        predicate = 0.70
        positive.append(f"Range predicate on {columns[0]}")
    elif where_usage > 0:
        predicate = 0.80
        positive.append(f"Filter predicate on {', '.join(columns)}")
    elif covers_order or covers_group:
        predicate = 0.40
    else:
        predicate = 0.20
    if covers_order:
        predicate += 0.15
        positive.append("Index order matches ORDER BY after the filter prefix, which can avoid a filesort")
    if covers_group:
        predicate += 0.10
        positive.append("Index order matches GROUP BY columns")
    if is_covering:
        predicate += 0.10
        positive.append("Index covers every column the supporting query reads, avoiding row lookups")
    components["predicate"] = _clip(predicate)

    rows = optional_int(candidate.get("table_size"))
    rows_source = candidate.get("table_row_count_source") or "unknown source"
    if rows is not None:
        components["table_size"] = _clip(math.log10(rows + 1) / 6.0)
        positive.append(f"{table} has {rows:,} rows ({rows_source})")
    else:
        components["table_size"] = weights.neutral_unknown
        unavailable.append("table row count")

    cardinality = optional_int(candidate.get("leading_column_cardinality"))
    if cardinality is not None and rows:
        selectivity_component = 0.0 if cardinality <= 1 else _clip(math.log10(cardinality) / math.log10(max(rows, 10)))
        if column_count > 1 and eq_prefix > 1:
            selectivity_component = _clip(selectivity_component + 0.2)
        components["selectivity"] = selectivity_component
        source = candidate.get("cardinality_source") or "unknown source"
        message = f"Leading column {columns[0]} has {cardinality:,} distinct values ({source})"
        if selectivity_component < 0.35:
            costs.append(message + "; low selectivity alone, so benefit depends on the other index columns or ordering")
        else:
            positive.append(message)
    else:
        components["selectivity"] = weights.neutral_unknown
        unavailable.append("leading column cardinality")

    raw = sum(getattr(weights, key) * value for key, value in components.items())
    raw /= sum(getattr(weights, key) for key in components) or 1.0

    prefix_length = optional_int(candidate.get("existing_prefix_length")) or 0
    if prefix_length:
        raw *= 1 - weights.existing_prefix_discount * (prefix_length / column_count)
        costs.append(
            f"Existing index {candidate.get('existing_prefix_index')} already covers the first "
            f"{prefix_length} of {column_count} columns; only the added columns provide new benefit "
            "and the two indexes overlap"
        )

    write_ratio = optional_float(candidate.get("write_ratio"))
    if write_ratio is not None:
        raw -= weights.write_penalty * write_ratio
        if write_ratio > 0:
            costs.append(f"{_fmt_pct(write_ratio)} of captured executions on {table} are writes that must maintain this index")
        else:
            costs.append(f"No writes on {table} were observed in the captured workload; write overhead is not yet measured")
    else:
        unavailable.append("read/write ratio")

    estimated_bytes = optional_float(candidate.get("estimated_index_bytes"))
    if estimated_bytes is not None:
        raw -= weights.storage_penalty * _clip(estimated_bytes / _STORAGE_REFERENCE_BYTES)
        costs.append(f"Estimated index size {_bytes(estimated_bytes)} (heuristic estimate, not measured)")
    else:
        unavailable.append("index storage estimate")

    score = round(100.0 * _clip(raw), 3)
    priority = priority_for(score, weights)
    target = f"{table} ({', '.join(columns)})"
    reason = (
        f"Heuristic priority {priority.upper()} ({score:.1f}/100) for {target}: "
        + ("; ".join(positive[:3]) if positive else "limited workload evidence")
        + ". This is a rule-based score, not a measured speedup."
    )
    return {
        "rule_score": score,
        "priority": priority,
        "components": {key: round(value, 4) for key, value in components.items()},
        "positive_evidence": positive,
        "costs": costs,
        "unavailable": unavailable,
        "reason": reason,
    }


def weights_as_dict(weights: RuleWeights = DEFAULT_WEIGHTS) -> Dict[str, float]:
    return asdict(weights)


def optional_weights(path: Optional[str]) -> RuleWeights:
    return load_rule_weights(path) if path else DEFAULT_WEIGHTS
