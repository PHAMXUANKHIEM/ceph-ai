"""Bounded River SNARIMAX shadow adapter.

This module is deliberately independent from Ceph, SQLAlchemy and alerting.
It wraps River's forecaster API, which exposes ``learn_one`` and ``forecast``
but no ``predict_one`` or prediction interval.  The interval is estimated from
a bounded residual window and is evidence only: this adapter never promotes a
model or creates an alert.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping

from river import time_series
from river.utils import VectorDict

from shared.learning_safety import CircuitBreaker


ALGORITHM = "river_snarimax"
MODEL_VERSION = "river-snarimax-v1"
BACKEND_NAME = "river"
BACKEND_VERSION = "0.25.0"
FEATURE_SCHEMA = "snarimax-calendar-v1"
SNAPSHOT_SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 128 * 1024
MAX_RESIDUALS = 64
MAX_NORMALIZED_POINTS = 65_536
MIN_INTERVAL_RESIDUALS = 8
DEFAULT_INTERVAL_SECONDS = 3600


@dataclass(frozen=True)
class SnarimaxProfile:
    """Fixed, reviewed configuration for one supported metric scope."""

    metric: str
    p: int
    d: int
    q: int
    m: int
    sp: int
    sd: int
    sq: int
    horizon_steps: int
    min_samples: int
    expected_interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    exogenous_features: tuple[str, ...] = (
        "calendar_hour_sin",
        "calendar_hour_cos",
        "calendar_weekday_sin",
        "calendar_weekday_cos",
    )
    lower_bound: float | None = None
    upper_bound: float | None = None

    def validate(self) -> None:
        if self.metric not in {"cpu", "ram", "iops"}:
            raise ValueError("SNARIMAX metric profile is not supported")
        if not 0 <= self.p <= 4 or not 0 <= self.q <= 4:
            raise ValueError("SNARIMAX p/q must be bounded between 0 and 4")
        if not 0 <= self.d <= 1 or not 0 <= self.sd <= 1:
            raise ValueError("SNARIMAX d/sd must be bounded between 0 and 1")
        if not 2 <= self.m <= 168 or not 0 <= self.sp <= 1 or not 0 <= self.sq <= 1:
            raise ValueError("SNARIMAX seasonal parameters are outside the safe bound")
        if not 1 <= self.horizon_steps <= 24 or self.min_samples < self.m * max(1, self.sp) * 2:
            raise ValueError("SNARIMAX horizon/history profile is not eligible")
        if self.expected_interval_seconds <= 0:
            raise ValueError("SNARIMAX interval must be positive")


SNARIMAX_PROFILES: dict[str, SnarimaxProfile] = {
    # Hourly aggregation gives CPU/RAM a bounded daily seasonal lag. Calendar
    # terms are exogenous and are generated only for the observed/future time.
    "cpu": SnarimaxProfile("cpu", p=1, d=0, q=1, m=24, sp=1, sd=0, sq=0,
                            horizon_steps=1, min_samples=48, lower_bound=0, upper_bound=100),
    "ram": SnarimaxProfile("ram", p=1, d=0, q=1, m=24, sp=1, sd=0, sq=0,
                            horizon_steps=1, min_samples=48, lower_bound=0, upper_bound=100),
    "iops": SnarimaxProfile("iops", p=2, d=0, q=1, m=24, sp=1, sd=0, sq=0,
                             horizon_steps=1, min_samples=48),
}


def profile_for(metric: str) -> SnarimaxProfile:
    normalized = str(metric or "").strip().lower()
    if normalized in {"disk_iops", "disk-iops"}:
        normalized = "iops"
    try:
        profile = SNARIMAX_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(f"SNARIMAX does not support metric {metric!r}") from exc
    profile.validate()
    return profile


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def snapshot_checksum(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("SNARIMAX value must be finite")
    return numeric


def _exogenous(at: datetime, profile: SnarimaxProfile) -> dict[str, float]:
    when = _utc(at)
    hour = when.hour + when.minute / 60
    values = {
        "calendar_hour_sin": math.sin(2 * math.pi * hour / 24),
        "calendar_hour_cos": math.cos(2 * math.pi * hour / 24),
        "calendar_weekday_sin": math.sin(2 * math.pi * when.weekday() / 7),
        "calendar_weekday_cos": math.cos(2 * math.pi * when.weekday() / 7),
    }
    return {name: values[name] for name in profile.exogenous_features}


def normalize_points(
    points: Iterable[tuple[datetime, Any]],
    *,
    expected_interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
) -> tuple[list[tuple[datetime, float]], float, float]:
    """Normalize finite, ordered, deduplicated points and return gap/coverage."""
    by_time: dict[datetime, float] = {}
    for observed_at, value in points:
        try:
            timestamp = _utc(observed_at)
            numeric = _finite(value)
        except (AttributeError, TypeError, ValueError):
            continue
        by_time[timestamp] = numeric
        if len(by_time) > MAX_NORMALIZED_POINTS:
            raise ValueError("SNARIMAX input exceeds bounded point count")
    ordered = sorted(by_time.items())
    if len(ordered) < 2:
        return ordered, 0.0, 1.0 if ordered else 0.0
    gaps = [
        (right[0] - left[0]).total_seconds()
        for left, right in zip(ordered, ordered[1:])
    ]
    longest_gap = max(gaps)
    span = max(0.0, (ordered[-1][0] - ordered[0][0]).total_seconds())
    expected_count = max(1.0, span / max(1, expected_interval_seconds) + 1)
    coverage = min(1.0, len(ordered) / expected_count)
    return ordered, longest_gap, coverage


@dataclass(frozen=True)
class ShadowForecastResult:
    metric: str
    status: str
    reason: str
    current_value: float | None
    prediction: float | None
    predicted_low: float | None
    predicted_high: float | None
    predictions: tuple[float, ...]
    sample_count: int
    interval_sample_count: int
    observed_at: datetime | None
    training_duration_ms: float
    state_bytes: int
    coverage_ratio: float
    max_gap_seconds: float
    snapshot: dict[str, Any] | None = None

    @property
    def confidence(self) -> float:
        if self.status != "SHADOW_ONLY":
            return 0.0
        if self.interval_sample_count < MIN_INTERVAL_RESIDUALS:
            return 0.25
        return min(0.95, 0.5 + min(0.45, self.interval_sample_count / 128))

    def as_dict(self, *, include_snapshot: bool = False) -> dict[str, Any]:
        result = {
            "algorithm": ALGORITHM,
            "model_version": MODEL_VERSION,
            "feature_schema": FEATURE_SCHEMA,
            "metric": self.metric,
            "status": self.status,
            "reason": self.reason,
            "current_value": self.current_value,
            "prediction": self.prediction,
            "predicted_low": self.predicted_low,
            "predicted_high": self.predicted_high,
            "predictions": list(self.predictions),
            "sample_count": self.sample_count,
            "interval_sample_count": self.interval_sample_count,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "training_duration_ms": self.training_duration_ms,
            "state_bytes": self.state_bytes,
            "coverage_ratio": self.coverage_ratio,
            "max_gap_seconds": self.max_gap_seconds,
            "confidence": self.confidence,
            "execution_mode": "SHADOW_ONLY",
        }
        if include_snapshot:
            result["snapshot"] = self.snapshot
        return result


class SnarimaxTimeout(TimeoutError):
    pass


class SnarimaxShadowModel:
    """JSON-snapshot-friendly wrapper around River SNARIMAX."""

    def __init__(self, profile: SnarimaxProfile):
        profile.validate()
        self.profile = profile
        self._model = time_series.SNARIMAX(
            p=profile.p, d=profile.d, q=profile.q, m=profile.m,
            sp=profile.sp, sd=profile.sd, sq=profile.sq,
        )
        self._residuals: deque[float] = deque(maxlen=MAX_RESIDUALS)
        self._sample_count = 0
        self._last_observed_at: datetime | None = None

    @property
    def sample_count(self) -> int:
        return self._sample_count

    @property
    def last_observed_at(self) -> datetime | None:
        return self._last_observed_at

    def _check_deadline(self, deadline: float | None) -> None:
        if deadline is not None and time.monotonic() >= deadline:
            raise SnarimaxTimeout("SNARIMAX shadow training exceeded its budget")

    def _one_step_prediction(self, at: datetime) -> float | None:
        try:
            prediction = self._model.forecast(
                horizon=1, xs=[_exogenous(at, self.profile)],
            )[0]
            if prediction is None or not math.isfinite(float(prediction)):
                return None
            return float(prediction)
        except (IndexError, TypeError, ValueError, FloatingPointError):
            return None

    def _bounded_value(self, value: float) -> float:
        if self.profile.lower_bound is not None:
            value = max(self.profile.lower_bound, value)
        if self.profile.upper_bound is not None:
            value = min(self.profile.upper_bound, value)
        return value

    def _forecast(self, observed_at: datetime) -> tuple[float, ...]:
        future = [
            observed_at + timedelta(seconds=self.profile.expected_interval_seconds * step)
            for step in range(1, self.profile.horizon_steps + 1)
        ]
        predictions = self._model.forecast(
            horizon=self.profile.horizon_steps,
            xs=[_exogenous(at, self.profile) for at in future],
        )
        result = tuple(self._bounded_value(_finite(value)) for value in predictions)
        if not result:
            raise ValueError("SNARIMAX returned no forecast")
        return result

    def _interval(self, predictions: tuple[float, ...]) -> tuple[tuple[float, ...], tuple[float, ...]]:
        if len(self._residuals) < MIN_INTERVAL_RESIDUALS:
            return tuple(), tuple()
        residuals = list(self._residuals)
        center = statistics.fmean(residuals)
        scale = max(1.0, statistics.pstdev(residuals))
        lows = tuple(self._bounded_value(value + center - 1.96 * scale * math.sqrt(index + 1))
                     for index, value in enumerate(predictions))
        highs = tuple(self._bounded_value(value + center + 1.96 * scale * math.sqrt(index + 1))
                      for index, value in enumerate(predictions))
        return lows, highs

    def fit(
        self,
        points: Iterable[tuple[datetime, Any]],
        *,
        timeout_seconds: float,
        resume: bool = False,
    ) -> ShadowForecastResult:
        started = time.perf_counter()
        deadline = time.monotonic() + max(0.001, float(timeout_seconds))
        ordered, longest_gap, coverage = normalize_points(
            points, expected_interval_seconds=self.profile.expected_interval_seconds,
        )
        if len(ordered) < self.profile.min_samples:
            return self._result(
                "NOT_ELIGIBLE", "INSUFFICIENT_SEASONAL_HISTORY", None, (),
                ordered, longest_gap, coverage, started,
            )
        if longest_gap > self.profile.expected_interval_seconds * 2:
            return self._result(
                "NOT_ELIGIBLE", "GAP_DETECTED", None, (),
                ordered, longest_gap, coverage, started,
            )
        training = ordered
        if resume and self._last_observed_at is not None:
            training = [point for point in ordered if point[0] > self._last_observed_at]
        for observed_at, value in training:
            self._check_deadline(deadline)
            prediction = self._one_step_prediction(observed_at)
            if prediction is not None and self._sample_count >= self.profile.m:
                self._residuals.append(value - prediction)
            self._model.learn_one(value, x=_exogenous(observed_at, self.profile))
            self._sample_count += 1
            self._last_observed_at = observed_at
        self._check_deadline(deadline)
        observed_at = ordered[-1][0]
        predictions = self._forecast(observed_at)
        lows, highs = self._interval(predictions)
        snapshot = self.snapshot()
        return self._result(
            "SHADOW_ONLY", "shadow forecast generated", predictions[0], predictions,
            ordered, longest_gap, coverage, started, lows, highs, snapshot,
        )

    def _result(
        self,
        status: str,
        reason: str,
        prediction: float | None,
        predictions: tuple[float, ...],
        ordered: list[tuple[datetime, float]],
        longest_gap: float,
        coverage: float,
        started: float,
        lows: tuple[float, ...] = tuple(),
        highs: tuple[float, ...] = tuple(),
        snapshot: dict[str, Any] | None = None,
    ) -> ShadowForecastResult:
        state_bytes = 0
        if snapshot is not None:
            state_bytes = len(_canonical_json(snapshot).encode("utf-8"))
        return ShadowForecastResult(
            metric=self.profile.metric,
            status=status,
            reason=reason,
            current_value=ordered[-1][1] if ordered else None,
            prediction=prediction,
            predicted_low=lows[0] if lows else None,
            predicted_high=highs[0] if highs else None,
            predictions=predictions,
            sample_count=len(ordered),
            interval_sample_count=len(self._residuals),
            observed_at=ordered[-1][0] if ordered else None,
            training_duration_ms=round((time.perf_counter() - started) * 1000, 3),
            state_bytes=state_bytes,
            coverage_ratio=coverage,
            max_gap_seconds=longest_gap,
            snapshot=snapshot,
        )

    def _body(self) -> dict[str, Any]:
        scaler = self._model.regressor["StandardScaler"]
        regression = self._model.regressor["LinearRegression"]
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "algorithm": ALGORITHM,
            "version": MODEL_VERSION,
            "backend_name": BACKEND_NAME,
            "backend_version": BACKEND_VERSION,
            "feature_schema": FEATURE_SCHEMA,
            "profile": asdict(self.profile),
            "sample_count": self._sample_count,
            "last_observed_at": self._last_observed_at.isoformat() if self._last_observed_at else None,
            "residuals": list(self._residuals),
            "y_hist": list(self._model.y_hist),
            "y_diff": list(self._model.y_diff),
            "errors": list(self._model.errors),
            "scaler": {
                "counts": {str(key): int(value) for key, value in scaler.counts.items()},
                "means": {str(key): float(value) for key, value in scaler.means.items()},
                "vars": {str(key): float(value) for key, value in scaler.vars.items()},
            },
            "regression": {
                "intercept": float(regression.intercept),
                "weights": {str(key): float(value) for key, value in regression._weights.items()},
                "optimizer_iterations": int(regression.optimizer.n_iterations),
            },
        }

    def snapshot(self) -> dict[str, Any]:
        body = self._body()
        snapshot = {**body, "checksum": snapshot_checksum(body)}
        encoded = _canonical_json(snapshot).encode("utf-8")
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise ValueError("SNARIMAX snapshot exceeds bounded size")
        return snapshot

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "SnarimaxShadowModel":
        if not isinstance(snapshot, Mapping):
            raise ValueError("SNARIMAX snapshot must be an object")
        if len(_canonical_json(snapshot).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("SNARIMAX snapshot exceeds bounded size")
        payload = dict(snapshot)
        supplied = payload.pop("checksum", None)
        if not isinstance(supplied, str) or supplied != snapshot_checksum(payload):
            raise ValueError("SNARIMAX snapshot checksum mismatch")
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("SNARIMAX snapshot schema version mismatch")
        if payload.get("algorithm") != ALGORITHM or payload.get("version") != MODEL_VERSION:
            raise ValueError("SNARIMAX snapshot model version mismatch")
        raw_profile = payload.get("profile")
        if not isinstance(raw_profile, Mapping):
            raise ValueError("SNARIMAX snapshot profile is missing")
        profile = profile_for(str(raw_profile.get("metric") or ""))
        normalized_profile = dict(raw_profile)
        normalized_profile["exogenous_features"] = tuple(
            normalized_profile.get("exogenous_features") or ()
        )
        if dict(asdict(profile)) != normalized_profile:
            raise ValueError("SNARIMAX snapshot profile mismatch")
        learner = cls(profile)
        count = payload.get("sample_count")
        if not isinstance(count, int) or count < 0:
            raise ValueError("SNARIMAX snapshot sample count is invalid")

        def finite_list(name: str) -> list[float]:
            raw = payload.get(name, [])
            if not isinstance(raw, list) or len(raw) > MAX_RESIDUALS:
                raise ValueError(f"SNARIMAX snapshot {name} is invalid")
            values = [_finite(item) for item in raw]
            return values

        learner._sample_count = count
        learner._residuals = deque(finite_list("residuals"), maxlen=MAX_RESIDUALS)
        learner._model.y_hist.extend(finite_list("y_hist"))
        learner._model.y_diff.extend(finite_list("y_diff"))
        learner._model.errors.extend(finite_list("errors"))
        raw_last = payload.get("last_observed_at")
        if raw_last:
            learner._last_observed_at = _utc(datetime.fromisoformat(str(raw_last).replace("Z", "+00:00")))

        scaler_state = payload.get("scaler")
        regression_state = payload.get("regression")
        if not isinstance(scaler_state, Mapping) or not isinstance(regression_state, Mapping):
            raise ValueError("SNARIMAX snapshot state is incomplete")
        scaler = learner._model.regressor["StandardScaler"]
        regression = learner._model.regressor["LinearRegression"]
        scaler.counts = Counter({str(k): int(v) for k, v in (scaler_state.get("counts") or {}).items()})
        scaler.means = defaultdict(float, {str(k): _finite(v) for k, v in (scaler_state.get("means") or {}).items()})
        scaler.vars = defaultdict(float, {str(k): _finite(v) for k, v in (scaler_state.get("vars") or {}).items()})
        regression.intercept = _finite(regression_state.get("intercept", 0.0))
        regression._weights = VectorDict({str(k): _finite(v) for k, v in (regression_state.get("weights") or {}).items()})
        regression.optimizer.n_iterations = int(regression_state.get("optimizer_iterations", 0))
        if regression.optimizer.n_iterations < 0:
            raise ValueError("SNARIMAX snapshot optimizer state is invalid")
        return learner


def fallback_result(metric: str, value: float | None, reason: str) -> ShadowForecastResult:
    profile = profile_for(metric)
    numeric = None if value is None else _finite(value)
    return ShadowForecastResult(
        metric=profile.metric, status="FALLBACK", reason=reason,
        current_value=numeric,
        prediction=numeric, predicted_low=None, predicted_high=None,
        predictions=(numeric,) if numeric is not None else tuple(),
        sample_count=0, interval_sample_count=0, observed_at=None,
        training_duration_ms=0.0, state_bytes=0, coverage_ratio=0.0,
        max_gap_seconds=0.0, snapshot=None,
    )


class SnarimaxShadowRunner:
    """Bounded, per-scope shadow execution with circuit breaker/fallback."""

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 300):
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = max(1, int(failure_threshold))
        self._cooldown_seconds = max(1.0, float(cooldown_seconds))

    def _breaker(self, scope_key: str) -> CircuitBreaker:
        if scope_key not in self._breakers:
            self._breakers[scope_key] = CircuitBreaker(
                failure_threshold=self._failure_threshold,
                cooldown_seconds=self._cooldown_seconds,
            )
        return self._breakers[scope_key]

    def run(
        self,
        scope_key: str,
        metric: str,
        points: Iterable[tuple[datetime, Any]],
        *,
        fallback: float | None = None,
        timeout_seconds: float = 3.0,
        snapshot: Mapping[str, Any] | None = None,
    ) -> ShadowForecastResult:
        breaker = self._breaker(scope_key)
        if not breaker.allow():
            return fallback_result(metric, fallback, "CIRCUIT_OPEN")
        try:
            profile = profile_for(metric)
            model = (
                SnarimaxShadowModel.from_snapshot(snapshot)
                if snapshot is not None
                else SnarimaxShadowModel(profile)
            )
            result = model.fit(points, timeout_seconds=timeout_seconds, resume=snapshot is not None)
            if result.status == "SHADOW_ONLY":
                breaker.record_success()
                return result
            if result.reason in {"INSUFFICIENT_SEASONAL_HISTORY", "GAP_DETECTED"}:
                breaker.record_success()
                return result
            breaker.record_failure()
            return fallback_result(metric, fallback, result.reason)
        except SnarimaxTimeout:
            breaker.record_failure()
            return fallback_result(metric, fallback, "TIMEOUT")
        except (ImportError, TypeError, ValueError, KeyError, OverflowError, FloatingPointError) as exc:
            breaker.record_failure()
            return fallback_result(metric, fallback, f"SNARIMAX_ERROR:{type(exc).__name__}")

    def reset(self, scope_key: str) -> None:
        self._breakers.pop(scope_key, None)
