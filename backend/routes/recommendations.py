"""Recommendation and candidate endpoints backed by the generated CSV artifacts.

The original fields (candidate_id, table, columns, score, priority,
recommended_sql, reason) keep their meaning; ``score`` is the heuristic rule
score. New fields are additive. Missing values are returned as ``null``.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Query

from backend.config import ROOT_DIR, settings
from backend.services.stats import optional_float, optional_int, parse_bool


router = APIRouter(prefix="/recommendations", tags=["recommendations"])
candidates_router = APIRouter(prefix="/candidates", tags=["candidates"])


def _configured_recommendation_path() -> Path:
    path = Path(settings.recommendation_path)
    return path if path.is_absolute() else ROOT_DIR / path


def _json_list(value: Any) -> List[Any]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return [item for item in str(value).split(";") if item]
    return parsed if isinstance(parsed, list) else [parsed]


def _split(value: Any, separator: str) -> List[str]:
    return [item.strip() for item in str(value or "").split(separator) if item.strip()]


def _provenance_label(label_source: Any) -> Any:
    return {"measured_benchmark": "measured-trained", "synthetic_demo": "synthetic-trained"}.get(label_source or "")


def _parse_recommendation(row: Dict[str, str]) -> Dict[str, Any]:
    predicted = optional_float(row.get("ml_predicted_benefit"))
    return {
        "candidate_id": row.get("candidate_id", ""),
        "table": row.get("table", ""),
        "columns": _split(row.get("columns"), ","),
        "score": optional_float(row.get("score")) or 0.0,
        "priority": row.get("priority", ""),
        "recommended_sql": row.get("recommended_sql", ""),
        "reason": row.get("reason", ""),
        "rank": optional_int(row.get("rank")),
        "rule_score": optional_float(row.get("rule_score")),
        "score_type": "heuristic_rule_score",
        "ranking_method": row.get("ranking_method") or "rule",
        "ml_predicted_benefit": predicted,
        "ml_prediction_type": "model_prediction" if predicted is not None else None,
        "ml_model_version": row.get("ml_model_version") or None,
        "ml_label_source": row.get("ml_label_source") or None,
        "ml_provenance_label": _provenance_label(row.get("ml_label_source")),
        "ml_status": row.get("ml_status") or None,
        "ranking_reason": row.get("ranking_reason") or None,
        "explanation": row.get("explanation") or row.get("reason", ""),
        "positive_evidence": _json_list(row.get("positive_evidence")),
        "costs": _json_list(row.get("costs")),
        "drawbacks": _json_list(row.get("drawbacks")),
        "unavailable_metrics": _json_list(row.get("unavailable_metrics")),
        "supporting_query_ids": _split(row.get("supporting_query_ids"), ";"),
        "supporting_observations": optional_int(row.get("supporting_observations")),
        "generation_rules": _split(row.get("generation_rules"), ";"),
        "index_name": row.get("index_name") or None,
        "estimated_index_bytes": optional_int(row.get("estimated_index_bytes")),
        "storage_source": row.get("storage_source") or "unavailable",
        "write_ratio": optional_float(row.get("write_ratio")),
        "existing_prefix_index": row.get("existing_prefix_index") or None,
    }


def read_recommendations(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read generated recommendations from the configured CSV file."""

    recommendation_path = Path(path) if path is not None else _configured_recommendation_path()
    if not recommendation_path.exists():
        raise FileNotFoundError(f"Recommendation file not found: {recommendation_path}")
    with recommendation_path.open("r", newline="", encoding="utf-8") as handle:
        return [_parse_recommendation(row) for row in csv.DictReader(handle)]


def _load_or_raise() -> list[dict[str, Any]]:
    try:
        return read_recommendations()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _query_evidence(query_ids: List[str]) -> List[Dict[str, Any]]:
    path = settings.resolve_path(settings.query_feature_path)
    if not query_ids or not path.exists():
        return []
    wanted = set(query_ids)
    evidence = []
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("query_id") in wanted:
                evidence.append({
                    "query_id": row["query_id"],
                    "normalized_sql": row.get("normalized_sql"),
                    "query_type": row.get("query_type"),
                    "execution_count": optional_int(row.get("execution_count")),
                    "avg_latency_ms": optional_float(row.get("avg_latency_ms")),
                    "median_latency_ms": optional_float(row.get("median_latency_ms")),
                    "p95_latency_ms": optional_float(row.get("p95_latency_ms")),
                    "rows_examined_avg": optional_float(row.get("rows_examined_avg")),
                    "workload_share": optional_float(row.get("workload_share")),
                    "time_share": optional_float(row.get("time_share")),
                })
    return evidence


