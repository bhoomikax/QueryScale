#!/usr/bin/env python3
"""Leave-one-candidate-out evaluation of the index-benefit models on measured labels.

The trainer holds out 25% of candidates once. With a dozen candidates that is only
three held-out candidates, which is too few to support a general claim. This script
re-uses the same measured records and the same candidate models but evaluates each
model by leaving out one candidate at a time: every candidate receives a prediction
from a model that never saw any of its benchmark runs. All runs of the held-out
candidate are scored together, so repeated measurements never leak across the split.

It does not train or persist a production model and it does not create new labels.
Output: ``models/evaluation_logo.json`` (or ``--output``).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.model_selection import LeaveOneGroupOut


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.config import settings
from backend.services.ml.dataset import TARGET_NAME, build_measured_dataset, load_benchmark_history
from backend.services.ml.evaluation import ndcg_at_k, precision_at_k, regression_metrics, spearman
from backend.services.ml.features import FEATURE_COLUMNS
from backend.services.ml.trainer import BASELINE_MODEL, candidate_models


def evaluate(frame: pd.DataFrame, random_state: int = 42, k: int = 5) -> dict:
    features = frame[FEATURE_COLUMNS].astype(float)
    target = frame[TARGET_NAME].astype(float)
    groups = frame["group"].astype(str)
    splitter = LeaveOneGroupOut()
    results: dict = {}
    for name, pipeline in candidate_models(random_state).items():
        predictions = pd.Series(np.nan, index=frame.index)
        for train_index, test_index in splitter.split(features, target, groups):
            model = clone(pipeline).fit(features.iloc[train_index], target.iloc[train_index])
            predictions.iloc[test_index] = model.predict(features.iloc[test_index])
        # One out-of-fold prediction per run; per-candidate view averages the repeats.
        per_candidate = pd.DataFrame({"label": target, "prediction": predictions, "group": groups}).groupby("group").mean()
        results[name] = {
            "out_of_fold_rows": regression_metrics(target, predictions),
            "per_candidate": {
                **regression_metrics(per_candidate["label"], per_candidate["prediction"]),
                "spearman": spearman(per_candidate["label"], per_candidate["prediction"]),
                "ndcg_at_k": ndcg_at_k(per_candidate["label"], per_candidate["prediction"], k),
                "precision_at_k": precision_at_k(per_candidate["label"], per_candidate["prediction"], k),
            },
            "predictions_per_candidate": {
                str(group): {"label_mean": round(float(row["label"]), 6), "prediction_mean": round(float(row["prediction"]), 6)}
                for group, row in per_candidate.iterrows()
            },
        }
    rule = frame.groupby("group").agg(label=(TARGET_NAME, "mean"), rule_score=("rule_score", "mean"))
    results["rule_score_ranking"] = {
        "spearman": spearman(rule["label"], rule["rule_score"]),
        "ndcg_at_k": ndcg_at_k(rule["label"], rule["rule_score"], k),
        "precision_at_k": precision_at_k(rule["label"], rule["rule_score"], k),
    }
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--history", default=None, help="Benchmark history file (default: configured path).")
    parser.add_argument("--output", default=None, help="Report path (default: MODEL_DIR/evaluation_logo.json).")
    parser.add_argument("--strict-dataset", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)

    history_path = Path(args.history) if args.history else settings.resolve_path(settings.benchmark_history_path)
    frame, provenance = build_measured_dataset(load_benchmark_history(history_path), strict_dataset=args.strict_dataset)
    n_groups = int(frame["group"].nunique()) if len(frame) else 0
    report = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "method": "leave-one-candidate-out; every run of the held-out candidate is predicted by a model fitted on the other candidates",
        "samples": int(len(frame)),
        "groups": n_groups,
        "k": args.k,
        "relevance_threshold": 0.05,
        "baseline_model": BASELINE_MODEL,
        "provenance": provenance,
        "feature_names": FEATURE_COLUMNS,
        "random_state": args.seed,
        "note": "Diagnostic only. No model is trained or saved by this script; the production model is selected by scripts/train_model.py.",
    }
    if n_groups < 3:
        report["status"] = "insufficient_data"
        report["limitations"] = [f"{n_groups} candidates; leave-one-out needs at least 3."]
    else:
        report["status"] = "evaluated"
        report["results"] = evaluate(frame, args.seed, args.k)
        report["limitations"] = [
            f"{n_groups} candidates from one workload and one dataset; a candidate held out here still shares the workload with the training candidates.",
            "Per-candidate metrics average the repeated runs of each candidate, so ranking metrics are over the candidates, not the runs.",
        ]
    output = Path(args.output) if args.output else settings.resolve_path(settings.model_dir) / "evaluation_logo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("status", "samples", "groups")}, indent=2))
    if report["status"] == "evaluated":
        rows = report["results"]
        print(f"{'model':20s} {'OOF MAE (rows)':>15s} {'MAE (cand.)':>12s} {'Spearman':>9s} {'NDCG@k':>7s} {'P@k':>5s}")
        for name in candidate_models().keys():
            r = rows[name]
            pc = r["per_candidate"]
            print(f"{name:20s} {r['out_of_fold_rows']['mae']:15.4f} {pc['mae']:12.4f} {str(pc['spearman']):>9s} {str(pc['ndcg_at_k']):>7s} {str(pc['precision_at_k']):>5s}")
        rr = rows["rule_score_ranking"]
        print(f"{'rule_score':20s} {'-':>15s} {'-':>12s} {str(rr['spearman']):>9s} {str(rr['ndcg_at_k']):>7s} {str(rr['precision_at_k']):>5s}")
    print(f"Report: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
