"""MySQL connection handling for the QueryScale application."""

from __future__ import annotations

from typing import Any
from collections.abc import Mapping

import mysql.connector
from mysql.connector import MySQLConnection
from mysql.connector.errors import Error as MySQLError

from backend.config import settings


def open_connection(config: Mapping[str, object] | None = None) -> MySQLConnection:
    """Open MySQL using shared connection defaults, with optional config overrides."""

    mysql_config = settings.mysql_config() if config is None else dict(config)
    mysql_config.setdefault("connection_timeout", settings.db_connection_timeout)
    mysql_config.setdefault("read_timeout", settings.db_read_timeout)
    mysql_config.setdefault("write_timeout", settings.db_write_timeout)
    mysql_config.setdefault("ssl_verify_cert", settings.db_tls_verify)
    mysql_config.setdefault("ssl_verify_identity", settings.db_tls_verify)
    if settings.db_tls_ca:
        mysql_config.setdefault("ssl_ca", settings.db_tls_ca)

    try:
        return mysql.connector.connect(**mysql_config)
    except MySQLError as exc:
        host = mysql_config.get("host", settings.db_host)
        database = mysql_config.get("database", settings.db_name)
        raise RuntimeError(
            f"Unable to connect to MySQL at {host}:{mysql_config.get('port', settings.db_port)} "
            f"using database '{database}'. Check database settings and network access."
        ) from exc


def get_connection() -> MySQLConnection:
    """Open a connection to the configured application database."""

    return open_connection()


def close_connection(connection: Any) -> None:
    """Close a connection when it is open."""

    if connection is not None:
        try:
            connection.close()
        except Exception:
            # Cleanup must not obscure an exception from the operation using it.
            pass
