"""Application configuration loaded from the project environment file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    """Runtime settings shared by the API and database services."""

    db_host: str = os.getenv("DB_HOST", "127.0.0.1")
    db_port: int = int(os.getenv("DB_PORT", "3306"))
    db_name: str = os.getenv("DB_NAME", "queryscale")
    db_test_name: str = os.getenv("DB_TEST_NAME", "queryscale_test")
    db_user: str = os.getenv("DB_USER", "root")
    db_password: str = os.getenv("DB_PASSWORD", "")
    db_charset: str = os.getenv("DB_CHARSET", "utf8mb4")
    query_log_path: str = os.getenv("QUERY_LOG_PATH", "data/query_logs.csv")
    recommendation_path: str = os.getenv("RECOMMENDATION_PATH", "data/recommendations.csv")
    benchmark_result_path: str = os.getenv("BENCHMARK_RESULT_PATH", "data/benchmark_results.csv")

    # ML pipeline, analysis artifacts, and job state. All paths are relative to ROOT_DIR unless absolute.
    query_feature_path: str = os.getenv("QUERY_FEATURE_PATH", "data/query_features.csv")
    feature_path: str = os.getenv("FEATURE_PATH", "data/candidate_features.csv")
    candidate_path: str = os.getenv("CANDIDATE_PATH", "data/index_candidates.csv")
    schema_metadata_path: str = os.getenv("SCHEMA_METADATA_PATH", "data/schema_metadata.json")
    benchmark_history_path: str = os.getenv("BENCHMARK_HISTORY_PATH", "data/benchmark_history.jsonl")
    metadata_source: str = os.getenv("METADATA_SOURCE", "auto")
    metadata_exact_stats_max_rows: int = int(os.getenv("METADATA_EXACT_STATS_MAX_ROWS", "1000000"))
    max_index_candidates: int = int(os.getenv("MAX_INDEX_CANDIDATES", "40"))
    max_candidates_per_table: int = int(os.getenv("MAX_CANDIDATES_PER_TABLE", "10"))
    max_index_columns: int = int(os.getenv("MAX_INDEX_COLUMNS", "3"))
    rule_weights_path: str = os.getenv("RULE_WEIGHTS_PATH", "")
    model_dir: str = os.getenv("MODEL_DIR", "models")
    ml_min_training_samples: int = int(os.getenv("ML_MIN_TRAINING_SAMPLES", "20"))
    ml_min_training_groups: int = int(os.getenv("ML_MIN_TRAINING_GROUPS", "5"))
    ml_allow_synthetic_model: bool = os.getenv("ML_ALLOW_SYNTHETIC_MODEL", "false").strip().lower() in {"1", "true", "yes", "on"}
    benchmark_query_timeout_ms: int = int(os.getenv("BENCHMARK_QUERY_TIMEOUT_MS", "30000"))
    benchmark_write_probe_samples: int = int(os.getenv("BENCHMARK_WRITE_PROBE_SAMPLES", "25"))
    benchmark_seed: int = int(os.getenv("BENCHMARK_SEED", "42"))
    job_database_path: str = os.getenv("JOB_DATABASE_PATH", "runtime/queryscale_jobs.sqlite3")
    job_execution_mode: str = os.getenv("JOB_EXECUTION_MODE", "thread")
    worker_poll_seconds: float = float(os.getenv("WORKER_POLL_SECONDS", "2"))
    artifact_store: str = os.getenv("ARTIFACT_STORE", "local")
    s3_artifact_bucket: str = os.getenv("S3_ARTIFACT_BUCKET", "")
    s3_artifact_prefix: str = os.getenv("S3_ARTIFACT_PREFIX", "queryscale")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_format: str = os.getenv("LOG_FORMAT", "text")

    def resolve_path(self, value: str) -> Path:
        """Resolve a configured path relative to the repository root."""

        path = Path(value)
        return path if path.is_absolute() else ROOT_DIR / path

    def mysql_config(self) -> dict[str, object]:
        return {
            "host": self.db_host,
            "port": self.db_port,
            "database": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
            "charset": self.db_charset,
            "autocommit": True,
            "raise_on_warnings": True,
        }


settings = Settings()