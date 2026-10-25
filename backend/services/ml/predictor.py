"""Safe loading of the persisted ranker and prediction for index candidates.

The ranker is used only when every check passes:

* the metadata and model files exist and the SHA-256 checksum matches,
* the stored feature names and feature-schema version equal the current ones,
* the model beat the mean baseline on held-out data (``suitable``),
* its labels were measured benchmarks, unless synthetic models are explicitly allowed,
* the bundle loads and can predict a probe row with the current library versions.

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
import pandas as pd

from backend.config import settings
from backend.services.artifact_store import sha256_file
from backend.services.ml.dataset import LABEL_MEASURED, LABEL_SYNTHETIC
from backend.services.ml.features import FEATURE_COLUMNS, FEATURE_SCHEMA_VERSION, FeatureSchemaError, to_feature_frame
from backend.services.ml.trainer import METADATA_FILE, MODEL_FILE, resolve_model_dir


logger = logging.getLogger(__name__)

PROVENANCE_LABELS = {LABEL_MEASURED: "measured-trained", LABEL_SYNTHETIC: "synthetic-trained"}


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

    @property
    def label_source(self) -> Optional[str]:
        return self.metadata.get("label_source")

    @property
    def provenance_label(self) -> Optional[str]:
        return PROVENANCE_LABELS.get(self.label_source or "")

    def public_status(self) -> Dict[str, Any]:
        metadata = self.metadata
        metrics = metadata.get("metrics") or {}
        return {
            "available": self.available,
            "usable": self.usable,
            "reason": self.reason,
            "model_id": metadata.get("model_id") or metadata.get("model_version"),
            "model_version": metadata.get("model_version"),
            "model_name": metadata.get("model_name"),
            "trained_at": metadata.get("trained_at"),
            "label_source": metadata.get("label_source"),
            "provenance_label": self.provenance_label,
            "validation_status": "usable" if self.usable else ("rejected" if self.available else "absent"),
            "target_name": metadata.get("target_name"),
            "target_definition": metadata.get("target_definition"),
            "feature_names": metadata.get("feature_names"),
            "feature_schema_version": metadata.get("feature_schema_version"),
            "feature_importances": metadata.get("feature_importances"),
            "samples": metadata.get("samples"),
            "groups": metadata.get("groups"),
            "sample_counts": metadata.get("sample_counts"),
            "training_config": metadata.get("training_config"),
            "selection_basis": metadata.get("selection_basis"),
            "metrics": metrics,
            "suitable": metadata.get("suitable"),
            "suitability_reason": metadata.get("suitability_reason"),
            "provenance": metadata.get("provenance"),
            "sklearn_version": metadata.get("sklearn_version"),
            "environment": metadata.get("environment"),
            "artifact_sha256": metadata.get("artifact_sha256"),
            "model_dir": self.model_dir,
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
    if not isinstance(metadata, dict):
        return RankerState(False, False, "Model metadata is not a JSON object; refusing to use it.", model_dir=str(directory))

    def unusable(reason: str) -> RankerState:
        return RankerState(True, False, reason, metadata=metadata, model_dir=str(directory))

    expected_sha = metadata.get("artifact_sha256")
    if not expected_sha or sha256_file(model_path) != expected_sha:
        return unusable("Model file checksum does not match its metadata; refusing to load it.")
    if metadata.get("feature_names") != FEATURE_COLUMNS:
        return unusable("Model feature schema does not match the current feature pipeline; retrain the model.")
    if metadata.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        return unusable(
            f"Model was built with feature schema version {metadata.get('feature_schema_version')!r}, "
            f"current is {FEATURE_SCHEMA_VERSION}; retrain the model."
        )
    label_source = metadata.get("label_source")
    if label_source not in {LABEL_MEASURED, LABEL_SYNTHETIC}:
        return unusable(f"Model provenance is missing or unknown (label_source={label_source!r}); refusing to use it.")
    if label_source != LABEL_MEASURED and not allow:
        return unusable("Model was trained on synthetic demonstration labels and is not used for ranking.")
    if not metadata.get("suitable"):
        return unusable(metadata.get("suitability_reason") or "Model did not beat the baseline on held-out data.")

    try:
        bundle = joblib.load(model_path)
    except Exception as exc:  # corrupted file or incompatible library version
        return unusable(f"Model file could not be loaded: {type(exc).__name__}: {exc}")
    if not isinstance(bundle, dict) or bundle.get("feature_names") != FEATURE_COLUMNS or "pipeline" not in bundle:
        return unusable("Model bundle is missing its pipeline or has a different feature schema.")
    pipeline = bundle["pipeline"]
    try:
        probe = pd.DataFrame([[np.nan] * len(FEATURE_COLUMNS)], columns=FEATURE_COLUMNS)
        probe_prediction = np.asarray(pipeline.predict(probe), dtype=float)
        if probe_prediction.shape != (1,) or not np.isfinite(probe_prediction).all():
            return unusable("Model produced a non-finite or malformed probe prediction; refusing to use it.")
    except Exception as exc:
        return unusable(f"Model cannot predict with the installed libraries: {type(exc).__name__}: {exc}")
    return RankerState(True, True, "Model loaded and validated.", metadata=metadata, pipeline=pipeline, model_dir=str(directory))


def predict_benefit(state: RankerState, rows: Sequence[Dict[str, Any]]) -> List[float]:
    """Predict workload_benefit for candidate feature rows. Requires a usable state.

    Raises ``RuntimeError`` when the ranker is not usable and
    ``FeatureSchemaError`` when the rows cannot be turned into the exact
    21-feature frame. Callers that must never fail (the recommender) catch both
    and fall back to rule ranking.
    """

    if not state.usable or state.pipeline is None:
        raise RuntimeError(f"Ranker is not usable: {state.reason}")
    if not rows:
        return []
    frame = to_feature_frame(rows)
    if list(frame.columns) != FEATURE_COLUMNS or frame.shape[1] != len(FEATURE_COLUMNS):
        raise FeatureSchemaError("Prediction frame does not match the model's feature schema.")
    predictions = np.asarray(state.pipeline.predict(frame[FEATURE_COLUMNS]), dtype=float)
    if predictions.shape != (len(rows),):
        raise RuntimeError(f"Model returned {predictions.shape} predictions for {len(rows)} rows.")
    if not np.isfinite(predictions).all():
        raise RuntimeError("Model returned non-finite predictions.")
    return [round(float(value), 6) for value in predictions]
