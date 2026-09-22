"""River StandardScaler + LinearRegression adapter for shadow learning.

The adapter is intentionally independent from Ceph, SQLAlchemy and promotion.
It only accepts verified outcomes, emits JSON-only state, and fails closed on
checksum/version/schema drift.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping

from river import linear_model, preprocessing
from river.utils import VectorDict

from shared.online_model_backend import BackendMetadata, OnlineModelBackend


ALGORITHM = "river_linear_v2"
MODEL_VERSION = "river-linear-v2"
BACKEND_NAME = "river"
BACKEND_VERSION = "0.25.0"
DEFAULT_FEATURE_SCHEMA = "resource-v2"
SNAPSHOT_SCHEMA_VERSION = 1
MAX_FEATURES = 128
MAX_SNAPSHOT_BYTES = 64 * 1024
VERIFIED_OUTCOMES = frozenset({"VERIFIED_SUCCESS", "VERIFIED_FAILED"})


@dataclass(frozen=True)
class LinearScore:
    prediction: float
    absolute_error: float


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _checksum(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class RiverLinearV2(OnlineModelBackend):
    """Bounded online linear model with an explicit verified-label gate."""

    algorithm = ALGORITHM
    version = MODEL_VERSION
    backend_name = BACKEND_NAME
    backend_version = BACKEND_VERSION

    def __init__(
        self,
        *,
        feature_names: tuple[str, ...] = ("current", "lag_1", "rolling_mean_6"),
        feature_schema: str = DEFAULT_FEATURE_SCHEMA,
    ) -> None:
        normalized = tuple(str(name).strip() for name in feature_names)
        if not normalized or len(normalized) > MAX_FEATURES or any(not name for name in normalized):
            raise ValueError("river_linear_v2 feature schema is invalid")
        if len(set(normalized)) != len(normalized):
            raise ValueError("river_linear_v2 feature names must be unique")
        self.feature_names = normalized
        self.feature_schema = str(feature_schema).strip()
        if not self.feature_schema:
            raise ValueError("river_linear_v2 feature schema is required")
        self._model = preprocessing.StandardScaler() | linear_model.LinearRegression()
        self._sample_count = 0

    @property
    def sample_count(self) -> int:
        return self._sample_count

    def _features(self, features: Mapping[str, Any]) -> dict[str, float]:
        if not isinstance(features, Mapping):
            raise ValueError("river_linear_v2 features must be an object")
        if set(features) != set(self.feature_names):
            raise ValueError("river_linear_v2 feature keys do not match feature_schema")
        normalized: dict[str, float] = {}
        for name in self.feature_names:
            value = features[name]
            if isinstance(value, bool):
                raise ValueError("river_linear_v2 feature values must be finite numbers")
            try:
                numeric = float(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("river_linear_v2 feature values must be finite numbers") from exc
            if not math.isfinite(numeric):
                raise ValueError("river_linear_v2 feature values must be finite numbers")
            normalized[name] = numeric
        return normalized

    @staticmethod
    def _target(value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("river_linear_v2 target must be finite") from exc
        if not math.isfinite(numeric):
            raise ValueError("river_linear_v2 target must be finite")
        return numeric

    def predict_one(
        self, features: Mapping[str, Any], fallback: float | None = None,
    ) -> float | None:
        normalized = self._features(features)
        if self.sample_count == 0:
            return fallback
        prediction = self._model.predict_one(normalized)
        if prediction is None or not math.isfinite(float(prediction)):
            return fallback
        return float(prediction)

    def learn_one(
        self, features: Mapping[str, Any], value: Any, *, outcome: str | None = None,
    ) -> "RiverLinearV2":
        """Learn only from a persisted, verified forecast outcome."""
        if outcome not in VERIFIED_OUTCOMES:
            raise ValueError("river_linear_v2 accepts only verified outcomes")
        normalized = self._features(features)
        target = self._target(value)
        self._model.learn_one(normalized, target)
        self._sample_count += 1
        return self

    def learn_verified_one(
        self, features: Mapping[str, Any], value: Any, *, outcome: str,
    ) -> "RiverLinearV2":
        return self.learn_one(features, value, outcome=outcome)

    def score_one(self, features: Mapping[str, Any], actual: Any) -> LinearScore:
        prediction = self.predict_one(features)
        if prediction is None:
            raise ValueError("river_linear_v2 cannot score before its first verified sample")
        target = self._target(actual)
        return LinearScore(prediction=prediction, absolute_error=abs(prediction - target))

    def metadata(self) -> BackendMetadata:
        return BackendMetadata(
            backend_name=self.backend_name,
            backend_version=self.backend_version,
            algorithm=self.algorithm,
            model_version=self.version,
            feature_schema=self.feature_schema,
        )

    def _body(self) -> dict[str, Any]:
        scaler = self._model["StandardScaler"]
        regression = self._model["LinearRegression"]
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "algorithm": self.algorithm,
            "version": self.version,
            "backend_name": self.backend_name,
            "backend_version": self.backend_version,
            "feature_schema": self.feature_schema,
            "feature_names": list(self.feature_names),
            "sample_count": self.sample_count,
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
        encoded_size = len(_canonical_json(body).encode("utf-8"))
        if encoded_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("river_linear_v2 snapshot exceeds bounded size")
        return {**body, "checksum": _checksum(body)}

    def resource_cost(self) -> dict[str, float]:
        return {"state_bytes": float(len(_canonical_json(self.snapshot()).encode("utf-8")))}

    def resource_usage(self) -> dict[str, float]:
        """Compatibility name used by the v2 model contract."""
        return self.resource_cost()

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> "RiverLinearV2":
        if not isinstance(snapshot, Mapping):
            raise ValueError("river_linear_v2 snapshot must be an object")
        if len(_canonical_json(snapshot).encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError("river_linear_v2 snapshot exceeds bounded size")
        payload = dict(snapshot)
        supplied = payload.pop("checksum", None)
        if not isinstance(supplied, str) or supplied != _checksum(payload):
            raise ValueError("river_linear_v2 snapshot checksum mismatch")
        if payload.get("schema_version") != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError("river_linear_v2 snapshot schema version mismatch")
        if payload.get("algorithm") != ALGORITHM or payload.get("version") != MODEL_VERSION:
            raise ValueError("river_linear_v2 snapshot model version mismatch")
        names = payload.get("feature_names")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("river_linear_v2 snapshot feature schema is invalid")
        learner = cls(feature_names=tuple(names), feature_schema=str(payload.get("feature_schema") or ""))
        count = payload.get("sample_count")
        if not isinstance(count, int) or count < 0:
            raise ValueError("river_linear_v2 snapshot sample count is invalid")
        scaler_state = payload.get("scaler")
        regression_state = payload.get("regression")
        if not isinstance(scaler_state, Mapping) or not isinstance(regression_state, Mapping):
            raise ValueError("river_linear_v2 snapshot state is incomplete")
        if set(scaler_state.get("means", {})) - set(names) or set(regression_state.get("weights", {})) - set(names):
            raise ValueError("river_linear_v2 snapshot contains unknown feature state")
        scaler = learner._model["StandardScaler"]
        scaler.counts = Counter({str(k): int(v) for k, v in (scaler_state.get("counts") or {}).items()})
        scaler.means = defaultdict(float, {str(k): float(v) for k, v in (scaler_state.get("means") or {}).items()})
        scaler.vars = defaultdict(float, {str(k): float(v) for k, v in (scaler_state.get("vars") or {}).items()})
        regression = learner._model["LinearRegression"]
        regression.intercept = float(regression_state.get("intercept", 0.0))
        regression._weights = VectorDict({str(k): float(v) for k, v in (regression_state.get("weights") or {}).items()})
        regression.optimizer.n_iterations = int(regression_state.get("optimizer_iterations", 0))
        if not math.isfinite(regression.intercept) or regression.optimizer.n_iterations < 0:
            raise ValueError("river_linear_v2 snapshot regression state is invalid")
        learner._sample_count = count
        return learner

    def restore(self, snapshot: Mapping[str, Any]) -> "RiverLinearV2":
        """Restore validated state into this adapter without partial mutation."""
        restored = type(self).from_snapshot(snapshot)
        self.feature_names = restored.feature_names
        self.feature_schema = restored.feature_schema
        self._model = restored._model
        self._sample_count = restored._sample_count
        return self
