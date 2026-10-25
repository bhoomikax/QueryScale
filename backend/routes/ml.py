"""ML model metadata, dataset status, evaluation results, and training jobs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.config import ROOT_DIR, settings
from backend.services.analysis import DEMO_MODEL_SUBDIR, run_training
from backend.services.jobs import JobConflict, get_job_manager
from backend.services.ml.dataset import LABEL_MEASURED, LABEL_SYNTHETIC, TARGET_DEFINITION, TARGET_NAME
from backend.services.ml.features import feature_schema
from backend.services.ml.predictor import load_ranker
from backend.services.ml.status import ml_status
from backend.services.ml.store import open_store
from backend.services.ml.trainer import EVALUATION_FILE, resolve_model_dir


router = APIRouter(prefix="/ml", tags=["ml"])


class TrainingRequest(BaseModel):
    synthetic_demo: bool = False
    strict_dataset: bool = False
    rerank: bool = True
    seed: int = Field(default=42, ge=0, le=2**31 - 1)


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT_DIR))
    except ValueError:
        return str(path)


def _read_evaluation(directory) -> Optional[dict[str, Any]]:
    path = directory / EVALUATION_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _comparison_table(report: Optional[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    """The five evaluation entries side by side, taken verbatim from a training report."""

    if not report or report.get("status") != "trained":
        return None
    rows = []
    for name in sorted(report.get("holdout_metrics", {})):
        holdout = report["holdout_metrics"][name]
        rows.append({
            "model": name,
            "role": "baseline" if name == report.get("baseline_model") else "candidate",
            "cv_mae": (report.get("cv_mae") or {}).get(name),
            "holdout_mae": holdout.get("mae"),
            "holdout_rmse": holdout.get("rmse"),
            "selected": name == report.get("selected_model"),
        })
    return rows


@router.get("/features")
def features() -> dict[str, Any]:
    """The authoritative 21-feature schema and the target definition."""

    return {**feature_schema(), "target": {"name": TARGET_NAME, "definition": TARGET_DEFINITION}}


@router.get("/dataset")
def dataset_status() -> dict[str, Any]:
    """Persisted training samples: measured versus synthetic, valid versus excluded, and eligibility."""

    store = open_store()
    status = store.status()
    production = load_ranker()
    history_path = settings.resolve_path(settings.benchmark_history_path)
    records = 0
    if history_path.exists():
        with history_path.open("r", encoding="utf-8") as handle:
            records = sum(1 for line in handle if line.strip())
    status["path"] = _relative(Path(status["path"]))
    return {
        "store": status,
        "eligibility": ml_status(production, status),
        "history_file": {"path": _relative(history_path), "exists": history_path.exists(), "records": records},
        "note": "Only valid measured_benchmark samples count toward training eligibility; synthetic_demo rows are "
                "kept for pipeline demonstrations and are always labelled as such.",
    }


@router.get("/dataset/samples")
def dataset_samples(
    label_source: Optional[Literal["measured_benchmark", "synthetic_demo"]] = Query(default=None),
    valid_only: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=2000),
    include_features: bool = Query(default=True),
) -> list[dict[str, Any]]:
    """Persisted samples, newest first, without the raw candidate snapshot."""

    samples = open_store().samples(label_source=label_source, valid_only=valid_only, with_features=include_features)
    for sample in samples:
        sample.pop("raw_snapshot", None)
        if include_features and isinstance(sample.get("features"), dict):
            sample["features"] = {k: (None if v != v else v) for k, v in sample["features"].items()}
    return list(reversed(samples))[:limit]


@router.post("/dataset/sync")
def dataset_sync() -> dict[str, Any]:
    """Idempotently ingest the benchmark history file into the sample store."""

    store = open_store()
    result = store.sync_history(settings.resolve_path(settings.benchmark_history_path))
    result["history_path"] = _relative(Path(result["history_path"]))
    return {"sync": result, "store": {**store.status(), "path": _relative(store.path)}}


@router.get("/model")
def model_status() -> dict[str, Any]:
    """Status of the production model, eligibility, and the synthetic demonstration model, if present."""

    production = load_ranker()
    demo_dir = resolve_model_dir() / DEMO_MODEL_SUBDIR
    demo = load_ranker(demo_dir, allow_synthetic=True)
    status = ml_status(production, open_store().status())
    return {
        "ranking_method": status["ranking_method"],
        "eligibility": status,
        "production": production.public_status(),
        "demo": {
            **demo.public_status(),
            "note": "Synthetic demonstration labels; metrics describe a formula, not MySQL. Never used to rank "
                    "recommendations unless ML_ALLOW_SYNTHETIC_MODEL is enabled.",
        } if demo.available else None,
        "label_sources": {"measured": LABEL_MEASURED, "synthetic": LABEL_SYNTHETIC},
    }


@router.get("/evaluation")
def model_evaluation() -> dict[str, Any]:
    """Latest evaluation reports. ``production`` uses measured labels; ``demo`` uses synthetic labels."""

    production = _read_evaluation(resolve_model_dir())
    demo = _read_evaluation(resolve_model_dir() / DEMO_MODEL_SUBDIR)
    if production is None and demo is None:
        raise HTTPException(status_code=404, detail="No model evaluation has been run yet.")
    return {
        "production": production,
        "demo": demo,
        "comparison": {
            "production": _comparison_table(production),
            "demo": _comparison_table(demo),
            "note": "Each row is one of the five evaluation entries. demo rows come from synthetic labels and say "
                    "nothing about real index benefit.",
        },
    }


@router.post("/train", status_code=202)
def start_training(request: Optional[TrainingRequest] = None) -> JSONResponse:
    """Train in the background from the measured sample store (or synthetic demo data when requested)."""

    params = (request or TrainingRequest()).model_dump()
    dedupe_key = json.dumps(params, sort_keys=True)
    try:
        job, created = get_job_manager().submit("training", dedupe_key, params, run_training)
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "active_job": exc.active_job}) from exc
    return JSONResponse(status_code=202, content={**job, "created": created, "status_url": f"/api/jobs/{job['job_id']}"})
