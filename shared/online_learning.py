"""Small, bounded River adapter for the first online-learning canary.

This module deliberately starts with an online mean rather than a neural
model.  It is useful as a low-cost adaptive baseline, has a tiny state, and
can be safely promoted into a richer River pipeline later.  Callers must
pass a decision from ``shared.learning_runtime`` before mutating it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from river import stats


MODEL_ALGORITHM = "river_mean"
MODEL_VERSION = "river-mean-v1"


@dataclass(frozen=True)
class OnlineUpdate:
    applied: bool
    target: str
    reason: str
    sample_count: int
    prediction: float | None


class RiverMeanLearner:
    """A JSON-snapshot-friendly online baseline backed by River."""

    algorithm = MODEL_ALGORITHM
    version = MODEL_VERSION

    def __init__(self) -> None:
        self._mean = stats.Mean()

    @property
    def sample_count(self) -> int:
        return int(self._mean.n)

    @property
    def value(self) -> float | None:
        return float(self._mean.get()) if self.sample_count else None

    def predict_one(self, fallback: float | None = None) -> float | None:
        """Return the current adaptive baseline before the next update."""

        return self.value if self.value is not None else fallback

    def learn_one(self, value: float) -> None:
        """Learn one verified numeric outcome; reject invalid values."""

        numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("online learner requires a finite numeric value")
        self._mean.update(numeric)

    def snapshot(self) -> dict[str, Any]:
        """Return only JSON-safe state; never serialize executable objects."""

        return {
            "algorithm": self.algorithm,
            "version": self.version,
            "sample_count": self.sample_count,
            "mean": self.value,
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "RiverMeanLearner":
        if snapshot.get("algorithm") != cls.algorithm:
            raise ValueError("online learner snapshot algorithm mismatch")
        if snapshot.get("version") != cls.version:
            raise ValueError("online learner snapshot version mismatch")
        count = int(snapshot.get("sample_count", 0))
        mean = snapshot.get("mean")
        if count < 0 or (mean is not None and not math.isfinite(float(mean))):
            raise ValueError("online learner snapshot contains invalid state")
        if (count == 0) != (mean is None):
            raise ValueError("online learner snapshot count/mean mismatch")
        learner = cls()
        # River's Mean state is deliberately tiny.  Restore the two scalar
        # statistics without replaying historical samples or using pickle.
        learner._mean.n = float(count)
        learner._mean._mean = float(mean or 0.0)
        return learner


def guarded_update(learner: RiverMeanLearner, value: float, decision, *, target: str = "shadow") -> OnlineUpdate:
    """Apply one update only when the runtime gate permits that target."""

    if target == "active":
        allowed = bool(decision.can_update_active)
    elif target == "shadow":
        allowed = bool(decision.can_update_shadow)
    else:
        raise ValueError("online learner target must be 'shadow' or 'active'")
    prediction = learner.predict_one()
    if not allowed:
        return OnlineUpdate(
            applied=False, target=target, reason=str(decision.reason),
            sample_count=learner.sample_count, prediction=prediction,
        )
    learner.learn_one(value)
    return OnlineUpdate(
        applied=True, target=target, reason="online sample accepted",
        sample_count=learner.sample_count, prediction=prediction,
    )
