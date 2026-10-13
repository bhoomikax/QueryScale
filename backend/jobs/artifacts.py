"""Artifact storage boundary with a local filesystem adapter."""

from __future__ import annotations

import uuid
from pathlib import Path


class LocalArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def result_path(self, job_id: str) -> Path:
        canonical_id = str(uuid.UUID(job_id))
        path = (self.root / canonical_id / "results.csv").resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid job artifact identifier")
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def reference(job_id: str) -> str:
        return f"local-job-artifact:{uuid.UUID(job_id)}/results.csv"
