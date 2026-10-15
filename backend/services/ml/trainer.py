"""Train, evaluate, and persist the index-benefit regressor.

Protocol
--------
1. Refuse to train when there are fewer than ``ML_MIN_TRAINING_SAMPLES`` rows or
   ``ML_MIN_TRAINING_GROUPS`` distinct candidates. The evaluation report still
   records why and, when possible, a preliminary rule-baseline correlation.
2. Split by candidate (``GroupShuffleSplit``) so repeated benchmarks of the same
   index never appear in both train and test.
3. Select a model by grouped cross-validated MAE on the training split among
   ridge, decision tree, random forest, and gradient boosting. A mean predictor
   (``DummyRegressor``) is the baseline.
4. Report MAE/RMSE on the held-out split for every model, and compare rule-based
   versus ML ranking with NDCG@k / Precision@k within dataset groups.
5. Mark the model *suitable* only if its held-out MAE beats the baseline.
6. Refit the selected model on all rows and persist the pipeline, feature
   names, version, timestamp, target definition, metrics, and provenance.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.base import clone
from sklearn.dummy import DummyRegressor
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold, GroupShuffleSplit, cross_val_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.tree import DecisionTreeRegressor

from backend.config import settings
from backend.services.artifact_store import publish_artifacts, sha256_file
from backend.services.ml.dataset import TARGET_DEFINITION, TARGET_NAME, dataset_fingerprint, label_summary
from backend.services.ml.evaluation import compare_rankings, regression_metrics, spearman
from backend.services.ml.features import FEATURE_COLUMNS, FEATURE_DESCRIPTIONS, FEATURE_SCHEMA_VERSION, build_imputer


logger = logging.getLogger(__name__)

MODEL_FILE = "index_ranker.joblib"
METADATA_FILE = "index_ranker.json"
EVALUATION_FILE = "evaluation.json"
BASELINE_MODEL = "dummy_mean"


def candidate_models(random_state: int = 42) -> Dict[str, Pipeline]:
    def pipeline(model: Any, scale: bool = False) -> Pipeline:
        steps = [("impute", build_imputer())]
        if scale:
            steps.append(("scale", StandardScaler()))
        steps.append(("model", model))
        return Pipeline(steps)

    return {
        BASELINE_MODEL: pipeline(DummyRegressor(strategy="mean")),
        "ridge": pipeline(Ridge(alpha=1.0), scale=True),
        "decision_tree": pipeline(DecisionTreeRegressor(max_depth=4, min_samples_leaf=3, random_state=random_state)),
        "random_forest": pipeline(RandomForestRegressor(
            n_estimators=200, max_depth=6, min_samples_leaf=2, random_state=random_state, n_jobs=1,
        )),
        "gradient_boosting": pipeline(GradientBoostingRegressor(
            n_estimators=150, max_depth=2, learning_rate=0.05, subsample=0.8, random_state=random_state,
        )),
    }


def resolve_model_dir(model_dir: str | Path | None = None) -> Path:
    return Path(model_dir) if model_dir else settings.resolve_path(settings.model_dir)


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _feature_importances(pipeline: Pipeline) -> Optional[Dict[str, float]]:
    model = pipeline.named_steps["model"]
    values = getattr(model, "feature_importances_", None)
    if values is None:
        return None
    names = pipeline.named_steps["impute"].get_feature_names_out(FEATURE_COLUMNS)
    pairs = sorted(zip(names, values), key=lambda item: -item[1])
    return {str(name): round(float(value), 6) for name, value in pairs if value > 0}


def train_and_evaluate(
    frame: pd.DataFrame,
    provenance: Dict[str, Any],
    model_dir: str | Path | None = None,
    random_state: int = 42,
    min_samples: int | None = None,
    min_groups: int | None = None,
    test_size: float = 0.25,
    k: int = 5,
    publish: bool = True,
) -> Dict[str, Any]:
    """Train and evaluate on ``frame``; persist artifacts to ``model_dir``. Returns the evaluation report."""

    output_dir = resolve_model_dir(model_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    min_samples = settings.ml_min_training_samples if min_samples is None else min_samples
    min_groups = settings.ml_min_training_groups if min_groups is None else min_groups
    trained_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    n_samples = int(len(frame))
    n_groups = int(frame["group"].nunique()) if n_samples else 0
    report: Dict[str, Any] = {
        "generated_at": trained_at,
        "target_name": TARGET_NAME,
        "target_definition": TARGET_DEFINITION,
        "provenance": provenance,
        "label_source": provenance.get("label_source"),
        "samples": n_samples,
        "groups": n_groups,
        "label_summary": label_summary(frame),
        "minimums": {"samples": min_samples, "groups": min_groups},
        "feature_names": FEATURE_COLUMNS,
        "random_state": random_state,
    }

    if n_samples < min_samples or n_groups < min_groups:
        report["status"] = "insufficient_data"
        report["limitations"] = [
            f"{n_samples} labeled samples across {n_groups} candidates; at least {min_samples} samples "
            f"and {min_groups} candidates are required for a defensible held-out evaluation.",
            "No model was trained. Recommendations continue to use the rule-based ranking.",
        ]
        if n_samples >= 3:
            report["preliminary_rule_baseline"] = {
                "spearman_rule_score_vs_label": spearman(frame[TARGET_NAME], frame["rule_score"]),
                "note": "Preliminary: computed on all available labels, no holdout.",
            }
        _write_json(output_dir / EVALUATION_FILE, report)
        return report

    features = frame[FEATURE_COLUMNS].astype(float)
    target = frame[TARGET_NAME].astype(float)
    groups = frame["group"].astype(str)

    splitter = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=random_state)
    train_index, test_index = next(splitter.split(features, target, groups))
    x_train, x_test = features.iloc[train_index], features.iloc[test_index]
    y_train, y_test = target.iloc[train_index], target.iloc[test_index]
    g_train = groups.iloc[train_index]

    models = candidate_models(random_state)
    cv_folds = min(5, int(g_train.nunique()))
    cv_results: Dict[str, Optional[float]] = {}
    holdout: Dict[str, Dict[str, float]] = {}
    fitted: Dict[str, Pipeline] = {}
    for name, pipeline in models.items():
        if cv_folds >= 2:
            scores = cross_val_score(
                clone(pipeline), x_train, y_train, groups=g_train,
                cv=GroupKFold(n_splits=cv_folds), scoring="neg_mean_absolute_error",
            )
            cv_results[name] = round(float(-np.mean(scores)), 6)
        else:
            cv_results[name] = None
        model = clone(pipeline).fit(x_train, y_train)
        fitted[name] = model
        holdout[name] = regression_metrics(y_test, model.predict(x_test))

    contenders = {name: score for name, score in cv_results.items() if name != BASELINE_MODEL and score is not None}
    if contenders:
        selected = min(contenders, key=lambda name: (contenders[name], name))
        selection_basis = f"lowest grouped {cv_folds}-fold CV MAE on the training split"
    else:
        selected = min((n for n in holdout if n != BASELINE_MODEL), key=lambda n: (holdout[n]["mae"], n))
        selection_basis = "lowest held-out MAE (too few training groups for cross-validation)"

    baseline_mae = holdout[BASELINE_MODEL]["mae"]
    selected_mae = holdout[selected]["mae"]
    suitable = selected_mae < baseline_mae
    suitability_reason = (
        f"Held-out MAE {selected_mae:.4f} beats the mean baseline {baseline_mae:.4f}."
        if suitable else
        f"Held-out MAE {selected_mae:.4f} does not beat the mean baseline {baseline_mae:.4f}; rule-based ranking stays active."
    )

    test_frame = frame.iloc[test_index].copy()
    test_frame["ml_prediction"] = fitted[selected].predict(x_test)
    ranking = compare_rankings(test_frame, TARGET_NAME, k=k)

    final_pipeline = clone(models[selected]).fit(features, target)
    fingerprint = dataset_fingerprint(frame)
    version = f"v{trained_at.replace('-', '').replace(':', '')}-{fingerprint}"

    limitations = []
    if provenance.get("label_source") != "measured_benchmark":
        limitations.append("Labels are synthetic demonstration data. Metrics do not reflect real recommendation quality.")
    if len(test_index) < 10:
        limitations.append(f"Held-out set has only {len(test_index)} samples; metrics are preliminary and high-variance.")
    if ranking["groups_evaluated"] == 0:
        limitations.append("No dataset group in the test split had two or more candidates, so ranking metrics are unavailable.")
    limitations.append("Benchmarks run on a single local MySQL instance; buffer-pool caching and concurrent load are not modeled.")

    report.update({
        "status": "trained",
        "model_version": version,
        "selected_model": selected,
        "selection_basis": selection_basis,
        "baseline_model": BASELINE_MODEL,
        "train_samples": int(len(train_index)),
        "test_samples": int(len(test_index)),
        "train_groups": int(g_train.nunique()),
        "test_groups": int(groups.iloc[test_index].nunique()),
        "cv_mae": cv_results,
        "holdout_metrics": holdout,
        "suitable": suitable,
        "suitability_reason": suitability_reason,
        "ranking_comparison": ranking,
        "limitations": limitations,
        "dataset_fingerprint": fingerprint,
    })

    model_path = output_dir / MODEL_FILE
    temporary = output_dir / (MODEL_FILE + ".tmp")
    joblib.dump({"pipeline": final_pipeline, "feature_names": FEATURE_COLUMNS, "model_version": version}, temporary)
    os.replace(temporary, model_path)

    metadata = {
        "model_version": version,
        "model_name": selected,
        "trained_at": trained_at,
        "feature_names": FEATURE_COLUMNS,
        "feature_descriptions": FEATURE_DESCRIPTIONS,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "target_name": TARGET_NAME,
        "target_definition": TARGET_DEFINITION,
        "label_source": provenance.get("label_source"),
        "provenance": provenance,
        "metrics": {
            "holdout": holdout,
            "cv_mae": cv_results,
            "baseline_model": BASELINE_MODEL,
            "train_samples": report["train_samples"],
            "test_samples": report["test_samples"],
        },
        "suitable": suitable,
        "suitability_reason": suitability_reason,
        "refit_on_all_data": True,
        "feature_importances": _feature_importances(final_pipeline),
        "sklearn_version": sklearn.__version__,
        "artifact_file": MODEL_FILE,
        "artifact_sha256": sha256_file(model_path),
    }
    _write_json(output_dir / METADATA_FILE, metadata)
    _write_json(output_dir / EVALUATION_FILE, report)

    if publish:
        try:
            uploaded = publish_artifacts(
                "models", version, [model_path, output_dir / METADATA_FILE, output_dir / EVALUATION_FILE]
            )
            if uploaded:
                report["remote_artifacts"] = uploaded
                _write_json(output_dir / EVALUATION_FILE, report)
        except Exception as exc:
            report.setdefault("warnings", []).append(f"Remote artifact upload failed: {exc}")
            _write_json(output_dir / EVALUATION_FILE, report)

    logger.info("Trained %s model %s (suitable=%s)", selected, version, suitable)
    return report
