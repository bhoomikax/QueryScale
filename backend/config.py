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


settings = Settings()
