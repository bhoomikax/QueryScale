"""Shared test helpers. No helper here opens a database connection."""

from __future__ import annotations

import csv
import dataclasses
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Sequence
from unittest import mock

from backend.config import Settings, settings


LOG_HEADER = [
    "query_id", "query_template", "timestamp", "execution_time_ms", "frequency",
    "tables", "where_columns", "join_columns", "order_columns", "rows_returned", "rows_examined",
]


@contextmanager
def patched_settings(**overrides: Any) -> Iterator[Settings]:
    """Replace the shared Settings object in every loaded backend/scripts module."""

    # Base the replacement on whatever Settings object is currently active so nested
    # patches compose (an inner patch keeps the outer overrides).
    import backend.config

    current = getattr(backend.config, "settings", settings)
    replacement = dataclasses.replace(current, **overrides)
    patches = []
    for name, module in list(sys.modules.items()):
        if module is None or not name.startswith(("backend", "scripts")):
            continue
        if isinstance(getattr(module, "settings", None), Settings):
            patches.append(mock.patch.object(module, "settings", replacement))
    for patch in patches:
        patch.start()
    try:
        yield replacement
    finally:
        for patch in reversed(patches):
            patch.stop()


def write_log(path: Path, rows: Iterable[Sequence[Any]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(LOG_HEADER)
        for row in rows:
            writer.writerow(list(row) + [""] * (len(LOG_HEADER) - len(row)))
    return path


def log_rows(query_id: str, sql: str, latencies: List[float], **extra: Any) -> List[List[Any]]:
    return [
        [query_id, sql, f"2026-01-01T00:00:{i % 60:02d}Z", latency, "HIGH", "", "", "", "",
         extra.get("rows_returned", ""), extra.get("rows_examined", "")]
        for i, latency in enumerate(latencies)
    ]


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))
