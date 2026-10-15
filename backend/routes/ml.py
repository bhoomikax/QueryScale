"""ML model metadata, evaluation results, and training jobs."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.services.analysis import DEMO_MODEL_SUBDIR, run_training
from backend.services.jobs import JobConflict, get_job_manager
from backend.services.ml.predictor import load_ranker
from backend.services.ml.trainer import EVALUATION_FILE, resolve_model_dir


router = APIRouter(prefix="/ml", tags=["ml"])


class TrainingRequest(BaseModel):
    synthetic_demo: bool = False
    strict_dataset: bool = False
    rerank: bool = True
    seed: int = 42


def _read_evaluation(directory) -> Optional[dict[str, Any]]:
    path = directory / EVALUATION_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


@router.get("/model")
def model_status() -> dict[str, Any]:
    """Status of the production model and of the synthetic demonstration model, if present."""

    production = load_ranker()
    demo_dir = resolve_model_dir() / DEMO_MODEL_SUBDIR
    demo = load_ranker(demo_dir, allow_synthetic=True)
    return {
        "ranking_method": "ml" if production.usable else "rule",
        "production": production.public_status(),
        "demo": {**demo.public_status(), "note": "Synthetic demonstration labels; never used to rank recommendations."}
        if demo.available else None,
    }


@router.get("/evaluation")
def model_evaluation() -> dict[str, Any]:
    """Latest evaluation reports. ``production`` uses measured labels; ``demo`` uses synthetic labels."""

    production = _read_evaluation(resolve_model_dir())
    demo = _read_evaluation(resolve_model_dir() / DEMO_MODEL_SUBDIR)
    if production is None and demo is None:
        raise HTTPException(status_code=404, detail="No model evaluation has been run yet.")
    return {"production": production, "demo": demo}


@router.post("/train", status_code=202)
def start_training(request: Optional[TrainingRequest] = None) -> JSONResponse:
    """Train in the background from measured benchmark history (or synthetic demo data when requested)."""

    params = (request or TrainingRequest()).model_dump()
    dedupe_key = json.dumps(params, sort_keys=True)
    try:
        job, created = get_job_manager().submit("training", dedupe_key, params, run_training)
    except JobConflict as exc:
        raise HTTPException(status_code=409, detail={"message": str(exc), "active_job": exc.active_job}) from exc
    return JSONResponse(status_code=202, content={**job, "created": created, "status_url": f"/api/jobs/{job['job_id']}"})
