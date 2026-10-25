"""Train, evaluate, and persist the index-benefit regressor.

Protocol
--------
1. Validate the frame against the exact 21-feature schema and drop rows whose
   target is missing or non-finite (counted in the report, never imputed).
2. Refuse to train when there are fewer than ``ML_MIN_TRAINING_SAMPLES`` rows or
   ``ML_MIN_TRAINING_GROUPS`` distinct candidates. The evaluation report still
   records why and, when possible, a preliminary rule-baseline correlation.
3. Split by candidate (``GroupShuffleSplit``) so repeated benchmarks of the same
   index never appear in both train and test.
4. Select a model by grouped cross-validated MAE on the training split among
   ridge, decision tree, random forest, and gradient boosting. A mean predictor
   (``DummyRegressor``, fitted on the training split only) is the baseline. Ties
   break on the model name, so selection is deterministic.
5. Report MAE/RMSE on the held-out split for every model, and compare rule-based
   versus ML ranking with NDCG@k, Precision@k and Spearman within dataset groups.
   The held-out split is never used to choose the model.
6. Mark the model *suitable* only if its held-out MAE beats the baseline.
7. Refit the selected model on all rows and write the pipeline, metadata and
   report to a staging directory, re-load the staged bundle to prove it round-trips,
   and only then replace the active model. An unsuitable model is kept under
   ``models/rejected/<version>/`` and the previously active model stays in place.
   An interrupted or failed run therefore never leaves a half-written model marked
   valid, and a retrain that produces nothing usable never removes the last good one.

A per-directory lock file prevents two training runs (API job and CLI, for
example) from writing the same artifacts at once.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

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
from backend.services.ml.dataset import LABEL_MEASURED, TARGET_DEFINITION, TARGET_NAME, dataset_fingerprint, label_summary
from backend.services.ml.evaluation import compare_rankings, regression_metrics, spearman
from backend.services.ml.features import (
    FEATURE_COLUMNS,
    FEATURE_DESCRIPTIONS,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SOURCES,
    FeatureSchemaError,
    build_imputer,
    validate_feature_frame,
)


logger = logging.getLogger(__name__)

MODEL_FILE = "index_ranker.joblib"
METADATA_FILE = "index_ranker.json"
EVALUATION_FILE = "evaluation.json"
ARCHIVE_DIR = "archive"
REJECTED_DIR = "rejected"
TRAINING_LOCK_FILE = ".training.lock"
TRAINING_LOCK_MAX_AGE_SECONDS = 2 * 60 * 60
BASELINE_MODEL = "dummy_mean"
SELECTION_CRITERION = "lowest grouped cross-validated MAE on the training split; ties broken by model name"
SUITABILITY_CRITERION = "held-out MAE strictly lower than the mean-predictor baseline fitted on the training split"

_PROCESS_LOCK = threading.Lock()


class TrainingInProgress(RuntimeError):
    """Another training run holds the lock for this model directory."""


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


@contextmanager
def training_lock(output_dir: Path) -> Iterator[None]:
    """One training run per model directory, across threads and processes."""

    if not _PROCESS_LOCK.acquire(blocking=False):
        raise TrainingInProgress("Another training run is already in progress in this process.")
    path = output_dir / TRAINING_LOCK_FILE
    try:
        if path.exists() and time.time() - path.stat().st_mtime > TRAINING_LOCK_MAX_AGE_SECONDS:
            logger.warning("Removing stale training lock %s", path)
            path.unlink(missing_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise TrainingInProgress(f"Another training run holds {path}.") from exc
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()} started={datetime.now(timezone.utc).isoformat()}")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)
    finally:
        _PROCESS_LOCK.release()


def _archive_label(output_dir: Path) -> str:
    label = None
    for path, key in ((output_dir / METADATA_FILE, "model_version"), (output_dir / EVALUATION_FILE, "generated_at")):
        if path.exists():
            try:
                label = json.loads(path.read_text(encoding="utf-8")).get(key)
            except ValueError:
                label = None
            if label:
                break
    return str(label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")).replace(":", "").replace("-", "")


def _archive_target(output_dir: Path, label: str) -> Path:
    target = output_dir / ARCHIVE_DIR / label
    suffix = 1
    while target.exists():
        suffix += 1
        target = output_dir / ARCHIVE_DIR / f"{label}-{suffix}"
    return target


def archive_existing_artifacts(output_dir: Path, names: tuple[str, ...] = (MODEL_FILE, METADATA_FILE, EVALUATION_FILE)) -> Optional[Path]:
    """Move existing artifacts into ``archive/<version>/``. Nothing is deleted.

    An earlier model stays inspectable and can be restored by moving the files
    back. Called only once a replacement has been fully validated, or, for the
    evaluation report alone, when a new report is about to be written.
    """

    existing = [output_dir / name for name in names if (output_dir / name).exists()]
    if not existing:
        return None
    target = _archive_target(output_dir, _archive_label(output_dir))
    target.mkdir(parents=True)
    for path in existing:
        shutil.move(str(path), str(target / path.name))
    logger.info("Archived previous artifacts %s to %s", [p.name for p in existing], target)
    return target


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    os.replace(temporary, path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _feature_importances(pipeline: Pipeline) -> Optional[Dict[str, float]]:
    model = pipeline.named_steps["model"]
    values = getattr(model, "feature_importances_", None)
    if values is None:
        return None
    names = pipeline.named_steps["impute"].get_feature_names_out(FEATURE_COLUMNS)
    pairs = sorted(zip(names, values), key=lambda item: -item[1])
    return {str(name): round(float(value), 6) for name, value in pairs if value > 0}


def _environment() -> Dict[str, str]:
    return {
        "python": platform.python_version(),
        "sklearn": sklearn.__version__,
        "numpy": np.__version__,
        "pandas": pd.__version__,
        "joblib": joblib.__version__,
    }


def _sample_counts(frame: pd.DataFrame, provenance: Dict[str, Any]) -> Dict[str, int]:
    if "label_source" in frame.columns and len(frame):
        counts = frame["label_source"].value_counts()
        measured = int(counts.get(LABEL_MEASURED, 0))
        return {"measured": measured, "synthetic": int(len(frame) - measured)}
    counts = provenance.get("sample_counts") or {}
    return {"measured": int(counts.get("measured", 0)), "synthetic": int(counts.get("synthetic", 0))}


def prepare_training_frame(frame: pd.DataFrame) -> tuple[pd.DataFrame, Dict[str, int]]:
    """Validate the feature schema and drop rows without a finite target. Never imputes a label."""

    required = {TARGET_NAME, "group"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise FeatureSchemaError(f"Training frame is missing required columns {missing}")
    if len(frame) == 0:
        return frame.copy(), {"non_finite_target": 0, "missing_group": 0}
    features = validate_feature_frame(frame[FEATURE_COLUMNS], context="training features")
    prepared = frame.copy()
    prepared[FEATURE_COLUMNS] = features
    target = pd.to_numeric(prepared[TARGET_NAME], errors="coerce").astype(float)
    finite = np.isfinite(target.to_numpy())
    has_group = prepared["group"].notna() & (prepared["group"].astype(str) != "")
    keep = finite & has_group.to_numpy()
    dropped = {"non_finite_target": int((~finite).sum()), "missing_group": int((finite & ~has_group.to_numpy()).sum())}
    prepared = prepared.loc[keep].reset_index(drop=True)
    prepared[TARGET_NAME] = target[keep].to_numpy()
    return prepared, dropped


def _stage_artifacts(staging: Path, pipeline: Pipeline, metadata: Dict[str, Any], version: str) -> Dict[str, Any]:
    """Write model and metadata to ``staging`` and prove the bundle loads and predicts."""

    staging.mkdir(parents=True, exist_ok=True)
    model_path = staging / MODEL_FILE
    joblib.dump({"pipeline": pipeline, "feature_names": FEATURE_COLUMNS, "model_version": version,
                 "feature_schema_version": FEATURE_SCHEMA_VERSION}, model_path)
    metadata = dict(metadata, artifact_file=MODEL_FILE, artifact_sha256=sha256_file(model_path))
    bundle = joblib.load(model_path)
    if bundle.get("feature_names") != FEATURE_COLUMNS or bundle.get("model_version") != version:
        raise RuntimeError("Staged model bundle did not round-trip; refusing to publish it.")
    probe = pd.DataFrame([[np.nan] * len(FEATURE_COLUMNS)], columns=FEATURE_COLUMNS)
    bundle["pipeline"].predict(probe)
    _write_json(staging / METADATA_FILE, metadata)
    return metadata


def _move_into_place(staging: Path, output_dir: Path) -> None:
    for name in (MODEL_FILE, METADATA_FILE):
        os.replace(staging / name, output_dir / name)
    shutil.rmtree(staging, ignore_errors=True)


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
    with training_lock(output_dir):
        return _train_locked(frame, provenance, output_dir, random_state, min_samples, min_groups, test_size, k, publish)


def _train_locked(
    frame: pd.DataFrame,
    provenance: Dict[str, Any],
    output_dir: Path,
    random_state: int,
    min_samples: int | None,
    min_groups: int | None,
    test_size: float,
    k: int,
    publish: bool,
) -> Dict[str, Any]:
    min_samples = settings.ml_min_training_samples if min_samples is None else min_samples
    min_groups = settings.ml_min_training_groups if min_groups is None else min_groups
    trained_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    previous = _read_json(output_dir / METADATA_FILE) or {}
    previous_version = previous.get("model_version")

    frame, dropped = prepare_training_frame(frame)
    n_samples = int(len(frame))
    n_groups = int(frame["group"].nunique()) if n_samples else 0
    label_source = provenance.get("label_source")
    report: Dict[str, Any] = {
        "generated_at": trained_at,
        "target_name": TARGET_NAME,
        "target_definition": TARGET_DEFINITION,
        "provenance": provenance,
        "label_source": label_source,
        "samples": n_samples,
        "groups": n_groups,
        "dropped_rows": dropped,
        "sample_counts": _sample_counts(frame, provenance),
        "label_summary": label_summary(frame),
        "minimums": {"samples": min_samples, "groups": min_groups},
        "feature_names": FEATURE_COLUMNS,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "random_state": random_state,
        "training_config": {
            "test_size": test_size,
            "split": "GroupShuffleSplit by candidate (one split)",
            "cross_validation": "GroupKFold on the training split, up to 5 folds",
            "selection_criterion": SELECTION_CRITERION,
            "suitability_criterion": SUITABILITY_CRITERION,
            "ranking_k": k,
            "random_state": random_state,
        },
        "environment": _environment(),
        "active_model_version_before": previous_version,
        "active_model_version": previous_version,
        "model_replaced": False,
        "archived_previous": None,
    }

    if n_samples < min_samples or n_groups < min_groups:
        report["status"] = "insufficient_data"
        report["limitations"] = [
            f"{n_samples} labeled samples across {n_groups} candidates; at least {min_samples} samples "
            f"and {min_groups} candidates are required for a defensible held-out evaluation.",
            "No model was trained. "
            + (f"The previously trained model {previous_version} stays active." if previous_version
               else "Recommendations continue to use the rule-based ranking."),
        ]
        if n_samples >= 3:
            report["preliminary_rule_baseline"] = {
                "spearman_rule_score_vs_label": spearman(frame[TARGET_NAME], frame["rule_score"]),
                "note": "Preliminary: computed on all available labels, no holdout.",
            }
        archive_existing_artifacts(output_dir, names=(EVALUATION_FILE,))
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
    assert not set(g_train) & set(groups.iloc[test_index]), "candidate leaked across the split"

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
        f"Held-out MAE {selected_mae:.4f} does not beat the mean baseline {baseline_mae:.4f}; "
        + (f"the previously trained model {previous_version} stays active." if previous_version
           else "rule-based ranking stays active.")
    )

    test_frame = frame.iloc[test_index].copy()
    test_frame["ml_prediction"] = fitted[selected].predict(x_test)
    ranking = compare_rankings(test_frame, TARGET_NAME, k=k)

    final_pipeline = clone(models[selected]).fit(features, target)
    fingerprint = dataset_fingerprint(frame)
    version = f"v{trained_at.replace('-', '').replace(':', '')}-{fingerprint}"

    limitations = []
    if label_source != LABEL_MEASURED:
        limitations.append("Labels are synthetic demonstration data. Metrics do not reflect real recommendation quality.")
    if len(test_index) < 10:
        limitations.append(f"Held-out set has only {len(test_index)} samples; metrics are preliminary and high-variance.")
    if ranking["groups_evaluated"] == 0:
        limitations.append("No dataset group in the test split had two or more candidates, so ranking metrics are unavailable.")
    limitations.append("Benchmarks run on a single local MySQL instance; buffer-pool caching and concurrent load are not modeled.")

    report.update({
        "status": "trained",
        "model_version": version,
        "model_id": version,
        "selected_model": selected,
        "selection_basis": selection_basis,
        "baseline_model": BASELINE_MODEL,
        "baseline_fitted_on": "training split only",
        "train_samples": int(len(train_index)),
        "test_samples": int(len(test_index)),
        "train_groups": int(g_train.nunique()),
        "test_groups": int(groups.iloc[test_index].nunique()),
        "cv_folds": cv_folds,
        "cv_mae": cv_results,
        "holdout_metrics": holdout,
        "suitable": suitable,
        "suitability_reason": suitability_reason,
        "ranking_comparison": ranking,
        "limitations": limitations,
        "dataset_fingerprint": fingerprint,
    })

    metadata = {
        "model_version": version,
        "model_id": version,
        "model_name": selected,
        "trained_at": trained_at,
        "feature_names": FEATURE_COLUMNS,
        "feature_descriptions": FEATURE_DESCRIPTIONS,
        "feature_sources": FEATURE_SOURCES,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "target_name": TARGET_NAME,
        "target_definition": TARGET_DEFINITION,
        "label_source": label_source,
        "provenance": provenance,
        "dataset_fingerprint": fingerprint,
        "samples": n_samples,
        "groups": n_groups,
        "sample_counts": report["sample_counts"],
        "training_config": report["training_config"],
        "selection_basis": selection_basis,
        "metrics": {
            "holdout": holdout,
            "cv_mae": cv_results,
            "baseline_model": BASELINE_MODEL,
            "baseline_mae": baseline_mae,
            "selected_mae": selected_mae,
            "ranking_comparison": {key: ranking[key] for key in ("k", "groups_evaluated", "rule", "ml", "conclusion")},
            "train_samples": report["train_samples"],
            "test_samples": report["test_samples"],
        },
        "suitable": suitable,
        "suitability_reason": suitability_reason,
        "refit_on_all_data": True,
        "feature_importances": _feature_importances(final_pipeline),
        "sklearn_version": sklearn.__version__,
        "environment": report["environment"],
    }

    staging = output_dir / f".staging-{version}"
    try:
        metadata = _stage_artifacts(staging, final_pipeline, metadata, version)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    if suitable:
        archived = archive_existing_artifacts(output_dir)
        _move_into_place(staging, output_dir)
        report.update({
            "model_replaced": True,
            "active_model_version": version,
            "archived_previous": str(archived) if archived else None,
        })
    else:
        rejected = output_dir / REJECTED_DIR / version
        rejected.parent.mkdir(parents=True, exist_ok=True)
        if rejected.exists():
            shutil.rmtree(rejected)
        shutil.move(str(staging), str(rejected))
        archive_existing_artifacts(output_dir, names=(EVALUATION_FILE,))
        report["rejected_artifacts"] = str(rejected)
        report["limitations"].insert(0, "The new model was not suitable and was not activated; see rejected_artifacts.")
    _write_json(output_dir / EVALUATION_FILE, report)

    if publish and suitable:
        try:
            uploaded = publish_artifacts(
                "models", version, [output_dir / MODEL_FILE, output_dir / METADATA_FILE, output_dir / EVALUATION_FILE]
            )
            if uploaded:
                report["remote_artifacts"] = uploaded
                _write_json(output_dir / EVALUATION_FILE, report)
        except Exception as exc:
            report.setdefault("warnings", []).append(f"Remote artifact upload failed: {exc}")
            _write_json(output_dir / EVALUATION_FILE, report)

    logger.info("Trained %s model %s (suitable=%s, replaced=%s)", selected, version, suitable, report["model_replaced"])
    return report
