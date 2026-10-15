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


def run_analysis(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Extract features, generate and score candidates, and rank recommendations."""

    params = params or {}
    metadata = load_metadata(source=params.get("metadata_source") or settings.metadata_source)
    recommendations = run_pipeline(
        settings.resolve_path(settings.query_log_path),
        settings.resolve_path(settings.feature_path),
        settings.resolve_path(settings.candidate_path),
        settings.resolve_path(settings.recommendation_path),
        metadata=metadata,
        query_feature_path=settings.resolve_path(settings.query_feature_path),
        use_ml=bool(params.get("use_ml", True)),
    )
    summary = ranking_summary(recommendations)
    summary.update({
        "metadata_source": metadata.source,
        "metadata_collected_at": metadata.collected_at,
        "metadata_warnings": metadata.warnings,
        "top_candidate_id": recommendations[0]["candidate_id"] if recommendations else None,
    })
    logger.info("Analysis completed: %s recommendations (%s ranking)", summary["count"], summary["ranking_method"])
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
