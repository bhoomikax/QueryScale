"""Entry points used by API jobs and CLI scripts: analysis, training, and benchmarking."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from backend.config import settings
from backend.services.pipeline import run_pipeline
from backend.services.recommender import generate_recommendations, ranking_summary
from backend.services.schema_metadata import load_metadata


logger = logging.getLogger(__name__)
DEMO_MODEL_SUBDIR = "demo"


class AnalysisError(RuntimeError):
    """Analysis could not do useful work. ``code`` is reported in the job's error record."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _csv_row_count(path) -> int:
    import csv

    if not path.exists():
        return 0
    with path.open("r", newline="", encoding="utf-8") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def run_analysis(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract features, generate and score candidates, and rank recommendations.

    Raises ``AnalysisError`` (so the job is FAILED, not COMPLETED) when the query
    log is missing or empty or when no schema metadata is available. A completed
    analysis that produced no recommendations carries ``warnings`` explaining why.
    """

    params = params or {}
    log_path = settings.resolve_path(settings.query_log_path)
    if not log_path.exists():
        raise AnalysisError("query_log_missing", f"Query log not found: {log_path}. Run scripts/workload.py first.")
    log_rows = _csv_row_count(log_path)
    if log_rows == 0:
        raise AnalysisError("query_log_empty", f"Query log {log_path} has no rows. Run scripts/workload.py first.")

    metadata = load_metadata(source=params.get("metadata_source") or settings.metadata_source)
    if not metadata.tables:
        raise AnalysisError(
            "schema_metadata_empty",
            "No schema metadata is available (live database, snapshot, and schema.sql all yielded no tables); "
            "candidates cannot be validated. " + "; ".join(metadata.warnings),
        )

    feature_path = settings.resolve_path(settings.feature_path)
    candidate_path = settings.resolve_path(settings.candidate_path)
    recommendations = run_pipeline(
        log_path,
        feature_path,
        candidate_path,
        settings.resolve_path(settings.recommendation_path),
        metadata=metadata,
        query_feature_path=settings.resolve_path(settings.query_feature_path),
        use_ml=bool(params.get("use_ml", True)),
    )
    generated = _csv_row_count(feature_path)
    scored = _csv_row_count(candidate_path)
    warnings = list(metadata.warnings)
    if generated == 0:
        warnings.append(
            f"No index candidates were generated from {log_rows} log rows. Check that the logged queries reference "
            f"tables in the schema ({', '.join(sorted(metadata.tables)) or 'none'})."
        )
    elif scored == 0:
        warnings.append(f"All {generated} candidates are covered by existing indexes or scored zero; nothing to recommend.")
    elif not recommendations:
        warnings.append(f"{scored} candidates were scored but none reached MEDIUM priority or a usable ML prediction.")

    summary = ranking_summary(recommendations)
    summary.update({
        "query_log_rows": log_rows,
        "candidates_generated": generated,
        "candidates_scored": scored,
        "metadata_source": metadata.source,
        "metadata_collected_at": metadata.collected_at,
        "metadata_tables": len(metadata.tables),
        "metadata_warnings": metadata.warnings,
        "warnings": warnings,
        "top_candidate_id": recommendations[0]["candidate_id"] if recommendations else None,
    })
    logger.info(
        "Analysis completed: %s recommendations from %s scored candidates (%s ranking, metadata: %s)",
        summary["count"], scored, summary["ranking_method"] or "n/a", metadata.source,
    )
    for warning in warnings:
        logger.warning("Analysis: %s", warning)
    return summary


def run_training(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Train from measured benchmark history, or from synthetic demo data into ``models/demo``."""

    from backend.services.ml.dataset import build_measured_dataset, load_benchmark_history, make_synthetic_dataset
    from backend.services.ml.trainer import resolve_model_dir, train_and_evaluate

    params = params or {}
    if params.get("synthetic_demo"):
        frame, provenance = make_synthetic_dataset(seed=int(params.get("seed", 42)))
        model_dir = resolve_model_dir() / DEMO_MODEL_SUBDIR
    else:
        history = load_benchmark_history(settings.resolve_path(settings.benchmark_history_path))
        frame, provenance = build_measured_dataset(history, strict_dataset=bool(params.get("strict_dataset", False)))
        model_dir = resolve_model_dir()

    report = train_and_evaluate(frame, provenance, model_dir=model_dir, random_state=int(params.get("seed", 42)))
    result = {
        "status": report["status"],
        "label_source": report.get("label_source"),
        "samples": report["samples"],
        "groups": report["groups"],
        "model_version": report.get("model_version"),
        "selected_model": report.get("selected_model"),
        "suitable": report.get("suitable"),
        "model_dir": str(model_dir),
        "limitations": report.get("limitations", []),
    }

    if params.get("rerank", True) and not params.get("synthetic_demo"):
        candidate_path = settings.resolve_path(settings.candidate_path)
        if candidate_path.exists():
            recommendations = generate_recommendations(candidate_path, settings.resolve_path(settings.recommendation_path))
            result["reranked"] = ranking_summary(recommendations)
    return result


def run_benchmark_job(params: Dict[str, Any]) -> Dict[str, Any]:
    """Benchmark one recommendation and return a compact summary for job status."""

    from backend.services.benchmark import run_benchmark_detailed

    supporting: Optional[Sequence[str]] = params.get("supporting_query_ids") or None
    record = run_benchmark_detailed(
        candidate_id=params["candidate_id"],
        table=params["table"],
        columns=params["columns"],
        iterations=int(params.get("iterations", 100)),
        output_path=settings.resolve_path(settings.benchmark_result_path),
        repeats=int(params.get("repeats", 1)),
        supporting_query_ids=supporting,
    )
    return {
        "run_id": record["run_id"],
        "candidate_id": record["candidate_id"],
        "summary": record["summary"],
        "index_size_bytes": record.get("index_size_bytes"),
        "cleanup": record.get("cleanup"),
    }


# Job kind -> handler. The API (thread mode) and the worker (queue mode) use the same functions.
JOB_HANDLERS = {
    "analysis": run_analysis,
    "benchmark": run_benchmark_job,
    "training": run_training,
}
