"""Application configuration loaded from the project environment file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(ROOT_DIR / ".env")


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


@dataclass(frozen=True)
class Settings:
    """Runtime settings shared by the API and database services."""

    db_host: str = field(default_factory=lambda: os.getenv("DB_HOST", "127.0.0.1"))
    db_port: int = field(default_factory=lambda: int(os.getenv("DB_PORT", "3306")))
    db_name: str = field(default_factory=lambda: os.getenv("DB_NAME", "queryscale"))
    db_test_name: str = field(default_factory=lambda: os.getenv("DB_TEST_NAME", "queryscale_test"))
    db_user: str = field(default_factory=lambda: os.getenv("DB_USER", "root"))
    db_password: str = field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))
    db_test_user: str = field(default_factory=lambda: os.getenv("DB_TEST_USER", os.getenv("DB_USER", "root")))
    db_test_password: str = field(default_factory=lambda: os.getenv("DB_TEST_PASSWORD", os.getenv("DB_PASSWORD", "")))
    db_charset: str = field(default_factory=lambda: os.getenv("DB_CHARSET", "utf8mb4"))
    db_connection_timeout: int = field(default_factory=lambda: int(os.getenv("DB_CONNECTION_TIMEOUT", "10")))
    db_read_timeout: int = field(default_factory=lambda: int(os.getenv("DB_READ_TIMEOUT", "30")))
    db_write_timeout: int = field(default_factory=lambda: int(os.getenv("DB_WRITE_TIMEOUT", "30")))
    db_tls_verify: bool = field(default_factory=lambda: _env_bool("DB_TLS_VERIFY", True))
    db_tls_ca: str = field(default_factory=lambda: os.getenv("DB_TLS_CA", ""))
    api_host: str = field(default_factory=lambda: os.getenv("API_HOST", "0.0.0.0"))
    api_port: int = field(default_factory=lambda: int(os.getenv("API_PORT", "8000")))
    query_log_path: str = field(default_factory=lambda: os.getenv("QUERY_LOG_PATH", "data/query_logs.csv"))
    recommendation_path: str = field(default_factory=lambda: os.getenv("RECOMMENDATION_PATH", "data/recommendations.csv"))
    benchmark_result_path: str = field(default_factory=lambda: os.getenv("BENCHMARK_RESULT_PATH", "data/benchmark_results.csv"))
    job_database_path: str = field(default_factory=lambda: os.getenv("JOB_DATABASE_PATH", "runtime/queryscale_jobs.sqlite3"))
    job_artifact_directory: str = field(default_factory=lambda: os.getenv("JOB_ARTIFACT_DIRECTORY", "runtime/job_artifacts"))
    benchmark_job_max_iterations: int = field(default_factory=lambda: int(os.getenv("BENCHMARK_JOB_MAX_ITERATIONS", "1000")))
    benchmark_job_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("BENCHMARK_JOB_TIMEOUT_SECONDS", "900")))
    benchmark_job_max_attempts: int = field(default_factory=lambda: int(os.getenv("BENCHMARK_JOB_MAX_ATTEMPTS", "3")))

    def __post_init__(self) -> None:
        if not 1 <= self.benchmark_job_max_iterations <= 10_000:
            raise ValueError("BENCHMARK_JOB_MAX_ITERATIONS must be between 1 and 10000")
        if not 1 <= self.benchmark_job_timeout_seconds <= 86_400:
            raise ValueError("BENCHMARK_JOB_TIMEOUT_SECONDS must be between 1 and 86400")
        if self.benchmark_job_max_attempts < 1:
            raise ValueError("BENCHMARK_JOB_MAX_ATTEMPTS must be at least 1")

    def mysql_config(self) -> dict[str, object]:
        config: dict[str, object] = {
            "host": self.db_host,
            "port": self.db_port,
            "database": self.db_name,
            "user": self.db_user,
            "password": self.db_password,
            "charset": self.db_charset,
            "autocommit": True,
            "raise_on_warnings": True,
            "connection_timeout": self.db_connection_timeout,
            "read_timeout": self.db_read_timeout,
            "write_timeout": self.db_write_timeout,
            "ssl_verify_cert": self.db_tls_verify,
            "ssl_verify_identity": self.db_tls_verify,
        }
        if self.db_tls_ca:
            config["ssl_ca"] = self.db_tls_ca
        return config

    def test_mysql_config(self) -> dict[str, object]:
        """Build a connection scoped to the explicitly configured benchmark database."""

        config = self.mysql_config()
        config["database"] = self.db_test_name
        config["user"] = self.db_test_user
        config["password"] = self.db_test_password
        return config


settings = Settings()
