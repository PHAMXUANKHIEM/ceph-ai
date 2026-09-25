"""Fail-closed runtime gate for the future online-learning path.

The deterministic forecast pipeline has its own feature flags.  This module
protects only online model updates so introducing River cannot accidentally
turn a transient Watcher outage into a training event or a production model
change.  The gate is deliberately read-only: it computes a decision from
settings and the durable Watcher heartbeat, and callers decide whether to
persist anything.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from shared.time import utc_now

from config.settings import settings
from shared import heartbeat
from shared.forecast_scope import SCOPE_SCHEMA
from shared.models import ForecastModelPromotionAudit, ForecastModelRegistry

AUDIT_ONLY = "AUDIT_ONLY"
SHADOW_ONLY = "SHADOW_ONLY"
ACTIVE = "ACTIVE"
VALID_MODES = frozenset({AUDIT_ONLY, SHADOW_ONLY, ACTIVE})


def canary_scope_allows(
    cluster_id: str | None, host: str | None, metric: str | None,
) -> bool:
    """Return whether an identity is inside the explicitly configured canary.

    The default is permissive only while canary mode is disabled. Once it is
    enabled, every identity must match all three configured dimensions and a
    blank dimension fails closed.
    """
    if not settings.online_learning_canary_enabled:
        return True
    configured_cluster = str(settings.online_learning_canary_cluster_id or "").strip()
    configured_host = str(settings.online_learning_canary_host or "").strip()
    metrics = {
        item.strip().lower()
        for item in str(settings.online_learning_canary_metrics or "").split(",")
        if item.strip()
    }
    return bool(
        configured_cluster
        and configured_host
        and metrics
        and str(cluster_id or "").strip() == configured_cluster
        and str(host or "").strip() == configured_host
        and str(metric or "").strip().lower() in metrics
    )


@dataclass(frozen=True)
class LearningRuntimeDecision:
    enabled: bool
    mode: str
    can_observe: bool
    can_update_shadow: bool
    can_update_active: bool
    reason: str
    watcher_success: bool | None = None
    watcher_age_seconds: float | None = None
    watcher_failure_streak: int = 0
    checked_at: datetime | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class LearningTargetDecision:
    """Resolve the learner target from durable promotion state.

    ``shadow`` is the fail-closed default.  An ``active`` update is possible
    only for an exact, dimensioned registry row that has been operator-
    promoted and when the runtime gate itself is ACTIVE.  The online
    consumer must treat ``allowed=False`` as a hard stop because its durable
    state table is intentionally shared by the current learner version.
    """

    target: str
    allowed: bool
    registry_status: str | None
    registry_model_id: str | None
    reason: str

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_update_target(
    session,
    runtime: LearningRuntimeDecision,
    *,
    cluster_id: str | None,
    host: str | None,
    metric: str | None,
    algorithm: str,
    model_version: str,
) -> LearningTargetDecision:
    """Select ``shadow`` or ``active`` from exact registry state.

    Legacy or incomplete registry rows are deliberately ignored.  A row is
    exact only when all cluster/host/metric dimensions and the model identity
    match; this prevents a promotion for one stream leaking into another.
    """

    fallback_reason = "no exact promoted registry model; defaulting to shadow"
    if not runtime.enabled:
        return LearningTargetDecision(
            "shadow", False, None, None,
            f"runtime gate blocks updates: {runtime.reason}",
        )

    rows = session.query(ForecastModelRegistry).filter(
        ForecastModelRegistry.scope_type == "NODE_RESOURCE",
        ForecastModelRegistry.scope_schema == SCOPE_SCHEMA,
        ForecastModelRegistry.cluster_id == str(cluster_id or ""),
        ForecastModelRegistry.entity_type == "node",
        ForecastModelRegistry.entity_id == str(host or ""),
        ForecastModelRegistry.host == str(host or ""),
        ForecastModelRegistry.metric == str(metric or "").strip().lower(),
        ForecastModelRegistry.algorithm == str(algorithm or "").strip(),
        ForecastModelRegistry.version == str(model_version or "").strip(),
    ).all()
    exact_active = [row for row in rows if row.status == "ACTIVE"]
    if len(exact_active) > 1:
        return LearningTargetDecision(
            "shadow", False, "ACTIVE", None,
            "multiple exact ACTIVE registry rows; refusing to update",
        )
    active = exact_active[0] if exact_active else None
    if active is not None:
        promoted = session.query(ForecastModelPromotionAudit).filter_by(
            candidate_model_id=active.id,
            event_type="PROMOTED",
        ).first()
        if promoted is None:
            return LearningTargetDecision(
                "shadow", False, active.status, active.id,
                "ACTIVE registry row has no operator promotion audit",
            )
        if not runtime.can_update_active:
            return LearningTargetDecision(
                "shadow", False, active.status, active.id,
                "promoted model is active, but runtime mode is not ACTIVE",
            )
        return LearningTargetDecision(
            "active", True, active.status, active.id,
            "exact operator-promoted registry model selected",
        )

    if rows:
        row = rows[0]
        if row.status in {"BLOCKED", "RETIRED"}:
            return LearningTargetDecision(
                "shadow", False, row.status, row.id,
                f"exact registry model is {row.status}; refusing to update",
            )
        fallback_reason = f"exact registry model is {row.status}; defaulting to shadow"
    return LearningTargetDecision(
        "shadow", bool(runtime.can_update_shadow),
        rows[0].status if rows else None,
        rows[0].id if rows else None,
        fallback_reason if runtime.can_update_shadow else f"{fallback_reason}; runtime gate blocks shadow",
    )


def _blocked(mode: str, reason: str, *, checked_at: datetime | None = None,
             watcher_success: bool | None = None,
             watcher_age_seconds: float | None = None,
             watcher_failure_streak: int = 0) -> LearningRuntimeDecision:
    return LearningRuntimeDecision(
        enabled=False,
        mode=mode,
        can_observe=False,
        can_update_shadow=False,
        can_update_active=False,
        reason=reason,
        watcher_success=watcher_success,
        watcher_age_seconds=watcher_age_seconds,
        watcher_failure_streak=watcher_failure_streak,
        checked_at=checked_at,
    )


def evaluate(
    session, cluster_id: str | None, *, host: str | None = None,
    metric: str | None = None, now: datetime | None = None,
) -> LearningRuntimeDecision:
    """Return the current learning decision; never mutates the database."""

    checked_at = now or utc_now()
    if not settings.online_learning_enabled:
        return _blocked("DISABLED", "online learning feature flag is disabled", checked_at=checked_at)
    if settings.online_learning_kill_switch:
        return _blocked("KILL_SWITCH", "online learning kill switch is enabled", checked_at=checked_at)

    if not canary_scope_allows(cluster_id, host, metric):
        return _blocked(
            "CANARY_SCOPE",
            "sample is outside the configured online-learning canary scope",
            checked_at=checked_at,
        )

    mode = str(settings.online_learning_mode or "").strip().upper()
    if mode not in VALID_MODES:
        return _blocked(mode or "INVALID", "online learning mode is invalid", checked_at=checked_at)

    try:
        row = heartbeat.get_latest(session, cluster_id)
    except Exception:
        return _blocked(mode, "Watcher heartbeat is unavailable", checked_at=checked_at)
    if row is None:
        return _blocked(mode, "Watcher has no heartbeat yet", checked_at=checked_at)

    polled_at = row.polled_at
    age_seconds = max(0.0, (checked_at - polled_at).total_seconds())
    failure_streak = max(0, int(getattr(row, "consecutive_failures", 0) or 0))
    if age_seconds > settings.online_learning_watcher_staleness_seconds:
        return _blocked(
            mode, "Watcher heartbeat is stale", checked_at=checked_at,
            watcher_success=row.success, watcher_age_seconds=age_seconds,
            watcher_failure_streak=failure_streak,
        )
    if not row.success and failure_streak >= settings.online_learning_watcher_failure_threshold:
        return _blocked(
            mode, "Watcher has consecutive failures", checked_at=checked_at,
            watcher_success=row.success, watcher_age_seconds=age_seconds,
            watcher_failure_streak=failure_streak,
        )

    return LearningRuntimeDecision(
        enabled=True,
        mode=mode,
        can_observe=True,
        can_update_shadow=mode in {SHADOW_ONLY, ACTIVE},
        can_update_active=mode == ACTIVE,
        reason="runtime gate is healthy",
        watcher_success=row.success,
        watcher_age_seconds=age_seconds,
        watcher_failure_streak=failure_streak,
        checked_at=checked_at,
    )
