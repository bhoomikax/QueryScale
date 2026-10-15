"""CLI entry point for QueryScale recommendation generation."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from backend.services.pipeline import run_pipeline
from backend.services.schema_metadata import load_metadata


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate QueryScale index recommendations from SQL workload logs.")
    parser.add_argument("--log-path", default="data/query_logs.csv", help="Path to the query log CSV file.")
    parser.add_argument("--feature-path", default="data/candidate_features.csv", help="Output path for extracted feature rows.")
    parser.add_argument("--candidate-path", default="data/index_candidates.csv", help="Output path for generated index candidates.")
    parser.add_argument("--recommendation-path", default="data/recommendations.csv", help="Output path for final recommendations.")
    parser.add_argument(
        "--metadata-source", default="snapshot_or_schema",
        choices=["auto", "mysql", "snapshot", "schema", "snapshot_or_schema"],
        help="Schema statistics source. The default never connects to MySQL.",
    )
    parser.add_argument("--no-ml", action="store_true", help="Use rule-based ranking only.")
    args = parser.parse_args(argv)

    try:
        metadata = load_metadata(source=args.metadata_source)
        rows = run_pipeline(
            args.log_path, args.feature_path, args.candidate_path, args.recommendation_path,
            metadata=metadata, use_ml=not args.no_ml,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}")
        return 1

    method = rows[0]["ranking_method"] if rows else "n/a"
    print(f"Generated {len(rows)} recommendations ({method} ranking, metadata: {metadata.source}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
