"""Robust consensus for independent forecast candidates."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class ConsensusStatus(str, Enum):
    CONSENSUS = "CONSENSUS"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    INSUFFICIENT_CANDIDATES = "INSUFFICIENT_CANDIDATES"


@dataclass(frozen=True)
class ForecastConsensus:
    value: float
    lower: float
    upper: float
    candidate_count: int
    agreeing_count: int
    ratio: float
    status: str

    @property
    def usable(self) -> bool:
        return self.status == ConsensusStatus.CONSENSUS.value


def aggregate_forecasts(
    values: Iterable[float],
    *,
    minimum_candidates: int = 2,
    minimum_ratio: float = 0.67,
    absolute_tolerance: float = 5.0,
    relative_tolerance: float = 0.10,
) -> ForecastConsensus:
    """Aggregate finite predictions and reject outliers by median deviation."""

    if minimum_candidates < 1:
        raise ValueError("minimum_candidates must be at least 1")
    if not 0.0 < minimum_ratio <= 1.0:
        raise ValueError("minimum_ratio must be between 0 and 1")
    if absolute_tolerance < 0 or relative_tolerance < 0:
        raise ValueError("tolerances must be non-negative")

    finite = [
        float(value) for value in values
        if math.isfinite(float(value))
    ]
    if not finite:
        return ForecastConsensus(0.0, 0.0, 0.0, 0, 0, 0.0, ConsensusStatus.INSUFFICIENT_CANDIDATES.value)

    median = statistics.median(finite)
    tolerance = max(absolute_tolerance, abs(median) * relative_tolerance)
    agreeing = [value for value in finite if abs(value - median) <= tolerance]
    ratio = len(agreeing) / len(finite)
    status = (
        ConsensusStatus.CONSENSUS.value
        if len(finite) >= minimum_candidates and len(agreeing) / len(finite) >= minimum_ratio
        else ConsensusStatus.LOW_CONFIDENCE.value
        if len(finite) >= minimum_candidates
        else ConsensusStatus.INSUFFICIENT_CANDIDATES.value
    )
    interval_values = agreeing or finite
    return ForecastConsensus(
        value=float(median),
        lower=float(min(interval_values)),
        upper=float(max(interval_values)),
        candidate_count=len(finite),
        agreeing_count=len(agreeing),
        ratio=round(ratio, 6),
        status=status,
    )
