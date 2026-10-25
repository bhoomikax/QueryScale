"""Durable store of ML training samples (SQLite, standard library only).

Why a store in addition to ``data/benchmark_history.jsonl``
------------------------------------------------------------
The history file is the raw, append-only audit log of every benchmark run
(timings, EXPLAIN plans, write probes). It is the right place for forensics,
but it is a poor training set: nothing in it says which runs are valid labels,
why others are excluded, which feature-schema version produced the snapshot,
or whether a record has already been counted. This module keeps one structured
row per ``(run_id, candidate_id)`` with the complete 21-feature vector, the
target, its provenance, the underlying before/after measurements, and a
validation verdict, in ``ML_DATASET_PATH`` (default ``data/ml_dataset.sqlite3``).

Idempotency: ``sample_id`` is derived from ``run_id`` and ``candidate_id``, so
re-ingesting the same history file never duplicates a row, while every distinct
benchmark run of the same candidate stays a distinct observation.

Durability: the file lives under ``data/`` (bind-mounted in Docker) and is never
touched by schema initialization, seeding, cloning, or benchmark cleanup, which
only act on MySQL. Only ``SampleStore.reset(confirm=True)`` or deleting the file
removes samples.

Synthetic rows: the demonstration pipeline may persist its formula-generated
examples here so the dashboard can show them, but they carry
``label_source = 'synthetic_demo'`` and every measured-mode query filters them
out. They never count toward measured eligibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import sqlite3
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from backend.services.ml.dataset import BENCHMARK_PROTOCOL_VERSION, LABEL_MEASURED, LABEL_SYNTHETIC, TARGET_NAME
from backend.services.ml.features import FEATURE_COLUMNS, FEATURE_SCHEMA_VERSION, candidate_feature_vector
from backend.services.stats import optional_float


logger = logging.getLogger(__name__)

DATASET_VERSION = 1
VALID, EXCLUDED = "valid", "excluded"

SAMPLE_COLUMNS: List[str] = [
    "sample_id",
    "run_id",
    "candidate_id",
    "candidate_fingerprint",
    "table_name",
    "columns",
    "index_definition",
    "test_database",
    "server_version",
    "dataset_hash",
    "workload_signature",
    "supporting_query_ids",
    "affected_queries",
    "features",
    "raw_snapshot",
    "target",
    "label_source",
    "benchmark_status",
    "protocol_version",
    "baseline_workload_median_ms",
    "indexed_workload_median_ms",
    "before_mean_ms",
    "after_mean_ms",
    "execution_count",
    "rule_score",
    "cleanup_status",
    "validation_status",
    "exclusion_reason",
    "feature_schema_version",
    "dataset_version",
    "measured_at",
    "recorded_at",
]

_JSON_COLUMNS = {"supporting_query_ids", "affected_queries", "features", "raw_snapshot"}

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS training_samples (
    sample_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_fingerprint TEXT NOT NULL,
    table_name TEXT,
    columns TEXT,
    index_definition TEXT,
    test_database TEXT,
    server_version TEXT,
    dataset_hash TEXT,
    workload_signature TEXT,
    supporting_query_ids TEXT,
    affected_queries TEXT,
    features TEXT NOT NULL,
    raw_snapshot TEXT,
    target REAL,
    label_source TEXT NOT NULL,
    benchmark_status TEXT,
    protocol_version INTEGER,
    baseline_workload_median_ms REAL,
    indexed_workload_median_ms REAL,
    before_mean_ms REAL,
    after_mean_ms REAL,
    execution_count INTEGER,
    rule_score REAL,
    cleanup_status TEXT,
    validation_status TEXT NOT NULL,
    exclusion_reason TEXT,
    feature_schema_version INTEGER NOT NULL,
    dataset_version INTEGER NOT NULL,
    measured_at TEXT,
    recorded_at TEXT NOT NULL,
    UNIQUE (run_id, candidate_id)
);
CREATE INDEX IF NOT EXISTS idx_samples_source_status ON training_samples (label_source, validation_status);
CREATE INDEX IF NOT EXISTS idx_samples_candidate ON training_samples (candidate_id);
CREATE TABLE IF NOT EXISTS store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sample_id_for(run_id: str, candidate_id: str) -> str:
    return hashlib.sha256(f"{run_id}\x1f{candidate_id}".encode("utf-8")).hexdigest()[:24]


def candidate_fingerprint(table: Any, columns: Any) -> str:
    """Stable identity of an index definition: table and ordered column list, case-insensitive."""

    if isinstance(columns, (list, tuple)):
        names = [str(c).strip().lower() for c in columns if str(c).strip()]
    else:
        names = [c.strip().lower() for c in str(columns or "").replace(";", ",").split(",") if c.strip()]
    payload = f"{str(table or '').strip().lower()}({','.join(names)})"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _json_features(vector: Dict[str, float]) -> str:
    return json.dumps({name: (None if (value is None or not math.isfinite(value)) else value) for name, value in vector.items()})


def _features_from_json(text: str) -> Dict[str, float]:
    raw = json.loads(text) if text else {}
    return {name: (np.nan if raw.get(name) is None else float(raw[name])) for name in FEATURE_COLUMNS}


def build_sample(record: Dict[str, Any]) -> Dict[str, Any]:
    """Derive one training sample from a benchmark run record (never raises for bad records).

    Validation rules (``validation_status``/``exclusion_reason``):
    benchmark_not_completed, protocol_version_mismatch, missing_feature_snapshot,
    missing_label (absent or non-finite ``workload_benefit``), no_baseline_latency.
    Excluded rows are kept so the count and the reasons are reportable.
    """

    summary = record.get("summary") or {}
    snapshot = record.get("candidate_features") or {}
    columns = record.get("columns") or snapshot.get("columns") or ""
    table = record.get("table") or snapshot.get("table") or ""
    run_id = str(record.get("run_id") or "")
    candidate_id = str(record.get("candidate_id") or snapshot.get("candidate_id") or "")
    status = record.get("status")
    label_source = record.get("label_source") or LABEL_MEASURED
    target = optional_float(summary.get(TARGET_NAME))
    baseline = optional_float(summary.get("baseline_workload_median_ms"))

    exclusion: Optional[str] = None
    if status != "COMPLETED":
        exclusion = "benchmark_not_completed"
    elif record.get("protocol_version") != BENCHMARK_PROTOCOL_VERSION:
        exclusion = "protocol_version_mismatch"
    elif not snapshot:
        exclusion = "missing_feature_snapshot"
    elif target is None or not math.isfinite(target):
        exclusion = "missing_label"
    elif baseline is not None and baseline <= 0:
        exclusion = "no_baseline_latency"

    features = candidate_feature_vector(snapshot) if isinstance(snapshot, dict) else {name: np.nan for name in FEATURE_COLUMNS}
    rule_score = optional_float(record.get("rule_score"))
    if rule_score is None and snapshot:
        rule_score = optional_float(snapshot.get("rule_score")) or optional_float(snapshot.get("score"))
    cleanup = record.get("cleanup") or {}
    return {
        "sample_id": sample_id_for(run_id, candidate_id),
        "run_id": run_id,
        "candidate_id": candidate_id,
        "candidate_fingerprint": candidate_fingerprint(table, columns),
        "table_name": table,
        "columns": ",".join(columns) if isinstance(columns, (list, tuple)) else str(columns),
        "index_definition": record.get("index_sql") or snapshot.get("recommended_action") or None,
        "test_database": record.get("test_database"),
        "server_version": record.get("server_version"),
        "dataset_hash": (record.get("dataset_signature") or {}).get("hash"),
        "workload_signature": (record.get("workload") or {}).get("signature"),
        "supporting_query_ids": json.dumps(list(record.get("supporting_query_ids") or [])),
        "affected_queries": json.dumps(list(summary.get("affected_queries") or [])),
        "features": _json_features(features),
        "raw_snapshot": json.dumps(snapshot, default=str) if snapshot else None,
        "target": target if target is not None and math.isfinite(target) else None,
        "label_source": label_source,
        "benchmark_status": status,
        "protocol_version": record.get("protocol_version"),
        "baseline_workload_median_ms": baseline,
        "indexed_workload_median_ms": optional_float(summary.get("indexed_workload_median_ms")),
        "before_mean_ms": optional_float(summary.get("before_ms")),
        "after_mean_ms": optional_float(summary.get("after_ms")),
        "execution_count": int(summary["execution_count"]) if summary.get("execution_count") is not None else None,
        "rule_score": rule_score,
        "cleanup_status": cleanup.get("status") if isinstance(cleanup, dict) else None,
        "validation_status": VALID if exclusion is None else EXCLUDED,
        "exclusion_reason": exclusion,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "dataset_version": DATASET_VERSION,
        "measured_at": record.get("finished_at") or record.get("started_at"),
        "recorded_at": _now(),
    }


def synthetic_samples(frame: pd.DataFrame, seed: int) -> List[Dict[str, Any]]:
    """Samples for an explicitly synthetic frame from ``dataset.make_synthetic_dataset``."""

    samples = []
    for _, row in frame.iterrows():
        features = {name: float(row[name]) for name in FEATURE_COLUMNS}
        run_id, candidate_id = str(row["run_id"]), str(row["candidate_id"])
        samples.append({
            "sample_id": sample_id_for(run_id, candidate_id),
            "run_id": run_id,
            "candidate_id": candidate_id,
            "candidate_fingerprint": candidate_fingerprint("Synthetic", candidate_id),
            "table_name": "Synthetic",
            "columns": None,
            "index_definition": None,
            "test_database": None,
            "server_version": None,
            "dataset_hash": f"synthetic-seed-{seed}",
            "workload_signature": f"synthetic-seed-{seed}",
            "supporting_query_ids": "[]",
            "affected_queries": "[]",
            "features": _json_features(features),
            "raw_snapshot": None,
            "target": float(row[TARGET_NAME]),
            "label_source": LABEL_SYNTHETIC,
            "benchmark_status": None,
            "protocol_version": None,
            "baseline_workload_median_ms": None,
            "indexed_workload_median_ms": None,
            "before_mean_ms": None,
            "after_mean_ms": None,
            "execution_count": None,
            "rule_score": optional_float(row.get("rule_score")),
            "cleanup_status": None,
            "validation_status": VALID,
            "exclusion_reason": None,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "dataset_version": DATASET_VERSION,
            "measured_at": None,
            "recorded_at": _now(),
        })
    return samples


class SampleStore:
    """SQLite-backed training sample store. Safe for concurrent readers and one writer per process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO store_meta (key, value) VALUES (?, ?)", ("created_at", _now())
            )
            connection.execute(
                "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", ("dataset_version", str(DATASET_VERSION))
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    # -- writes ---------------------------------------------------------------

    def upsert(self, samples: Iterable[Dict[str, Any]]) -> Dict[str, int]:
        """Insert new samples; re-validate and update rows whose (run_id, candidate_id) already exist."""

        inserted = updated = 0
        placeholders = ", ".join("?" for _ in SAMPLE_COLUMNS)
        update_cols = [c for c in SAMPLE_COLUMNS if c not in {"sample_id", "run_id", "candidate_id", "recorded_at"}]
        assignments = ", ".join(f"{c} = excluded.{c}" for c in update_cols)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for sample in samples:
                    if not sample.get("run_id") or not sample.get("candidate_id"):
                        continue
                    existed = connection.execute(
                        "SELECT 1 FROM training_samples WHERE sample_id = ?", (sample["sample_id"],)
                    ).fetchone()
                    connection.execute(
                        f"INSERT INTO training_samples ({', '.join(SAMPLE_COLUMNS)}) VALUES ({placeholders}) "
                        f"ON CONFLICT(sample_id) DO UPDATE SET {assignments}",
                        tuple(sample.get(column) for column in SAMPLE_COLUMNS),
                    )
                    if existed:
                        updated += 1
                    else:
                        inserted += 1
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {"inserted": inserted, "updated": updated}

    def record_benchmark(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """Persist the sample for one benchmark run record (completed or failed) and return it."""

        sample = build_sample(record)
        self.upsert([sample])
        return sample

    def record_synthetic(self, frame: pd.DataFrame, seed: int) -> Dict[str, int]:
        return self.upsert(synthetic_samples(frame, seed))

    def sync_history(self, history_path: str | Path) -> Dict[str, Any]:
        """Idempotently ingest every record of a benchmark history file."""

        path = Path(history_path)
        counts = {"records": 0, "inserted": 0, "updated": 0, "unparseable": 0, "history_path": str(path), "exists": path.exists()}
        if not path.exists():
            return counts
        samples: List[Dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    counts["unparseable"] += 1
                    continue
                counts["records"] += 1
                samples.append(build_sample(record))
        result = self.upsert(samples)
        counts.update(result)
        with self._lock, self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", ("last_sync_at", _now()))
        return counts

    def reset(self, confirm: bool = False) -> int:
        """Delete every sample. Requires ``confirm=True``; returns the number of rows removed."""

        if not confirm:
            raise ValueError("SampleStore.reset requires confirm=True; this deletes all persisted training samples.")
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM training_samples")
            connection.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)", ("last_reset_at", _now()))
            return cursor.rowcount

    # -- reads ----------------------------------------------------------------

    def samples(
        self,
        label_source: Optional[str] = None,
        valid_only: bool = False,
        with_features: bool = True,
    ) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if label_source:
            clauses.append("label_source = ?")
            params.append(label_source)
        if valid_only:
            clauses.append("validation_status = ?")
            params.append(VALID)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM training_samples{where} ORDER BY measured_at, recorded_at, sample_id", params
            ).fetchall()
        result = []
        for row in rows:
            sample = dict(row)
            for column in _JSON_COLUMNS:
                if column == "features":
                    sample[column] = _features_from_json(sample[column]) if with_features else None
                elif sample.get(column):
                    try:
                        sample[column] = json.loads(sample[column])
                    except ValueError:
                        pass
            result.append(sample)
        return result

    def meta(self) -> Dict[str, str]:
        with self._connect() as connection:
            return {row["key"]: row["value"] for row in connection.execute("SELECT key, value FROM store_meta")}

    def status(self) -> Dict[str, Any]:
        """Counts by provenance and validity, plus exclusion reasons and the time span of the samples."""

        with self._connect() as connection:
            rows = connection.execute(
                "SELECT label_source, validation_status, exclusion_reason, candidate_id, measured_at, recorded_at, "
                "workload_signature FROM training_samples"
            ).fetchall()
        by_source: Dict[str, Dict[str, Any]] = {}
        # Valid measured rows grouped by workload signature: only one signature can be trained on at a time.
        signatures: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if row["label_source"] != LABEL_MEASURED or row["validation_status"] != VALID:
                continue
            entry = signatures.setdefault(str(row["workload_signature"]), {"samples": 0, "candidates": set()})
            entry["samples"] += 1
            entry["candidates"].add(row["candidate_id"])
        signature_summary = {
            key: {"samples": value["samples"], "candidates": len(value["candidates"])} for key, value in signatures.items()
        }
        comparable = max(signature_summary, key=lambda k: (signature_summary[k]["samples"], k)) if signature_summary else None
        for source in (LABEL_MEASURED, LABEL_SYNTHETIC):
            subset = [r for r in rows if r["label_source"] == source]
            valid = [r for r in subset if r["validation_status"] == VALID]
            by_source[source] = {
                "samples": len(subset),
                "valid_samples": len(valid),
                "excluded_samples": len(subset) - len(valid),
                "distinct_candidates": len({r["candidate_id"] for r in valid}),
                "exclusion_reasons": dict(Counter(r["exclusion_reason"] for r in subset if r["exclusion_reason"])),
            }
        timestamps = [r["measured_at"] or r["recorded_at"] for r in rows if (r["measured_at"] or r["recorded_at"])]
        return {
            "path": str(self.path),
            "dataset_version": DATASET_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "total_samples": len(rows),
            "by_label_source": by_source,
            "measured_workload_signatures": signature_summary,
            "comparable_workload_signature": comparable,
            "comparable_measured": signature_summary.get(comparable, {"samples": 0, "candidates": 0}),
            "first_sample_at": min(timestamps) if timestamps else None,
            "last_sample_at": max(timestamps) if timestamps else None,
            "meta": self.meta(),
        }

    def training_frame(
        self,
        label_source: str = LABEL_MEASURED,
        strict_dataset: bool = False,
    ) -> Tuple[pd.DataFrame, Dict[str, Any]]:
        """Valid samples of one provenance as a training frame plus provenance details.

        Only rows with ``validation_status = valid`` and the requested
        ``label_source`` are used. Measured rows are further restricted to the
        most common workload signature (and, with ``strict_dataset``, the most
        common dataset hash) so only comparable benchmarks are combined.
        """

        samples = self.samples(label_source=label_source, valid_only=True)
        excluded: Counter = Counter()
        all_rows = self.samples(label_source=label_source, valid_only=False, with_features=False)
        for row in all_rows:
            if row["validation_status"] != VALID:
                excluded[row["exclusion_reason"] or "excluded"] += 1

        selected_workload = selected_dataset = None
        if label_source == LABEL_MEASURED and samples:
            counts = Counter(s["workload_signature"] for s in samples)
            selected_workload = counts.most_common(1)[0][0]
            comparable = [s for s in samples if s["workload_signature"] == selected_workload]
            excluded["different_workload"] += len(samples) - len(comparable)
            samples = comparable
            if strict_dataset and samples:
                counts = Counter(s["dataset_hash"] for s in samples)
                selected_dataset = counts.most_common(1)[0][0]
                comparable = [s for s in samples if s["dataset_hash"] == selected_dataset]
                excluded["different_dataset"] += len(samples) - len(comparable)
                samples = comparable

        frame = _frame_from_samples(samples, label_source)
        provenance = {
            "label_source": label_source,
            "source": "sample_store",
            "store_path": str(self.path),
            "dataset_version": DATASET_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "strict_dataset": strict_dataset,
            "workload_signature": selected_workload,
            "dataset_signature": selected_dataset,
            "records_total": len(all_rows),
            "records_used": len(samples),
            "records_excluded": dict(excluded),
            "run_ids": [s["run_id"] for s in samples],
            "datasets": sorted({str(s.get("dataset_hash")) for s in samples}),
            "server_versions": sorted({str(s.get("server_version")) for s in samples}),
            "sample_counts": {
                "measured": len(samples) if label_source == LABEL_MEASURED else 0,
                "synthetic": len(samples) if label_source == LABEL_SYNTHETIC else 0,
            },
        }
        return frame, provenance


def _frame_from_samples(samples: Sequence[Dict[str, Any]], label_source: str) -> pd.DataFrame:
    columns = FEATURE_COLUMNS + [TARGET_NAME, "group", "candidate_id", "run_id", "comparison_group", "rule_score", "label_source"]
    if not samples:
        return pd.DataFrame(columns=columns)
    records = []
    for sample in samples:
        row = dict(sample["features"])
        row.update({
            TARGET_NAME: float(sample["target"]),
            "group": sample["candidate_id"],
            "candidate_id": sample["candidate_id"],
            "run_id": sample["run_id"],
            "comparison_group": sample.get("dataset_hash") or "unknown",
            "rule_score": sample.get("rule_score") if sample.get("rule_score") is not None else np.nan,
            "label_source": label_source,
        })
        records.append(row)
    frame = pd.DataFrame(records, columns=columns)
    frame[FEATURE_COLUMNS] = frame[FEATURE_COLUMNS].astype(float)
    frame[TARGET_NAME] = frame[TARGET_NAME].astype(float)
    return frame


def measured_eligibility(status: Dict[str, Any], min_samples: int, min_groups: int) -> Dict[str, Any]:
    """Whether the *comparable* measured samples satisfy the training minimums.

    Only valid ``measured_benchmark`` rows that share the most common workload signature
    count, because that is exactly the set the trainer will use. Rows from other
    protocols (different iterations, repeats, or seed) and synthetic rows never count.
    """

    measured = status["by_label_source"][LABEL_MEASURED]
    comparable = status.get("comparable_measured") or {"samples": measured["valid_samples"], "candidates": measured["distinct_candidates"]}
    samples, groups = comparable["samples"], comparable["candidates"]
    other_workloads = measured["valid_samples"] - samples
    eligible = samples >= min_samples and groups >= min_groups
    note = None
    if not eligible:
        note = (
            f"{samples} comparable measured samples across {groups} candidates; at least {min_samples} samples and "
            f"{min_groups} candidates are required. Synthetic samples do not count."
        )
        if other_workloads:
            note += f" {other_workloads} valid measured sample(s) use a different benchmark protocol and cannot be combined."
    return {
        "eligible": eligible,
        "status": "eligible" if eligible else "insufficient_measured_data",
        "measured_valid_samples": samples,
        "measured_candidates": groups,
        "measured_valid_samples_all_protocols": measured["valid_samples"],
        "measured_other_protocol_samples": other_workloads,
        "comparable_workload_signature": status.get("comparable_workload_signature"),
        "measured_excluded_samples": measured["excluded_samples"],
        "synthetic_samples": status["by_label_source"][LABEL_SYNTHETIC]["samples"],
        "required": {"samples": min_samples, "groups": min_groups},
        "note": note,
    }


def open_store(path: str | Path | None = None) -> SampleStore:
    from backend.config import settings

    return SampleStore(Path(path) if path else settings.resolve_path(settings.ml_dataset_path))
