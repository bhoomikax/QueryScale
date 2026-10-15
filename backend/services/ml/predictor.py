"""Safe loading of the persisted ranker and prediction for index candidates.

The ranker is used only when every check passes:

* the metadata and model files exist and the SHA-256 checksum matches,
* the stored feature names equal the current ``FEATURE_COLUMNS``,
* the model beat the mean baseline on held-out data (``suitable``), and
* its labels were measured benchmarks, unless synthetic models are explicitly allowed.

Otherwise ``RankerState.usable`` is False with a human-readable reason and
callers fall back to rule-based ranking. Predictions are point estimates of
``workload_benefit``; no confidence interval is reported because the small
benchmark datasets do not justify one.

Only load model files produced by this project: joblib files are pickles and
can execute code when loaded. The checksum check guards against accidental
replacement, not against an attacker who can also rewrite the metadata file.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import joblib
import numpy as np

from backend.config import settings
from backend.services.artifact_store import sha256_file
from backend.services.ml.dataset import LABEL_MEASURED
from backend.services.ml.features import FEATURE_COLUMNS, to_feature_frame
from backend.services.ml.trainer import METADATA_FILE, MODEL_FILE, resolve_model_dir


logger = logging.getLogger(__name__)


@dataclass
class RankerState:
    available: bool
    usable: bool
    reason: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    pipeline: Any = None
    model_dir: Optional[str] = None

    @property
    def version(self) -> Optional[str]:
        return self.metadata.get("model_version")

    def public_status(self) -> Dict[str, Any]:
        metadata = self.metadata
        return {
            "available": self.available,
            "usable": self.usable,
            "reason": self.reason,
            "model_version": metadata.get("model_version"),
            "model_name": metadata.get("model_name"),
            "trained_at": metadata.get("trained_at"),
            "label_source": metadata.get("label_source"),
            "target_name": metadata.get("target_name"),
            "target_definition": metadata.get("target_definition"),
            "feature_names": metadata.get("feature_names"),
            "feature_importances": metadata.get("feature_importances"),
            "metrics": metadata.get("metrics"),
            "suitable": metadata.get("suitable"),
            "suitability_reason": metadata.get("suitability_reason"),
            "provenance": metadata.get("provenance"),
            "sklearn_version": metadata.get("sklearn_version"),
        }


def load_ranker(model_dir: str | Path | None = None, allow_synthetic: bool | None = None) -> RankerState:
    directory = resolve_model_dir(model_dir)
    allow = settings.ml_allow_synthetic_model if allow_synthetic is None else allow_synthetic
    metadata_path = directory / METADATA_FILE
    model_path = directory / MODEL_FILE

    if not metadata_path.exists() or not model_path.exists():
        return RankerState(False, False, "No trained model is available; using rule-based ranking.", model_dir=str(directory))
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return RankerState(False, False, f"Model metadata is unreadable: {exc}", model_dir=str(directory))

    def unusable(reason: str) -> RankerState:
        return RankerState(True, False, reason, metadata=metadata, model_dir=str(directory))

    expected_sha = metadata.get("artifact_sha256")
    if not expected_sha or sha256_file(model_path) != expected_sha:
        return unusable("Model file checksum does not match its metadata; refusing to load it.")
    if metadata.get("feature_names") != FEATURE_COLUMNS:
        return unusable("Model feature schema does not match the current feature pipeline; retrain the model.")
    if metadata.get("label_source") != LABEL_MEASURED and not allow:
        return unusable("Model was trained on synthetic demonstration labels and is not used for ranking.")
    if not metadata.get("suitable"):
        return unusable(metadata.get("suitability_reason") or "Model did not beat the baseline on held-out data.")

    try:
        bundle = joblib.load(model_path)
    except Exception as exc:  # corrupted file or incompatible library version
        return unusable(f"Model file could not be loaded: {type(exc).__name__}: {exc}")
    if not isinstance(bundle, dict) or bundle.get("feature_names") != FEATURE_COLUMNS or "pipeline" not in bundle:
        return unusable("Model bundle is missing its pipeline or has a different feature schema.")
    return RankerState(True, True, "Model loaded and validated.", metadata=metadata, pipeline=bundle["pipeline"], model_dir=str(directory))


def predict_benefit(state: RankerState, rows: Sequence[Dict[str, Any]]) -> List[float]:
    """Predict workload_benefit for candidate feature rows. Requires a usable state."""

    if not state.usable or state.pipeline is None:
        raise RuntimeError(f"Ranker is not usable: {state.reason}")
    if not rows:
        return []
    frame = to_feature_frame(rows)
    predictions = state.pipeline.predict(frame[FEATURE_COLUMNS])
    return [round(float(value), 6) for value in np.asarray(predictions, dtype=float)]
