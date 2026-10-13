"""Rule-based recommendation engine for index candidates."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, List


def _read_candidates(path: str | Path) -> List[Dict[str, str]]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Candidate file not found: {csv_path}")

    with csv_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return list(reader)


def _priority_for(score: float) -> str:
    if score >= 20:
        return "high"
    if score >= 10:
        return "medium"
    return "low"

#score =
#    frequency × 2
 # + execution_time × 0.5
 # + WHERE usage × 10
 # + JOIN usage × 6
 # + ORDER usage × 4
 # + table-size component
  
def generate_recommendations(candidate_path: str | Path, output_path: str | Path) -> List[Dict[str, object]]:
    candidates = _read_candidates(candidate_path)
    recommendations: List[Dict[str, object]] = []

    for row in candidates:
        try:
            score = float(row.get("score", 0) or 0)
        except ValueError:
            continue

        if score <= 0:
            continue

        priority = _priority_for(score)
        recommendations.append({
            "candidate_id": row.get("candidate_id", ""),
            "table": row.get("table", ""),
            "columns": row.get("columns", ""),
            "score": round(score, 3),
            "priority": priority,
            "recommended_sql": row.get("recommended_action", ""),
            "reason": row.get("reason", ""),
        })

    recommendations.sort(key=lambda item: float(item["score"]), reverse=True)
    recommendations = [item for item in recommendations if _priority_for(float(item["score"])) != "low"]

    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = ["candidate_id", "table", "columns", "score", "priority", "recommended_sql", "reason"]
    with output_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(recommendations)

    return recommendations


if __name__ == "__main__":
    generate_recommendations("data/index_candidates.csv", "data/recommendations.csv")
