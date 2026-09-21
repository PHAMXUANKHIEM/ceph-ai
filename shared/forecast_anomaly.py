"""Bounded anomaly candidates used by shadow and offline benchmark paths."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping


def candidate_d_isolation_scores(
    rows: Iterable[Mapping[str, float]], *, history_size: int = 24, threshold: float = 3.5,
) -> list[float | None]:
    """Return a lightweight, deterministic isolation score per feature row.

    This is Candidate D: a robust multivariate isolation score.  It is bounded
    O(n * history_size * dimensions), has no model-state side effect, and is
    intentionally evidence-only until a separate promotion decision approves
    it.  The score is the RMS robust z-score against the preceding window.
    """

    ordered = [dict(row) for row in rows]
    result: list[float | None] = []
    size = max(3, int(history_size))
    for index, row in enumerate(ordered):
        history = ordered[max(0, index - size):index]
        keys = sorted(set(row) & set().union(*(item.keys() for item in history)) if history else set())
        if len(history) < max(3, size // 2) or not keys:
            result.append(None)
            continue
        scores: list[float] = []
        for key in keys:
            values = [float(item[key]) for item in history if math.isfinite(float(item[key]))]
            current = float(row[key])
            if len(values) < 3 or not math.isfinite(current):
                continue
            center = statistics.median(values)
            mad = statistics.median(abs(value - center) for value in values)
            scale = max(1e-6, 1.4826 * mad)
            scores.append(abs(current - center) / scale)
        result.append(math.sqrt(sum(score * score for score in scores) / len(scores)) if scores else None)
    return result


def candidate_d_alerts(scores: Iterable[float | None], *, threshold: float = 3.5) -> list[bool]:
    return [score is not None and math.isfinite(float(score)) and float(score) >= float(threshold) for score in scores]
