"""MySQL connection handling for the QueryScale application."""

from __future__ import annotations

from typing import Any

import mysql.connector
from mysql.connector import MySQLConnection
from mysql.connector.errors import Error as MySQLError

from backend.config import settings


def open_connection(config: dict[str, object] | None = None) -> MySQLConnection:
    """Open MySQL with the shared settings (TLS, timeout) or an explicit config (AWS scripts)."""

    mysql_config = settings.mysql_config() if config is None else dict(config)
    try:
        return mysql.connector.connect(**mysql_config)
    except MySQLError as exc:
        raise RuntimeError(
            f"Unable to connect to MySQL at {mysql_config.get('host', settings.db_host)}:{mysql_config.get('port', settings.db_port)} "
            f"using database '{mysql_config.get('database', settings.db_name)}'. Check .env and ensure MySQL is running."
        ) from exc


def get_connection() -> MySQLConnection:
    """Open a connection using the configured database settings."""

    return open_connection()


def close_connection(connection: Any) -> None:
    """Close a connection when it is open."""

    if connection is not None and connection.is_connected():
        connection.close()