"""Canonical, fail-closed identity for forecast model evidence.

The registry historically stored a compact pipe-delimited ``scope_key``.  This
module keeps that value for compatibility while exposing the dimensions that
must never be accidentally merged: cluster, entity, host, metric and horizon.
It is deliberately pure and does not read the database or infer missing
legacy dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass


UNKNOWN_SCOPE = "UNKNOWN_SCOPE"
MISSING_HOST_METRIC = "MISSING_HOST_METRIC"
SCOPE_SCHEMA = "forecast-scope-v2"


@dataclass(frozen=True)
class ForecastScope:
    cluster_id: str
    entity_type: str
    entity_id: str
    metric: str
    horizon_hours: int
    host: str | None = None

    def __post_init__(self) -> None:
        for name in ("cluster_id", "entity_type", "entity_id", "metric"):
            value = str(getattr(self, name) or "").strip()
            if not value or "|" in value:
                raise ValueError(f"{name} must be non-empty and cannot contain '|'")
        if int(self.horizon_hours) <= 0:
            raise ValueError("horizon_hours must be positive")
        if self.host is not None and (not str(self.host).strip() or "|" in str(self.host)):
            raise ValueError("host must be non-empty and cannot contain '|' when supplied")

    @property
    def canonical_key(self) -> str:
        parts = [
            f"cluster={self.cluster_id.strip()}",
            f"entity_type={self.entity_type.strip().lower()}",
            f"entity_id={self.entity_id.strip()}",
            f"host={(self.host or '').strip()}",
            f"metric={self.metric.strip().lower()}",
            f"horizon={int(self.horizon_hours)}h",
        ]
        return "|".join(parts)


def parse_legacy_scope(scope_type: str, scope_key: str, *, horizon_hours: int | None = None) -> ForecastScope | None:
    """Parse only known legacy keys; return ``None`` instead of guessing."""

    parts = [item.strip() for item in str(scope_key or "").split("|")]
    kind = str(scope_type or "").strip().upper()
    if kind == "NODE_RESOURCE" and len(parts) == 3:
        cluster, host, metric = parts
        if cluster and host and metric and horizon_hours:
            return ForecastScope(cluster, "node", host, metric, int(horizon_hours), host=host)
    if kind == "VOLUME" and len(parts) == 4:
        cluster, pool, image, metric = parts
        if cluster and pool and image and metric and horizon_hours:
            return ForecastScope(cluster, "volume", f"{pool}/{image}", metric, int(horizon_hours))
    return None


def validate_scope_dimensions(*, scope: ForecastScope | None, scope_schema: str | None) -> tuple[bool, str | None]:
    """Return a promotion-safe decision for registry dimensions."""

    if scope is None:
        return False, UNKNOWN_SCOPE
    if str(scope_schema or "").strip() != SCOPE_SCHEMA:
        return False, UNKNOWN_SCOPE
    if scope.entity_type == "node" and not scope.host:
        return False, MISSING_HOST_METRIC
    if not scope.metric:
        return False, MISSING_HOST_METRIC
    return True, None
