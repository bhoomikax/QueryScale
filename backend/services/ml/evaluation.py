"""Evaluation metrics comparing ML predictions and rule-based ranking against measured labels."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd
from sklearn.metrics import mean_absolute_error, mean_squared_error

# A candidate is "relevant" for Precision@k when its measured workload benefit is at least 5%.
RELEVANCE_THRESHOLD = 0.05


def regression_metrics(y_true: Sequence[float], y_pred: Sequence[float]) -> Dict[str, float]:
    return {
        "mae": round(float(mean_absolute_error(y_true, y_pred)), 6),
        "rmse": round(float(math.sqrt(mean_squared_error(y_true, y_pred))), 6),
    }


def _order(scores: np.ndarray) -> np.ndarray:
    """Descending by score; ties broken by original position for determinism."""

    return np.lexsort((np.arange(len(scores)), -scores))


def ndcg_at_k(y_true: Sequence[float], y_score: Sequence[float], k: int) -> Optional[float]:
    """NDCG@k with linear gains max(label, 0). None when no item has positive gain."""

    gains = np.clip(np.asarray(y_true, dtype=float), 0, None)
    scores = np.asarray(y_score, dtype=float)
    if len(gains) == 0:
        return None
    k = min(k, len(gains))
    discounts = 1.0 / np.log2(np.arange(2, k + 2))
    order = _order(scores)[:k]
    ideal = np.sort(gains)[::-1][:k]
    idcg = float(np.sum(ideal * discounts))
    if idcg <= 0:
        return None
    return round(float(np.sum(gains[order] * discounts)) / idcg, 6)


def precision_at_k(
    y_true: Sequence[float], y_score: Sequence[float], k: int, threshold: float = RELEVANCE_THRESHOLD
) -> Optional[float]:
    """Share of the top-k by score whose label meets the threshold. None if n < k or nothing is relevant."""

    labels = np.asarray(y_true, dtype=float)
    scores = np.asarray(y_score, dtype=float)
    if len(labels) < k or not np.any(labels >= threshold):
        return None
    order = _order(scores)[:k]
    return round(float(np.mean(labels[order] >= threshold)), 6)


def spearman(y_true: Sequence[float], y_score: Sequence[float]) -> Optional[float]:
    if len(y_true) < 3:
        return None
    frame = pd.DataFrame({"t": list(y_true), "s": list(y_score)})
    if frame["t"].nunique() < 2 or frame["s"].nunique() < 2:
        return None
    value = frame["t"].rank().corr(frame["s"].rank())
    return None if pd.isna(value) else round(float(value), 6)


def _mean(values: List[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(float(np.mean(present)), 6) if present else None


def compare_rankings(frame: pd.DataFrame, label: str, k: int = 5) -> Dict[str, Any]:
    """Compare ``rule_score`` and ``ml_prediction`` rankings within each comparison group.

    Rankings are only meaningful among candidates benchmarked on the same
    dataset, so metrics are computed per ``comparison_group`` (groups with fewer
    than two candidates are skipped) and averaged.
    """

    per_group = []
    for group_name, group in frame.groupby("comparison_group", sort=True):
        if len(group) < 2:
            continue
        k_eff = min(k, len(group))
        per_group.append({
            "group": str(group_name),
            "size": int(len(group)),
            "k": k_eff,
            "rule_ndcg": ndcg_at_k(group[label], group["rule_score"], k_eff),
            "ml_ndcg": ndcg_at_k(group[label], group["ml_prediction"], k_eff),
            "rule_precision": precision_at_k(group[label], group["rule_score"], k_eff),
            "ml_precision": precision_at_k(group[label], group["ml_prediction"], k_eff),
            "rule_spearman": spearman(group[label], group["rule_score"]),
            "ml_spearman": spearman(group[label], group["ml_prediction"]),
        })

    summary: Dict[str, Any] = {
        "k": k,
        "relevance_threshold": RELEVANCE_THRESHOLD,
        "groups_evaluated": len(per_group),
        "rule": {
            "ndcg_at_k": _mean([g["rule_ndcg"] for g in per_group]),
            "precision_at_k": _mean([g["rule_precision"] for g in per_group]),
            "spearman": _mean([g["rule_spearman"] for g in per_group]),
        },
        "ml": {
            "ndcg_at_k": _mean([g["ml_ndcg"] for g in per_group]),
            "precision_at_k": _mean([g["ml_precision"] for g in per_group]),
            "spearman": _mean([g["ml_spearman"] for g in per_group]),
        },
        "per_group": per_group,
    }
    rule_ndcg, ml_ndcg = summary["rule"]["ndcg_at_k"], summary["ml"]["ndcg_at_k"]
    if rule_ndcg is None or ml_ndcg is None:
        summary["conclusion"] = "Insufficient ranked test data to compare rule-based and ML ranking."
    elif ml_ndcg > rule_ndcg:
        summary["conclusion"] = "ML ranking had higher NDCG@k than the rule baseline on this test split."
    elif ml_ndcg < rule_ndcg:
        summary["conclusion"] = "The rule baseline had higher NDCG@k than ML ranking on this test split."
    else:
        summary["conclusion"] = "ML and rule-based ranking had equal NDCG@k on this test split."
    return summary
