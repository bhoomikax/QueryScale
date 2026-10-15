"""Training datasets for the index-benefit model.

Target (label) definition, used identically in training and evaluation
----------------------------------------------------------------------
For one benchmark run of candidate index ``c``:

* The same seeded replay plan is executed against the isolated test database
  before and after creating ``c``. ``n_q`` is the number of executions of query
  template ``q`` in the plan.
* ``B_q`` and ``A_q`` are the **median** latencies of ``q`` before and after
  (pooled over all timed replays, warm-up excluded).
* The affected set ``S`` is the templates whose post-index EXPLAIN plan uses
  ``c``. When EXPLAIN is unavailable, ``S`` is the candidate's supporting queries.

    workload_benefit = sum_{q in S} n_q * (B_q - A_q) / sum_{q in all} n_q * B_q

It is the fraction of total median-weighted workload latency removed by the
index. Negative values mean the index made affected queries slower. Storage and
write overheads are recorded separately and are not folded into the label.

Comparability
-------------
Only runs with ``status == COMPLETED``, a numeric label, the same benchmark
protocol version, and the same workload signature are combined. Datasets of
different sizes may be combined because table size is a model feature; pass
``strict_dataset=True`` to require an identical dataset signature as well.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from backend.services.ml.features import FEATURE_COLUMNS, to_feature_frame
from backend.services.rule_scorer import score_candidate
from backend.services.stats import optional_float


TARGET_NAME = "workload_benefit"
TARGET_DEFINITION = (
    "workload_benefit = sum over affected query templates q of n_q * (median_before_q - median_after_q) "
    "divided by sum over all templates of n_q * median_before_q, from paired before/after replays of the "
    "same seeded workload on the isolated test database. Affected templates are those whose post-index "
    "EXPLAIN uses the candidate index (supporting queries when EXPLAIN is unavailable)."
)
BENCHMARK_PROTOCOL_VERSION = 2
LABEL_MEASURED = "measured_benchmark"
LABEL_SYNTHETIC = "synthetic_demo"


def load_benchmark_history(path: str | Path) -> List[Dict[str, Any]]:
    history_path = Path(path)
    if not history_path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with history_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    return records


def _frame_from_records(records: List[Dict[str, Any]], label_source: str) -> pd.DataFrame:
    snapshots = [record.get("candidate_features") or {} for record in records]
    frame = to_feature_frame(snapshots)
    frame[TARGET_NAME] = [float(record["summary"][TARGET_NAME]) for record in records]
    frame["group"] = [record.get("candidate_id", "") for record in records]
    frame["candidate_id"] = frame["group"]
    frame["run_id"] = [record.get("run_id", "") for record in records]
    frame["comparison_group"] = [
        (record.get("dataset_signature") or {}).get("hash", "unknown") for record in records
    ]
    frame["rule_score"] = [
        optional_float(record.get("rule_score")) if record.get("rule_score") is not None
        else score_candidate(record.get("candidate_features") or {})["rule_score"]
        for record in records
    ]
    frame["label_source"] = label_source
    return frame


def build_measured_dataset(
    history: List[Dict[str, Any]],
    strict_dataset: bool = False,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Build a training frame from comparable, completed benchmark runs."""

    usable: List[Dict[str, Any]] = []
    excluded = Counter()
    for record in history:
        if record.get("status") != "COMPLETED":
            excluded["not_completed"] += 1
        elif record.get("protocol_version") != BENCHMARK_PROTOCOL_VERSION:
            excluded["different_protocol_version"] += 1
        elif optional_float((record.get("summary") or {}).get(TARGET_NAME)) is None:
            excluded["missing_label"] += 1
        elif not record.get("candidate_features"):
            excluded["missing_feature_snapshot"] += 1
        else:
            usable.append(record)

    workload_counts = Counter((r.get("workload") or {}).get("signature") for r in usable)
    selected_workload = workload_counts.most_common(1)[0][0] if workload_counts else None
    comparable = [r for r in usable if (r.get("workload") or {}).get("signature") == selected_workload]
    excluded["different_workload"] += len(usable) - len(comparable)

    selected_dataset = None
    if strict_dataset and comparable:
        dataset_counts = Counter((r.get("dataset_signature") or {}).get("hash") for r in comparable)
        selected_dataset = dataset_counts.most_common(1)[0][0]
        before = len(comparable)
        comparable = [r for r in comparable if (r.get("dataset_signature") or {}).get("hash") == selected_dataset]
        excluded["different_dataset"] += before - len(comparable)

    frame = _frame_from_records(comparable, LABEL_MEASURED) if comparable else pd.DataFrame(
        columns=FEATURE_COLUMNS + [TARGET_NAME, "group", "candidate_id", "run_id", "comparison_group", "rule_score", "label_source"]
    )
    provenance = {
        "label_source": LABEL_MEASURED,
        "target_definition": TARGET_DEFINITION,
        "benchmark_protocol_version": BENCHMARK_PROTOCOL_VERSION,
        "workload_signature": selected_workload,
        "dataset_signature": selected_dataset,
        "strict_dataset": strict_dataset,
        "records_total": len(history),
        "records_used": len(comparable),
        "records_excluded": dict(excluded),
        "run_ids": [r.get("run_id") for r in comparable],
        "datasets": sorted({(r.get("dataset_signature") or {}).get("hash", "unknown") for r in comparable}),
        "server_versions": sorted({str(r.get("server_version")) for r in comparable}),
    }
    return frame, provenance


