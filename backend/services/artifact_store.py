"""Artifact storage for model files and reports: local filesystem, optionally mirrored to S3.

Local storage always holds the working copy. With ``ARTIFACT_STORE=s3`` each
published artifact is also uploaded to
``s3://{S3_ARTIFACT_BUCKET}/{S3_ARTIFACT_PREFIX}/{category}/{version}/{filename}``
and a ``latest.json`` pointer is updated. boto3 is imported lazily, so local
development does not need it. Credentials come from the standard AWS chain
(ECS task role, environment, or profile) and are never stored here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from backend.config import settings


logger = logging.getLogger(__name__)


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class S3ArtifactStore:
    def __init__(self, bucket: str, prefix: str, client: Any = None) -> None:
        if not bucket:
            raise ValueError("S3_ARTIFACT_BUCKET is required when ARTIFACT_STORE=s3")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3  # type: ignore
            except ImportError as exc:
                raise RuntimeError("ARTIFACT_STORE=s3 requires boto3. Install it with: pip install boto3") from exc
            client = boto3.client("s3")
        self.client = client

    def key(self, category: str, version: str, filename: str) -> str:
        return "/".join(part for part in (self.prefix, category, version, filename) if part)

    def publish(self, category: str, version: str, files: Iterable[Path]) -> Dict[str, str]:
        uploaded: Dict[str, str] = {}
        for path in files:
            key = self.key(category, version, path.name)
            self.client.upload_file(str(path), self.bucket, key)
            uploaded[path.name] = f"s3://{self.bucket}/{key}"
        pointer = {"version": version, "files": uploaded}
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(category, "", "latest.json"),
            Body=json.dumps(pointer).encode("utf-8"),
            ContentType="application/json",
        )
        return uploaded

    def download_latest(self, category: str, target_dir: Path) -> Optional[str]:
        response = self.client.get_object(Bucket=self.bucket, Key=self.key(category, "", "latest.json"))
        pointer = json.loads(response["Body"].read())
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename in pointer.get("files", {}):
            self.client.download_file(
                self.bucket, self.key(category, pointer["version"], filename), str(target_dir / filename)
            )
        return pointer.get("version")


def get_remote_store() -> Optional[S3ArtifactStore]:
    if settings.artifact_store.lower() != "s3":
        return None
    return S3ArtifactStore(settings.s3_artifact_bucket, settings.s3_artifact_prefix)


def publish_artifacts(category: str, version: str, files: Iterable[Path]) -> Dict[str, str]:
    """Mirror artifacts to S3 when configured. Returns uploaded URIs (empty for local-only)."""

    store = get_remote_store()
    if store is None:
        return {}
    try:
        return store.publish(category, version, list(files))
    except Exception as exc:
        logger.error("Artifact upload to S3 failed for %s/%s: %s", category, version, exc)
        raise
