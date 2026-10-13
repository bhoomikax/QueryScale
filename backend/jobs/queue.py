"""Queue boundary and local SQLite-backed development adapter."""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from backend.jobs.store import SQLiteJobStore


class JobQueue(Protocol):
    def submit(self, payload: dict[str, Any]) -> dict[str, Any]: ...
    def claim_next(self, owner: str) -> dict[str, Any] | None: ...


class LocalSQLiteJobQueue:
    """A persistent local queue sharing the SQLite job metadata database."""

    def __init__(self, store: SQLiteJobStore):
        self.store = store

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.store.enqueue(payload, str(uuid.uuid4()))

    def claim_next(self, owner: str) -> dict[str, Any] | None:
        return self.store.claim_next(owner)
