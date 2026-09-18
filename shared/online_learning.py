"""Small, bounded River adapter for the first online-learning canary.

This module deliberately starts with an online mean rather than a neural
model.  It is useful as a low-cost adaptive baseline, has a tiny state, and
can be safely promoted into a richer River pipeline later.  Callers must
pass a decision from ``shared.learning_runtime`` before mutating it.
"""

from __future__ import annotations

import math
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from river import stats
from sqlalchemy import select

from shared.models import OnlineLearnerState
from shared.online_learning_gate import OnlineLearningGateStatus


MODEL_ALGORITHM = "river_mean"
MODEL_VERSION = "river-mean-v1"
DEFAULT_FEATURE_SCHEMA = "scalar-v1"


@dataclass(frozen=True)
class OnlineUpdate:
    applied: bool
    target: str
    reason: str
    sample_count: int
    prediction: float | None


@dataclass(frozen=True)
class BoundedLearningResult:
    """Auditable result of one bounded learning cycle."""

    processed: int
    applied: int
    failed: int
    skipped: int
    reason: str
    elapsed_seconds: float


class LearningCircuitBreaker:
    """Small in-process breaker that stops a failing learner from retrying hot."""

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        if failure_threshold < 1 or cooldown_seconds <= 0:
            raise ValueError("invalid learning circuit-breaker settings")
        self.failure_threshold = int(failure_threshold)
        self.cooldown_seconds = float(cooldown_seconds)
        self.consecutive_failures = 0
        self.opened_until = 0.0

    def allow(self, *, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        return current >= self.opened_until

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_until = 0.0

    def record_failure(self, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.opened_until = current + self.cooldown_seconds


def run_bounded_updates(
    values,
    update,
    *,
    max_samples: int,
    timeout_seconds: float,
    circuit_breaker: LearningCircuitBreaker,
    clock=time.monotonic,
) -> BoundedLearningResult:
    """Run a finite update cycle with hard item/time/failure bounds."""

    if max_samples < 1 or timeout_seconds <= 0:
        raise ValueError("learning cycle budget must be positive")
    started = clock()
    deadline = started + timeout_seconds
    if not circuit_breaker.allow(now=started):
        return BoundedLearningResult(0, 0, 0, 0, "circuit_open", 0.0)

    processed = applied = failed = skipped = 0
    reason = "completed"
    for value in values:
        now = clock()
        if processed >= max_samples:
            reason = "sample_budget_exhausted"
            break
        if now >= deadline:
            reason = "timeout"
            break
        processed += 1
        try:
            update(value)
        except Exception:
            failed += 1
            circuit_breaker.record_failure(now=now)
            reason = "update_failed"
            break
        applied += 1
        circuit_breaker.record_success()
    else:
        if processed >= max_samples:
            reason = "sample_budget_exhausted"
    elapsed = max(0.0, clock() - started)
    return BoundedLearningResult(processed, applied, failed, skipped, reason, elapsed)


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


def guarded_update(
    learner: RiverMeanLearner,
    value: float,
    decision,
    *,
    target: str = "shadow",
    quality_decision=None,
) -> OnlineUpdate:
    """Apply one update only when the runtime gate permits that target."""

    if quality_decision is not None and (
        not bool(getattr(quality_decision, "allowed", False))
        or getattr(quality_decision, "status", None) != OnlineLearningGateStatus.READY_TO_LEARN.value
    ):
        return OnlineUpdate(
            applied=False,
            target=target,
            reason=str(getattr(quality_decision, "reason", "quality gate blocked sample")),
            sample_count=learner.sample_count,
            prediction=learner.predict_one(),
        )
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


def _cluster_key(cluster_id: str | None) -> str:
    return str(cluster_id or "__default__")


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def snapshot_checksum(snapshot: dict[str, Any]) -> str:
    """Return a deterministic checksum for the JSON learner payload."""

    return hashlib.sha256(_canonical_json(snapshot).encode("utf-8")).hexdigest()


def _empty_state_payload() -> dict[str, Any]:
    return RiverMeanLearner().snapshot()


def load_or_reset_state(
    session,
    *,
    cluster_id: str | None,
    host: str,
    metric: str,
    model_version: str = MODEL_VERSION,
    feature_schema: str = DEFAULT_FEATURE_SCHEMA,
) -> tuple[RiverMeanLearner, OnlineLearnerState | None]:
    """Load one state or fail closed to a fresh baseline.

    A checksum, identity, algorithm/version, and sample-count mismatch is
    treated as corruption or schema drift.  The row is reset in place and is
    only persisted when the caller commits its transaction.
    """

    row = session.scalar(
        select(OnlineLearnerState).where(
            OnlineLearnerState.cluster_key == _cluster_key(cluster_id),
            OnlineLearnerState.host == host,
            OnlineLearnerState.metric == metric,
            OnlineLearnerState.model_version == model_version,
        )
    )
    if row is None:
        return RiverMeanLearner(), None

    try:
        payload = json.loads(row.state_json)
        if not isinstance(payload, dict):
            raise ValueError("state payload is not an object")
        if row.state_checksum != snapshot_checksum(payload):
            raise ValueError("state checksum mismatch")
        if row.algorithm != MODEL_ALGORITHM or row.feature_schema != feature_schema:
            raise ValueError("state schema mismatch")
        learner = RiverMeanLearner.from_snapshot(payload)
        if row.sample_count != learner.sample_count:
            raise ValueError("state sample count mismatch")
        return learner, row
    except (TypeError, ValueError, json.JSONDecodeError, OverflowError):
        baseline = _empty_state_payload()
        row.algorithm = MODEL_ALGORITHM
        row.feature_schema = feature_schema
        row.state_json = _canonical_json(baseline)
        row.state_checksum = snapshot_checksum(baseline)
        row.sample_count = 0
        row.last_learned_at = None
        row.updated_at = datetime.utcnow()
        return RiverMeanLearner(), row


def save_state(
    session,
    learner: RiverMeanLearner,
    *,
    cluster_id: str | None,
    host: str,
    metric: str,
    feature_schema: str = DEFAULT_FEATURE_SCHEMA,
    learned_at: datetime | None = None,
) -> OnlineLearnerState:
    """Create/update a validated JSON state row for a learner."""

    if not host or not metric:
        raise ValueError("online learner state requires host and metric")
    payload = learner.snapshot()
    now = datetime.utcnow()
    row = session.scalar(
        select(OnlineLearnerState).where(
            OnlineLearnerState.cluster_key == _cluster_key(cluster_id),
            OnlineLearnerState.host == host,
            OnlineLearnerState.metric == metric,
            OnlineLearnerState.model_version == learner.version,
        )
    )
    values = {
        "cluster_key": _cluster_key(cluster_id),
        "host": host,
        "metric": metric,
        "model_version": learner.version,
        "algorithm": learner.algorithm,
        "feature_schema": feature_schema,
        "state_json": _canonical_json(payload),
        "state_checksum": snapshot_checksum(payload),
        "sample_count": learner.sample_count,
        "last_learned_at": learned_at if learner.sample_count else None,
        "updated_at": now,
    }
    if row is None:
        row = OnlineLearnerState(created_at=now, **values)
        session.add(row)
    else:
        for key, value in values.items():
            setattr(row, key, value)
    session.flush()
    return row
