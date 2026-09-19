"""Fail-closed per-sample gate for the online-learning path.

This is intentionally separate from the forecast history quality summary:
forecast quality describes a window, while online learning must decide whether
one individual sample is safe to mutate model state with.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class OnlineLearningGateStatus(str, Enum):
    DATA_QUALITY = "DATA_QUALITY"
    NO_LABEL = "NO_LABEL"
    DRIFT = "DRIFT"
    READY_TO_LEARN = "READY_TO_LEARN"


@dataclass(frozen=True)
class OnlineLearningSample:
    value: float | None
    observed_at: datetime | None
    label: float | None = None
    sample_id: str | None = None


@dataclass(frozen=True)
class OnlineLearningGateDecision:
    status: str
    allowed: bool
    reason: str
    sample_key: str | None = None
    age_seconds: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "allowed": self.allowed,
            "reason": self.reason,
            "sample_key": self.sample_key,
            "age_seconds": self.age_seconds,
        }


@dataclass
class OnlineLearningInputTracker:
    """Bounded in-memory identity/timestamp tracker for one stream."""

    last_observed_at: datetime | None = None
    max_seen_keys: int = 2048
    _seen_keys: set[str] = field(default_factory=set, init=False, repr=False)
    _seen_order: deque[str] = field(default_factory=deque, init=False, repr=False)

    def remember(self, sample_key: str, observed_at: datetime) -> None:
        if sample_key not in self._seen_keys:
            self._seen_keys.add(sample_key)
            self._seen_order.append(sample_key)
            while len(self._seen_order) > self.max_seen_keys:
                self._seen_keys.discard(self._seen_order.popleft())
        if self.last_observed_at is None or observed_at > self.last_observed_at:
            self.last_observed_at = observed_at

    def has_seen(self, sample_key: str) -> bool:
        return sample_key in self._seen_keys


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def evaluate_sample(
    sample: OnlineLearningSample,
    tracker: OnlineLearningInputTracker,
    *,
    now: datetime,
    max_age_seconds: float = 120.0,
    max_forward_gap_seconds: float = 300.0,
    require_label: bool = True,
    drift_reference: float | None = None,
    drift_absolute_threshold: float = 0.0,
    drift_relative_threshold: float = 0.0,
) -> OnlineLearningGateDecision:
    """Return whether one sample is allowed to update a learner.

    The tracker changes only for a fully accepted sample, so rejected stale or
    out-of-order inputs cannot move the stream cursor forward.
    """

    if max_age_seconds < 0 or max_forward_gap_seconds < 0:
        raise ValueError("sample age and gap limits must be non-negative")
    if drift_absolute_threshold < 0 or drift_relative_threshold < 0:
        raise ValueError("drift thresholds must be non-negative")
    if sample.value is None or not math.isfinite(float(sample.value)):
        return OnlineLearningGateDecision(
            OnlineLearningGateStatus.DATA_QUALITY.value, False,
            "sample value is missing or non-finite",
        )
    if sample.observed_at is None:
        return OnlineLearningGateDecision(
            OnlineLearningGateStatus.DATA_QUALITY.value, False,
            "sample timestamp is missing",
        )

    observed_at = _utc(sample.observed_at)
    reference_now = _utc(now)
    age = max(0.0, (reference_now - observed_at).total_seconds())
    sample_key = str(sample.sample_id or observed_at.isoformat())
    if age > max_age_seconds:
        return OnlineLearningGateDecision(
            OnlineLearningGateStatus.DATA_QUALITY.value, False,
            f"sample is stale: {age:.1f}s old; limit is {max_age_seconds:.1f}s",
            sample_key, age,
        )
    if tracker.has_seen(sample_key):
        return OnlineLearningGateDecision(
            OnlineLearningGateStatus.DATA_QUALITY.value, False,
            "duplicate sample identity", sample_key, age,
        )
    if tracker.last_observed_at is not None:
        if observed_at <= tracker.last_observed_at:
            return OnlineLearningGateDecision(
                OnlineLearningGateStatus.DATA_QUALITY.value, False,
                "sample is out of order", sample_key, age,
            )
        gap = (observed_at - tracker.last_observed_at).total_seconds()
        if gap > max_forward_gap_seconds:
            return OnlineLearningGateDecision(
                OnlineLearningGateStatus.DATA_QUALITY.value, False,
                f"sample gap is {gap:.1f}s; limit is {max_forward_gap_seconds:.1f}s",
                sample_key, age,
            )
    if require_label and sample.label is None:
        return OnlineLearningGateDecision(
            OnlineLearningGateStatus.NO_LABEL.value, False,
            "verified label is required before online learning", sample_key, age,
        )
    if sample.label is not None and not math.isfinite(float(sample.label)):
        return OnlineLearningGateDecision(
            OnlineLearningGateStatus.NO_LABEL.value, False,
            "label is non-finite", sample_key, age,
        )
    if drift_reference is not None:
        if not math.isfinite(float(drift_reference)):
            raise ValueError("drift reference must be finite")
        distance = abs(float(sample.value) - float(drift_reference))
        limit = max(
            float(drift_absolute_threshold),
            abs(float(drift_reference)) * float(drift_relative_threshold),
        )
        if distance > limit:
            return OnlineLearningGateDecision(
                OnlineLearningGateStatus.DRIFT.value, False,
                f"sample drift {distance:.4g} exceeds limit {limit:.4g}",
                sample_key, age,
            )

    tracker.remember(sample_key, observed_at)
    return OnlineLearningGateDecision(
        OnlineLearningGateStatus.READY_TO_LEARN.value, True,
        "sample passed online-learning quality gate", sample_key, age,
    )
