"""Paired champion-vs-candidate forecast comparison on a temporal holdout.

Both models are scored only on the cases where each produced a prediction
for the same verified outcome, so neither side gains from scoring an easier
subset.  Point metrics (MAE, RMSE, SMAPE, bias), alert behaviour at the
production trigger threshold (alert volume, false-positive rate, missed
alerts), interval coverage when a model publishes an interval, and a
deterministic bootstrap confidence interval of the MAE difference are
reported.  Nothing here decides a promotion; it is evidence for a reviewer.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PairedCase:
    actual: float
    champion: float
    candidate: float
    champion_low: float | None = None
    champion_high: float | None = None


def _smape(predicted: float, actual: float) -> float:
    denominator = abs(predicted) + abs(actual)
    return 0.0 if denominator == 0 else min(200.0, 200.0 * abs(predicted - actual) / denominator)


def point_metrics(pairs: Sequence[tuple[float, float]]) -> dict[str, float | int | None]:
    """``pairs`` are ``(predicted, actual)``."""
    if not pairs:
        return {"n": 0, "mae": None, "rmse": None, "smape": None, "bias": None}
    errors = [predicted - actual for predicted, actual in pairs]
    return {
        "n": len(pairs),
        "mae": round(sum(abs(value) for value in errors) / len(errors), 6),
        "rmse": round(math.sqrt(sum(value * value for value in errors) / len(errors)), 6),
        "smape": round(sum(_smape(p, a) for p, a in pairs) / len(pairs), 6),
        "bias": round(sum(errors) / len(errors), 6),
    }


def alert_metrics(pairs: Sequence[tuple[float, float]], threshold: float) -> dict[str, float | int | None]:
    """Alerts the forecast would have raised at ``threshold`` versus reality."""
    alerts = [(p, a) for p, a in pairs if p >= threshold]
    false_positives = sum(1 for _p, a in alerts if a < threshold)
    missed = sum(1 for p, a in pairs if a >= threshold and p < threshold)
    return {
        "alert_volume": len(alerts),
        "false_positive_rate": round(false_positives / len(alerts), 6) if alerts else None,
        "missed_alerts": missed,
    }


def interval_coverage(cases: Sequence[tuple[float | None, float | None, float]]) -> float | None:
    """Fraction of actuals inside ``(low, high)``; ``None`` without intervals."""
    usable = [(low, high, actual) for low, high, actual in cases if low is not None and high is not None]
    if not usable:
        return None
    return round(sum(1 for low, high, actual in usable if low <= actual <= high) / len(usable), 6)


def bootstrap_mae_delta(cases: Sequence[PairedCase], *, resamples: int = 1000,
                        seed: int = 0) -> dict[str, float | None]:
    """95% bootstrap interval of MAE(candidate) - MAE(champion); negative favours the candidate."""
    if not cases:
        return {"delta": None, "ci_low": None, "ci_high": None}
    diffs = [abs(case.candidate - case.actual) - abs(case.champion - case.actual) for case in cases]
    rng = random.Random(seed)  # nosec B311 - statistics, not security
    means = sorted(
        sum(diffs[rng.randrange(len(diffs))] for _ in diffs) / len(diffs) for _ in range(resamples)
    )
    return {
        "delta": round(sum(diffs) / len(diffs), 6),
        "ci_low": round(means[int(0.025 * (resamples - 1))], 6),
        "ci_high": round(means[int(0.975 * (resamples - 1))], 6),
    }


def paired_comparison(cases: Sequence[PairedCase], *, threshold: float, resamples: int = 1000,
                      seed: int = 0) -> dict[str, object]:
    champion = [(case.champion, case.actual) for case in cases]
    candidate = [(case.candidate, case.actual) for case in cases]
    delta = bootstrap_mae_delta(cases, resamples=resamples, seed=seed)
    if delta["ci_high"] is not None and delta["ci_high"] < 0:
        verdict = "CANDIDATE_BETTER"
    elif delta["ci_low"] is not None and delta["ci_low"] > 0:
        verdict = "CHAMPION_BETTER"
    else:
        verdict = "INCONCLUSIVE"
    return {
        "paired_cases": len(cases),
        "threshold_percent": threshold,
        "champion": {
            **point_metrics(champion), **alert_metrics(champion, threshold),
            "interval_coverage": interval_coverage(
                [(case.champion_low, case.champion_high, case.actual) for case in cases]
            ),
        },
        "candidate": {**point_metrics(candidate), **alert_metrics(candidate, threshold), "interval_coverage": None},
        "mae_delta_candidate_minus_champion": delta,
        "verdict": verdict,
    }
