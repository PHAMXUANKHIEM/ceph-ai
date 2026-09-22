"""Explicit registry for online model identities used by shadow learning."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class OnlineModelRegistration:
    algorithm: str
    version: str
    feature_schema: str
    factory: Callable[..., Any]
    execution_mode: str = "SHADOW_ONLY"


def _river_linear_v2(**kwargs):
    from shared.river_linear_v2 import RiverLinearV2

    return RiverLinearV2(**kwargs)


ONLINE_MODEL_REGISTRY = {
    "river_linear_v2": OnlineModelRegistration(
        algorithm="river_linear_v2",
        version="river-linear-v2",
        feature_schema="resource-v2",
        factory=_river_linear_v2,
    ),
}


def get_online_model_registration(algorithm: str) -> OnlineModelRegistration:
    try:
        return ONLINE_MODEL_REGISTRY[str(algorithm).strip()]
    except KeyError as exc:
        raise ValueError(f"unknown online model algorithm: {algorithm!r}") from exc


def create_online_model(algorithm: str, **kwargs: Any):
    return get_online_model_registration(algorithm).factory(**kwargs)


def registry_snapshot() -> tuple[dict[str, str], ...]:
    return tuple({
        "algorithm": item.algorithm,
        "version": item.version,
        "feature_schema": item.feature_schema,
        "execution_mode": item.execution_mode,
    } for item in sorted(ONLINE_MODEL_REGISTRY.values(), key=lambda row: row.algorithm))