@router.get("")
def get_recommendations(limit: int = Query(default=100, ge=1, le=1000)) -> list[dict[str, Any]]:
    """Return ranked index recommendations."""

    return _load_or_raise()[:limit]


@router.get("/{recommendation_id}")
def get_recommendation(recommendation_id: str) -> dict[str, Any]:
    """Return one recommendation with its explanation and supporting query evidence."""

    for recommendation in _load_or_raise():
        if recommendation["candidate_id"] == recommendation_id:
            return {**recommendation, "supporting_queries": _query_evidence(recommendation["supporting_query_ids"])}
    raise HTTPException(status_code=404, detail=f"Recommendation not found: {recommendation_id}")


def _parse_candidate(row: Dict[str, str], scored: bool) -> Dict[str, Any]:
    return {
        "candidate_id": row.get("candidate_id", ""),
        "table": row.get("table", ""),
        "columns": _split(row.get("columns"), ","),
        "index_name": row.get("index_name") or None,
        "index_type": row.get("index_type") or None,
        "database": row.get("database") or None,
        "rule_score": optional_float(row.get("rule_score")) if scored else 0.0,
        "priority": row.get("priority") if scored else "low",
        "existing_index": parse_bool(row.get("existing_index")),
        "covered_by": row.get("covered_by") or None,
        "existing_prefix_index": row.get("existing_prefix_index") or None,
        "generation_rules": _split(row.get("generation_rules"), ";"),
        "supporting_query_ids": _split(row.get("supporting_query_ids"), ";"),
        "supporting_observations": optional_int(row.get("supporting_observations")),
        "workload_share": optional_float(row.get("workload_share")),
        "time_share": optional_float(row.get("time_share")),
        "table_rows": optional_int(row.get("table_size")),
        "table_row_count_source": row.get("table_row_count_source") or None,
        "leading_column_cardinality": optional_int(row.get("leading_column_cardinality")),
        "cardinality_source": row.get("cardinality_source") or None,
        "write_ratio": optional_float(row.get("write_ratio")),
        "estimated_index_bytes": optional_int(row.get("estimated_index_bytes")),
        "storage_source": row.get("storage_source") or "unavailable",
        "drawbacks": _json_list(row.get("drawbacks")),
        "explanation": row.get("explanation") or None,
        "positive_evidence": _json_list(row.get("positive_evidence")) if scored else [],
        "costs": _json_list(row.get("costs")) if scored else [],
        "reason": row.get("reason") if scored else None,
    }


def _attach_predictions(candidates: List[Dict[str, Any]], raw_rows: List[Dict[str, str]]) -> None:
    """Add a live ML prediction per candidate when a validated model is usable; never fails the request."""

    for candidate in candidates:
        candidate["ml_predicted_benefit"] = None
        candidate["ml_model_version"] = None
        candidate["ml_label_source"] = None
    try:
        from backend.services.ml.predictor import load_ranker, predict_benefit

        state = load_ranker()
        if not state.usable:
            for candidate in candidates:
                candidate["ml_status"] = state.reason
            return
        predictions = predict_benefit(state, raw_rows)
        for candidate, prediction in zip(candidates, predictions):
            candidate.update({
                "ml_predicted_benefit": prediction,
                "ml_model_version": state.version,
                "ml_label_source": state.label_source,
                "ml_status": f"Prediction from model {state.version} ({state.provenance_label}); not a measured result.",
            })
    except Exception as exc:  # predictions are informational; the rule score is always present
        for candidate in candidates:
            candidate["ml_status"] = f"Prediction unavailable ({type(exc).__name__}); rule score shown."


@candidates_router.get("")
def get_candidates(
    include_covered: bool = Query(default=False),
    include_predictions: bool = Query(default=True),
) -> list[dict[str, Any]]:
    """Return scored index candidates with rule scores and, when a validated model exists, ML predictions."""

    scored_path = settings.resolve_path(settings.candidate_path)
    if not scored_path.exists():
        raise HTTPException(status_code=404, detail=f"Candidate file not found: {scored_path}")
    with scored_path.open("r", newline="", encoding="utf-8") as handle:
        raw_rows = list(csv.DictReader(handle))
    candidates = [_parse_candidate(row, scored=True) for row in raw_rows]
    if include_predictions:
        _attach_predictions(candidates, raw_rows)
    if include_covered:
        feature_path = settings.resolve_path(settings.feature_path)
        if feature_path.exists():
            with feature_path.open("r", newline="", encoding="utf-8") as handle:
                candidates.extend(
                    _parse_candidate(row, scored=False)
                    for row in csv.DictReader(handle)
                    if parse_bool(row.get("existing_index"))
                )
    return candidates
