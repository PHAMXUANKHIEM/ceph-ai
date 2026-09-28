"""Fail-closed ADWIN policy for forecast drift and promotion safety.

The detector is deliberately kept independent from SQLAlchemy so replay tests,
the watcher, and a future promotion worker use exactly the same policy.  ADWIN
signals are not treated as a verdict by themselves: the policy adds hysteresis
and a post-drift warm-up window, and it keeps metric drift separate from model
error drift.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from statistics import mean, pstdev
from typing import Iterable

try:  # River is a production dependency, but imports must fail closed.
    from river.drift import ADWIN
except Exception:  # pragma: no cover - exercised by deployment smoke tests
    ADWIN = None  # type: ignore[assignment,misc]


ADWIN_POLICY_VERSION = "adwin-policy-v1"
MAX_REPLAY_SAMPLES = 512


@dataclass(frozen=True)
class DriftSample:
    """One evaluated forecast outcome in chronological order."""

    evaluated_at: datetime
    metric_value: float
    residual: float | None
    mae: float | None


@dataclass(frozen=True)
class DriftDecision:
    """The policy result consumed by forecast and promotion code."""

    status: str
    metric_drift: bool = False
    residual_drift: bool = False
    mae_drift: bool = False
    confidence_multiplier: float = 1.0
    promotion_blocked: bool = True
    warmup_remaining: int = 0
    sample_count: int = 0
    drift_score: float = 0.0
    reason: str = ""


@dataclass
class AdwinPolicy:
    """ADWIN with explicit hysteresis, warm-up, and JSON-safe state.

    ``metric_value`` is the observed metric level.  ``residual`` is the signed
    forecast residual and ``mae`` is its absolute error.  A metric shift is
    therefore distinguishable from a model-quality regression: only residual
    drift lowers confidence and enters ``DRIFT``; MAE drift additionally blocks
    promotion.  A metric-only shift is reported as ``METRIC_DRIFT``.
    """

    delta: float = 0.002
    min_samples: int = 10
    clear_consecutive: int = 3
    warmup_samples: int = 5
    max_samples: int = MAX_REPLAY_SAMPLES
    version: str = ADWIN_POLICY_VERSION
    samples: list[dict] = field(default_factory=list)
    metric_active: bool = False
    residual_active: bool = False
    mae_active: bool = False
    clear_streak: int = 0
    warmup_remaining: int = 0
    _metric_detector: object | None = field(default=None, init=False, repr=False)
    _residual_detector: object | None = field(default=None, init=False, repr=False)
    _mae_detector: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_samples < 32:
            raise ValueError("ADWIN max_samples must be at least 32")
        if self.min_samples < 1 or self.clear_consecutive < 1:
            raise ValueError("ADWIN sample thresholds must be positive")
        self._reset_detectors()

    @property
    def available(self) -> bool:
        return ADWIN is not None

    def _reset_detectors(self) -> None:
        if ADWIN is None:
            self._metric_detector = self._residual_detector = self._mae_detector = None
            return
        self._metric_detector = ADWIN(delta=self.delta)
        self._residual_detector = ADWIN(delta=self.delta)
        self._mae_detector = ADWIN(delta=self.delta)

    @staticmethod
    def _valid(value: float | None) -> bool:
        return value is not None and math.isfinite(float(value))

    def _raw_drift(self, detector: object | None, value: float | None) -> bool:
        if detector is None or not self._valid(value):
            return False
        detector.update(float(value))  # type: ignore[attr-defined]
        return bool(detector.drift_detected)  # type: ignore[attr-defined]

    def update(self, sample: DriftSample) -> DriftDecision:
        """Consume one sample and return the hysteresis-aware decision."""

        if not self.available:
            return self._decision(
                status="INSUFFICIENT_DATA",
                promotion_blocked=True,
                reason="River ADWIN is unavailable; drift policy is fail-closed",
            )
        if not self._valid(sample.metric_value):
            return self._decision(
                status="INSUFFICIENT_DATA",
                promotion_blocked=True,
                reason="metric value is missing or non-finite",
            )

        metric_raw = self._raw_drift(self._metric_detector, sample.metric_value)
        residual_raw = self._raw_drift(self._residual_detector, sample.residual)
        mae_raw = self._raw_drift(self._mae_detector, sample.mae)
        self.samples.append({
            "evaluated_at": sample.evaluated_at.isoformat(),
            "metric_value": float(sample.metric_value),
            "residual": float(sample.residual) if self._valid(sample.residual) else None,
            "mae": float(sample.mae) if self._valid(sample.mae) else None,
        })
        self.samples = self.samples[-self.max_samples:]

        if metric_raw:
            self.metric_active = True
        if residual_raw:
            self.residual_active = True
        if mae_raw:
            self.mae_active = True

        any_raw = metric_raw or residual_raw or mae_raw
        if any_raw:
            self.clear_streak = 0
            self.warmup_remaining = self.warmup_samples
        elif self.metric_active or self.residual_active or self.mae_active:
            self.clear_streak += 1
            if self.clear_streak >= self.clear_consecutive:
                self.metric_active = False
                self.residual_active = False
                self.mae_active = False
                self.clear_streak = 0

        if self.warmup_remaining:
            self.warmup_remaining -= 1

        if len(self.samples) < self.min_samples:
            return self._decision(
                status="INSUFFICIENT_DATA",
                promotion_blocked=True,
                reason=f"ADWIN warm-up evidence {len(self.samples)}/{self.min_samples}",
            )
        if self.residual_active or self.mae_active:
            reasons = []
            if self.residual_active:
                reasons.append("residual drift")
            if self.mae_active:
                reasons.append("MAE drift blocks promotion")
            return self._decision(
                status="DRIFT",
                confidence_multiplier=0.5 if self.residual_active else 0.7,
                promotion_blocked=True,
                reason="; ".join(reasons),
            )
        if self.metric_active:
            return self._decision(
                status="METRIC_DRIFT",
                confidence_multiplier=0.8,
                promotion_blocked=bool(self.warmup_remaining),
                reason="metric distribution drift; model error drift not confirmed",
            )
        if self.warmup_remaining:
            return self._decision(
                status="WARMUP",
                confidence_multiplier=0.8,
                promotion_blocked=True,
                reason="post-drift warm-up is still running",
            )
        return self._decision(
            status="OK",
            promotion_blocked=False,
            reason="no active ADWIN drift",
        )

    def _decision(self, *, status: str, promotion_blocked: bool,
                  reason: str, confidence_multiplier: float = 1.0) -> DriftDecision:
        active = sum((self.metric_active, self.residual_active, self.mae_active))
        return DriftDecision(
            status=status,
            metric_drift=self.metric_active,
            residual_drift=self.residual_active,
            mae_drift=self.mae_active,
            confidence_multiplier=confidence_multiplier,
            promotion_blocked=promotion_blocked,
            warmup_remaining=self.warmup_remaining,
            sample_count=len(self.samples),
            drift_score=active / 3.0,
            reason=reason,
        )

    def decision(self) -> DriftDecision:
        """Return the current state without feeding a duplicate sample."""
        if len(self.samples) < self.min_samples:
            return self._decision(
                status="INSUFFICIENT_DATA", promotion_blocked=True,
                reason=f"ADWIN warm-up evidence {len(self.samples)}/{self.min_samples}",
            )
        if self.residual_active or self.mae_active:
            return self._decision(
                status="DRIFT", confidence_multiplier=0.5 if self.residual_active else 0.7,
                promotion_blocked=True, reason="active model-error drift",
            )
        if self.metric_active:
            return self._decision(
                status="METRIC_DRIFT", confidence_multiplier=0.8,
                promotion_blocked=bool(self.warmup_remaining),
                reason="active metric drift; model error drift not confirmed",
            )
        if self.warmup_remaining:
            return self._decision(
                status="WARMUP", confidence_multiplier=0.8,
                promotion_blocked=True, reason="post-drift warm-up is still running",
            )
        return self._decision(status="OK", promotion_blocked=False,
                              reason="no active ADWIN drift")

    def to_dict(self) -> dict:
        """Return JSON-safe state; no pickle or executable payload is used."""
        payload = {
            "version": self.version,
            "delta": self.delta,
            "min_samples": self.min_samples,
            "clear_consecutive": self.clear_consecutive,
            "warmup_samples": self.warmup_samples,
            "max_samples": self.max_samples,
            "samples": self.samples[-self.max_samples:],
            "metric_active": self.metric_active,
            "residual_active": self.residual_active,
            "mae_active": self.mae_active,
            "clear_streak": self.clear_streak,
            "warmup_remaining": self.warmup_remaining,
            "decision": asdict(self.decision()),
        }
        payload["checksum"] = hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> "AdwinPolicy":
        raw = json.loads(payload)
        if not isinstance(raw, dict) or raw.get("version") != ADWIN_POLICY_VERSION:
            raise ValueError("unsupported ADWIN policy state version")
        checksum = raw.pop("checksum", None)
        expected = hashlib.sha256(
            json.dumps(raw, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()
        if not isinstance(checksum, str) or checksum != expected:
            raise ValueError("ADWIN policy state checksum mismatch")
        samples = raw.get("samples")
        max_samples = int(raw.get("max_samples", MAX_REPLAY_SAMPLES))
        if (
            not isinstance(samples, list)
            or len(samples) > MAX_REPLAY_SAMPLES
            or max_samples < 32
            or max_samples > MAX_REPLAY_SAMPLES
        ):
            raise ValueError("invalid ADWIN sample window")
        policy = cls(
            delta=float(raw["delta"]),
            min_samples=int(raw["min_samples"]),
            clear_consecutive=int(raw["clear_consecutive"]),
            warmup_samples=int(raw["warmup_samples"]),
            max_samples=max_samples,
        )
        # Rebuild the River detector once after a process restart. Subsequent
        # watcher cycles only feed samples newer than the persisted cursor.
        for item in samples:
            policy.update(DriftSample(
                evaluated_at=datetime.fromisoformat(str(item["evaluated_at"])),
                metric_value=float(item["metric_value"]),
                residual=float(item["residual"]) if item.get("residual") is not None else None,
                mae=float(item["mae"]) if item.get("mae") is not None else None,
            ))
        policy.metric_active = bool(raw.get("metric_active", False))
        policy.residual_active = bool(raw.get("residual_active", False))
        policy.mae_active = bool(raw.get("mae_active", False))
        policy.clear_streak = int(raw.get("clear_streak", 0))
        policy.warmup_remaining = int(raw.get("warmup_remaining", 0))
        return policy


def compare_replay(samples: Iterable[DriftSample]) -> dict[str, object]:
    """Compare ADWIN with the pre-ADWIN fixed 3-sigma detector.

    This is intentionally read-only and bounded.  It is suitable for replay
    acceptance tests and does not touch model state or trigger promotion.
    """

    ordered = list(samples)[-MAX_REPLAY_SAMPLES:]
    adwin = AdwinPolicy(min_samples=10, warmup_samples=0)
    adwin_hits: list[int] = []
    legacy_hits: list[int] = []
    history: list[float] = []
    for index, sample in enumerate(ordered):
        result = adwin.update(sample)
        if result.metric_drift or result.residual_drift or result.mae_drift:
            adwin_hits.append(index)
        if len(history) >= 10:
            baseline = history[-32:]
            deviation = abs(sample.metric_value - mean(baseline))
            limit = max(3.0 * pstdev(baseline), 1.0)
            if deviation > limit:
                legacy_hits.append(index)
        history.append(sample.metric_value)
    return {
        "sample_count": len(ordered),
        "adwin_detection_count": len(adwin_hits),
        "legacy_detection_count": len(legacy_hits),
        "adwin_first_detection": adwin_hits[0] if adwin_hits else None,
        "legacy_first_detection": legacy_hits[0] if legacy_hits else None,
        "adwin_detection_indexes": adwin_hits,
        "legacy_detection_indexes": legacy_hits,
    }
