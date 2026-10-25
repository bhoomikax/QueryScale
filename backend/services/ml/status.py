"""One combined view of ML eligibility: dataset minimums, model validity, and the ranking in use.

Used by ``GET /api/ml/model``, ``GET /api/ml/dataset``, ``GET /health/ready`` and the
dashboard so every surface reports the same status code and reason.

``eligibility.status`` values:

* ``measured_model_active``: a validated model trained on measured labels ranks recommendations.
* ``synthetic_model_active``: ``ML_ALLOW_SYNTHETIC_MODEL=true`` and a synthetic-trained model is in use
  (development only; the API labels every prediction as synthetic-trained).
* ``no_model``: enough measured samples exist but no model has been trained yet.
* ``model_rejected``: a model exists but failed validation (checksum, schema, suitability, provenance, load).
* ``insufficient_measured_data``: too few valid measured samples or candidates; rule ranking is authoritative.
"""

from __future__ import annotations

from typing import Any, Dict

from backend.config import settings
from backend.services.ml.dataset import LABEL_MEASURED
from backend.services.ml.predictor import RankerState
from backend.services.ml.store import measured_eligibility


def ml_status(production: RankerState, store_status: Dict[str, Any]) -> Dict[str, Any]:
    data = measured_eligibility(store_status, settings.ml_min_training_samples, settings.ml_min_training_groups)
    if production.usable and production.label_source == LABEL_MEASURED:
        status, mode = "measured_model_active", "measured"
        reason = f"Model {production.version} ({production.provenance_label}) ranks recommendations."
    elif production.usable:
        status, mode = "synthetic_model_active", "synthetic_demo"
        reason = (
            f"Model {production.version} was trained on synthetic labels and is used only because "
            "ML_ALLOW_SYNTHETIC_MODEL is enabled. Its predictions are not evidence of real index benefit."
        )
    elif production.available:
        status, mode = "model_rejected", "rule_fallback"
        reason = production.reason
    elif data["eligible"]:
        status, mode = "no_model", "rule_fallback"
        reason = "Enough measured samples exist but no model has been trained yet; run training."
    else:
        status, mode = "insufficient_measured_data", "rule_fallback"
        reason = data["note"] or "Not enough measured benchmark labels."
    return {
        "mode": mode,
        "status": status,
        "reason": reason,
        "ranking_method": "ml" if production.usable else "rule",
        "fallback": None if production.usable else "rule_based_ranking",
        "measured_data": data,
        "model": {
            "available": production.available,
            "usable": production.usable,
            "reason": production.reason,
            "model_version": production.version,
            "label_source": production.label_source,
            "provenance_label": production.provenance_label,
        },
        "synthetic_models_allowed": settings.ml_allow_synthetic_model,
    }
