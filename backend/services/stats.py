"""Small, dependency-free statistics helpers shared by analysis and benchmarking."""

from __future__ import annotations

import math
import statistics
from typing import Any, Optional, Sequence

# Minimum sample count before a P95 is reported. Below this it is reported as unavailable.
MIN_P95_SAMPLES = 20


def percentile(values: Sequence[float], percentile_rank: float) -> float:
    """Return a linearly interpolated percentile for a non-empty sample."""

    if not values:
        raise ValueError("Cannot calculate a percentile for an empty sample")
    if not 0 <= percentile_rank <= 100:
        raise ValueError("Percentile rank must be between 0 and 100")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile_rank / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def p95_or_none(values: Sequence[float], min_samples: int = MIN_P95_SAMPLES) -> Optional[float]:
    return round(percentile(values, 95), 3) if len(values) >= min_samples else None


def median_or_none(values: Sequence[float]) -> Optional[float]:
    return round(statistics.median(values), 3) if values else None


def mean_or_none(values: Sequence[float]) -> Optional[float]:
    return round(statistics.fmean(values), 3) if values else None


def optional_float(value: Any) -> Optional[float]:
    """Parse a CSV/JSON value. Empty strings and invalid values mean 'unavailable', not zero."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    text = str(value).strip()
    if not text or text.lower() in {"none", "null", "nan", "n/a"}:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return None if math.isnan(parsed) else parsed


def optional_int(value: Any) -> Optional[int]:
    parsed = optional_float(value)
    return int(parsed) if parsed is not None else None


def parse_bool(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def speedup(baseline: Optional[float], indexed: Optional[float]) -> Optional[float]:
    """baseline / indexed, or None when either value is missing or non-positive."""

    if baseline is None or indexed is None or baseline <= 0 or indexed <= 0:
        return None
    return round(baseline / indexed, 3)


def latency_reduction_pct(baseline: Optional[float], indexed: Optional[float]) -> Optional[float]:
    """((baseline - indexed) / baseline) * 100, or None when the baseline is missing or non-positive."""

    if baseline is None or indexed is None or baseline <= 0 or indexed < 0:
        return None
    return round((baseline - indexed) / baseline * 100, 3)
