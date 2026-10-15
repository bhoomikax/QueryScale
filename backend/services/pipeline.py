"""Analysis pipeline: query features -> candidates -> rule scores -> (optional ML) ranking."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

from backend.services.candidate_generator import generate_candidates
from backend.services.feature_extractor import extract_features
from backend.services.recommender import generate_recommendations
from backend.services.schema_metadata import SchemaMetadata


def run_pipeline(
    log_path: str | Path,
    feature_path: str | Path,
    candidate_path: str | Path,
    recommendation_path: str | Path,
    metadata: SchemaMetadata | None = None,
    query_feature_path: str | Path | None = None,
    use_ml: bool = True,
    ranker: Any = None,
) -> List[Dict[str, object]]:
    """Run every analysis stage and return the ranked recommendations.

    Query features are written next to ``feature_path`` unless a path is given.
    Without ``metadata`` the saved snapshot or ``database/schema.sql`` is used;
    this function does not open database connections on its own.
    """

    query_features = Path(query_feature_path) if query_feature_path else Path(feature_path).with_name("query_features.csv")
    extract_features(log_path, feature_path, metadata=metadata, query_feature_path=query_features)
    generate_candidates(feature_path, candidate_path)
    return generate_recommendations(candidate_path, recommendation_path, use_ml=use_ml, ranker=ranker)


if __name__ == "__main__":
    run_pipeline("data/query_logs.csv", "data/candidate_features.csv", "data/index_candidates.csv", "data/recommendations.csv")
