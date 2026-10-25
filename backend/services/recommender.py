"""Recommendation ranking: rule-based baseline with optional ML re-ranking.

* ``score`` and ``priority`` always come from the rule-based engine
  (``rule_scorer``). They are heuristic and are labelled as such.
* When a validated model is usable (see ``ml.predictor.load_ranker``),
  candidates are ordered by ``ml_predicted_benefit``, a model prediction of
  ``workload_benefit``. Otherwise ordering falls back to the rule score.
* A candidate whose columns are a left prefix of a higher-ranked recommendation
  on the same table is dropped as superseded, which avoids recommending
  redundant indexes together.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.services.rule_scorer import RuleWeights, load_rule_weights, priority_for
from backend.services.stats import optional_float


logger = logging.getLogger(__name__)

ML_MIN_PREDICTED_BENEFIT = 0.02

RECOMMENDATION_FIELDS = [
    "candidate_id",
    "table",
    "columns",
    "score",
    "priority",
    "recommended_sql",
    "reason",
    "rank",
    "rule_score",
    "ranking_method",
    "ml_predicted_benefit",
    "ml_model_version",
    "ml_label_source",
    "ml_status",
    "ranking_reason",
    "explanation",
    "positive_evidence",
    "costs",
    "drawbacks",
    "unavailable_metrics",
    "supporting_query_ids",
    "supporting_observations",
    "generation_rules",
    "index_name",
    "estimated_index_bytes",
    "storage_source",
    "write_ratio",
    "existing_prefix_index",
    "superseded",
]


def _read_candidates(path: str | Path) -> List[Dict[str, str]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Candidate file not found: {csv_path}")
    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _columns(row: Dict[str, Any]) -> List[str]:
    return [c.strip().lower() for c in str(row.get("columns", "")).split(",") if c.strip()]


def _is_left_prefix(shorter: List[str], longer: List[str]) -> bool:
    return len(shorter) < len(longer) and longer[: len(shorter)] == shorter


def _load_state(use_ml: bool, ranker: Any) -> Any:
    if ranker is not None or not use_ml:
        return ranker
    try:
        from backend.services.ml.predictor import load_ranker

        return load_ranker()
    except Exception as exc:  # never let ML problems break rule-based recommendations
        logger.warning("ML ranker could not be loaded: %s", exc)
        return None


def generate_recommendations(
    candidate_path: str | Path,
    output_path: str | Path,
    use_ml: bool = True,
    ranker: Any = None,
    weights: Optional[RuleWeights] = None,
    ml_min_benefit: float = ML_MIN_PREDICTED_BENEFIT,
) -> List[Dict[str, object]]:
    """Rank scored candidates and write ``recommendations.csv``."""

    from backend.config import settings

    rule_weights = weights or load_rule_weights(settings.rule_weights_path or None)
    candidates: List[Dict[str, Any]] = []
    for row in _read_candidates(candidate_path):
        rule_score = optional_float(row.get("rule_score"))
        if rule_score is None:
            rule_score = optional_float(row.get("score"))
        if rule_score is None or rule_score <= 0:
            continue
        candidates.append({**row, "rule_score": round(rule_score, 3), "priority": priority_for(rule_score, rule_weights)})

    state = _load_state(use_ml, ranker)
    ml_used = bool(state is not None and getattr(state, "usable", False))
    ml_status = getattr(state, "reason", "ML ranking disabled.") if state is not None else "ML ranking disabled."
    if ml_used and candidates:
        from backend.services.ml.predictor import predict_benefit

        try:
            for row, prediction in zip(candidates, predict_benefit(state, candidates)):
                row["ml_predicted_benefit"] = prediction
        except Exception as exc:
            logger.warning("ML prediction failed; falling back to rule ranking: %s", exc)
            ml_used = False
            ml_status = f"Prediction failed ({type(exc).__name__}); using rule-based ranking."
            for row in candidates:
                row.pop("ml_predicted_benefit", None)

    if ml_used:
        candidates.sort(key=lambda r: (-r["ml_predicted_benefit"], -r["rule_score"], r.get("candidate_id", "")))
    else:
        candidates.sort(key=lambda r: (-r["rule_score"], r.get("candidate_id", "")))

    kept: List[Dict[str, Any]] = []
    for row in candidates:
        actionable = row["priority"] in {"high", "medium"} or (
            ml_used and row.get("ml_predicted_benefit", 0) >= ml_min_benefit
        )
        if not actionable:
            continue
        superseded_by = next(
            (
                other["candidate_id"] for other in kept
                if other.get("table", "").lower() == row.get("table", "").lower()
                and _is_left_prefix(_columns(row), _columns(other))
            ),
            None,
        )
        if superseded_by:
            logger.info("Dropping %s: superseded by %s", row.get("candidate_id"), superseded_by)
            continue
        kept.append(row)

    version = state.version if ml_used else None
    label_source = (getattr(state, "metadata", None) or {}).get("label_source") if ml_used else None
    if ml_used:
        ranking_reason = (
            f"Ordered by predicted workload_benefit from model {version} "
            f"({label_source or 'unknown'} labels); the rule score breaks ties."
        )
        logger.info("Ranking %s candidates with model %s (%s labels)", len(candidates), version, label_source)
    else:
        ranking_reason = f"Rule-based ranking. {ml_status}"
        logger.info("Ranking %s candidates by rule score: %s", len(candidates), ml_status)
    recommendations: List[Dict[str, Any]] = []
    for rank, row in enumerate(kept, start=1):
        predicted = row.get("ml_predicted_benefit") if ml_used else None
        explanation = row.get("reason", "")
        if row.get("explanation"):
            explanation = f"{row['explanation']} {explanation}".strip()
        if predicted is not None:
            explanation += (
                f" Model {version} predicts a {predicted * 100:.1f}% reduction in median-weighted workload "
                "latency. This is a model prediction, not a measured result."
            )
        recommendations.append({
            "candidate_id": row.get("candidate_id", ""),
            "table": row.get("table", ""),
            "columns": row.get("columns", ""),
            "score": row["rule_score"],
            "priority": row["priority"],
            "recommended_sql": row.get("recommended_action") or row.get("recommended_sql", ""),
            "reason": row.get("reason", ""),
            "rank": rank,
            "rule_score": row["rule_score"],
            "ranking_method": "ml" if ml_used else "rule",
            "ml_predicted_benefit": predicted,
            "ml_model_version": version,
            "ml_label_source": label_source,
            "ml_status": ml_status,
            "ranking_reason": ranking_reason,
            "explanation": explanation,
            "positive_evidence": row.get("positive_evidence") or "[]",
            "costs": row.get("costs") or "[]",
            "drawbacks": row.get("drawbacks") or "[]",
            "unavailable_metrics": row.get("unavailable_metrics") or "[]",
            "supporting_query_ids": row.get("supporting_query_ids", ""),
            "supporting_observations": row.get("supporting_observations") or row.get("frequency", ""),
            "generation_rules": row.get("generation_rules", ""),
            "index_name": row.get("index_name", ""),
            "estimated_index_bytes": row.get("estimated_index_bytes", ""),
            "storage_source": row.get("storage_source", ""),
            "write_ratio": row.get("write_ratio", ""),
            "existing_prefix_index": row.get("existing_prefix_index", ""),
            "superseded": "false",
        })

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECOMMENDATION_FIELDS)
        writer.writeheader()
        for item in recommendations:
            writer.writerow({key: ("" if item.get(key) is None else item.get(key)) for key in RECOMMENDATION_FIELDS})
    return recommendations


def ranking_summary(recommendations: List[Dict[str, Any]]) -> Dict[str, Any]:
    method = recommendations[0]["ranking_method"] if recommendations else None
    return {
        "count": len(recommendations),
        "ranking_method": method,
        "ml_status": recommendations[0]["ml_status"] if recommendations else None,
        "ranking_reason": recommendations[0].get("ranking_reason") if recommendations else None,
        "ml_model_version": recommendations[0].get("ml_model_version") if recommendations else None,
        "ml_label_source": recommendations[0].get("ml_label_source") if recommendations else None,
        "priorities": {p: sum(1 for r in recommendations if r["priority"] == p) for p in ("high", "medium", "low")},
    }


if __name__ == "__main__":
    generate_recommendations("data/index_candidates.csv", "data/recommendations.csv")