def make_synthetic_dataset(n_candidates: int = 60, runs_per_candidate: int = 3, seed: int = 42) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """Generate an explicitly synthetic dataset to exercise the pipeline.

    Labels come from a hand-written formula plus Gaussian noise. They are not
    benchmark outcomes and say nothing about real recommendation quality.
    """

    rng = np.random.default_rng(seed)
    records: List[Dict[str, Any]] = []
    for index in range(n_candidates):
        column_count = int(rng.integers(1, 4))
        eq_prefix = int(rng.integers(0, column_count + 1))
        has_range = bool(eq_prefix < column_count and rng.random() < 0.5)
        covers_order = bool(not has_range and eq_prefix < column_count and rng.random() < 0.5)
        rows = int(10 ** rng.uniform(2, 6.5))
        cardinality = max(1, int(rows * rng.uniform(0.0005, 1.0)))
        time_share = float(rng.beta(1.2, 6))
        workload_share = float(min(1.0, time_share * rng.uniform(0.5, 1.5)))
        write_ratio = float(rng.choice([0.0, 0.0, rng.uniform(0, 0.5)]))
        prefix = int(rng.integers(0, column_count)) if rng.random() < 0.3 else 0
        avg_latency = float(rng.lognormal(1.0, 1.0))
        observations = int(rng.integers(5, 200))
        features = {
            "candidate_id": f"SYN_{index:03d}",
            "table": "Synthetic",
            "columns": ",".join(f"c{i}" for i in range(column_count)),
            "column_count": column_count,
            "equality_prefix_length": eq_prefix,
            "has_range_column": has_range,
            "covers_order_by": covers_order,
            "covers_group_by": False,
            "is_covering": bool(rng.random() < 0.15),
            "supporting_query_count": int(rng.integers(1, 4)),
            "supporting_observations": observations,
            "frequency": observations,
            "workload_share": workload_share,
            "time_share": time_share,
            "average_execution_time": avg_latency,
            "median_execution_time": avg_latency * 0.9,
            "p95_execution_time": avg_latency * 2.5 if observations >= 20 else None,
            "where_usage": observations if eq_prefix or has_range else 0,
            "join_usage": observations if not eq_prefix and rng.random() < 0.3 else 0,
            "order_usage": observations if covers_order else 0,
            "table_size": rows,
            "leading_column_cardinality": cardinality,
            "leading_column_selectivity": cardinality / rows,
            "write_ratio": write_ratio,
            "existing_prefix_length": prefix,
            "estimated_index_bytes": rows * 24 / 0.69,
        }
        selectivity = np.log10(cardinality + 1) / np.log10(rows + 10)
        structure = 0.6 * (eq_prefix > 0) + 0.25 * has_range + 0.3 * covers_order + 0.1 * features["is_covering"]
        size_factor = min(1.0, np.log10(rows) / 6)
        expected = time_share * structure * selectivity * size_factor * (1 - 0.6 * prefix / column_count) - 0.05 * write_ratio
        for run in range(runs_per_candidate):
            label = float(np.clip(expected + rng.normal(0, 0.01), -0.2, 0.95))
            records.append({
                "run_id": f"SYN_RUN_{index:03d}_{run}",
                "candidate_id": features["candidate_id"],
                "candidate_features": features,
                "dataset_signature": {"hash": "synthetic"},
                "summary": {TARGET_NAME: label},
            })

    frame = _frame_from_records(records, LABEL_SYNTHETIC)
    provenance = {
        "label_source": LABEL_SYNTHETIC,
        "target_definition": TARGET_DEFINITION + " SYNTHETIC: labels generated by a formula, not measured.",
        "generator": "backend.services.ml.dataset.make_synthetic_dataset",
        "seed": seed,
        "records_used": len(records),
        "warning": "Synthetic demonstration labels. Do not use for production ranking or as evidence of model quality.",
    }
    return frame, provenance


def dataset_fingerprint(frame: pd.DataFrame) -> str:
    payload = frame[FEATURE_COLUMNS + [TARGET_NAME]].round(8).to_csv(index=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def label_summary(frame: pd.DataFrame) -> Optional[Dict[str, float]]:
    if frame.empty:
        return None
    labels = frame[TARGET_NAME].astype(float)
    return {
        "count": int(labels.count()),
        "mean": round(float(labels.mean()), 6),
        "std": round(float(labels.std(ddof=0)), 6),
        "min": round(float(labels.min()), 6),
        "max": round(float(labels.max()), 6),
    }
