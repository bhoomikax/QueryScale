"""Shared benchmark result aggregation for API responses and background jobs."""

from __future__ import annotations


def aggregate_results(results: list[dict[str, object]]) -> dict[str, float | int]:
    if not results:
        raise ValueError("Benchmark produced no query results")
    execution_count = sum(int(row["execution_count"]) for row in results)
    if execution_count <= 0:
        raise ValueError("Benchmark results contain no measured executions")
    before_ms = sum(
        float(row["before_avg_ms"]) * int(row["execution_count"]) for row in results
    ) / execution_count
    after_ms = sum(
        float(row["after_avg_ms"]) * int(row["execution_count"]) for row in results
    ) / execution_count
    return {
        "before_ms": round(before_ms, 3),
        "after_ms": round(after_ms, 3),
        "speedup": round(before_ms / after_ms, 3) if after_ms else 0.0,
        "improvement_percentage": round((before_ms - after_ms) / before_ms * 100, 3)
        if before_ms
        else 0.0,
        "execution_count": execution_count,
    }
