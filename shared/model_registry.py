"""Safe metadata registry for forecast model versions.

The registry records lifecycle intent only.  It does not select a model,
send an alert, or enable remediation; those decisions require the guarded
promotion workflow in a later phase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from shared.time import utc_now

from config.settings import settings
from shared.models import (
    ForecastModelEvaluation,
    ForecastModelPromotionAudit,
    ForecastModelRegistry,
    NodeResourceModelState,
    VolumeModelState,
)
from shared.forecast_scope import ForecastScope, SCOPE_SCHEMA, parse_legacy_scope

MODEL_STATUSES = {"CANDIDATE", "SHADOW", "ACTIVE", "RETIRED", "BLOCKED"}
REGISTRY_SCOPE_TYPES = {"NODE_RESOURCE", "VOLUME"}
PROMOTION_REQUESTED = "PROMOTION_REQUESTED"
PROMOTION_BLOCKED = "PROMOTION_BLOCKED"
PROMOTED = "PROMOTED"
ROLLED_BACK = "ROLLED_BACK"
ROLLBACK_BLOCKED = "ROLLBACK_BLOCKED"


@dataclass(frozen=True)
class PromotionPolicy:
    """Explicit, conservative gates for an operator-approved promotion."""

    minimum_outcomes: int = 20
    required_consecutive_evaluations: int = 3
    max_false_positive_rate_increase: float = 0.0
    minimum_mae_improvement: float = 0.0
    minimum_smape_improvement: float = 0.0
    max_poll_latency_ms: float = 5000.0
    max_drift_score: float = 0.0


@dataclass(frozen=True)
class PromotionDecision:
    allowed: bool
    status: str
    reason: str
    checks: dict


def default_promotion_policy() -> PromotionPolicy:
    return PromotionPolicy(
        minimum_outcomes=max(1, int(settings.forecast_promotion_min_outcomes)),
        required_consecutive_evaluations=max(
            1, int(settings.forecast_promotion_required_evaluations)
        ),
        max_false_positive_rate_increase=max(
            0.0, float(settings.forecast_promotion_max_false_positive_rate_increase)
        ),
        minimum_mae_improvement=max(0.0, float(settings.forecast_promotion_min_mae_improvement)),
        minimum_smape_improvement=max(0.0, float(settings.forecast_promotion_min_smape_improvement)),
        max_poll_latency_ms=max(1.0, float(settings.forecast_promotion_max_poll_latency_ms)),
        max_drift_score=max(0.0, float(settings.forecast_promotion_max_drift_score)),
    )


def _promotion_evidence(row) -> dict:
    raw = getattr(row, "evidence_json", None)
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"_invalid": True}
    return payload if isinstance(payload, dict) else {"_invalid": True}


def _resource_budget_ok(payload: dict, policy: PromotionPolicy) -> bool:
    if payload.get("_invalid") is True or payload.get("resource_budget_ok", True) is False:
        return False
    raw_latency = payload.get("poll_latency_ms")
    if raw_latency is None:
        return True
    try:
        return float(raw_latency) <= policy.max_poll_latency_ms
    except (TypeError, ValueError):
        return False


def _drift_guard_ok(payload: dict, policy: PromotionPolicy) -> bool:
    if payload.get("_invalid") is True or payload.get("candidate_drift_status") == "DRIFT":
        return False
    try:
        return float(payload.get("candidate_drift_score", 0.0) or 0.0) <= policy.max_drift_score
    except (TypeError, ValueError):
        return False


def evaluate_guarded_promotion(
    evaluations: list, *, policy: PromotionPolicy | None = None,
) -> PromotionDecision:
    """Evaluate recent consecutive evidence without changing any state.

    Every evaluation in the required streak must satisfy every gate.  Missing
    metrics fail closed; volume evaluations have no meaningful CPU-style
    false-positive rate, so two missing rates are treated as not applicable.
    """
    policy = policy or default_promotion_policy()
    ordered = sorted(evaluations, key=lambda row: row.target_at)
    recent = ordered[-policy.required_consecutive_evaluations:]
    checks = {
        "minimum_outcomes": False,
        "consecutive_evaluations": len(recent) >= policy.required_consecutive_evaluations,
        "mae_improved": False,
        "smape_improved": False,
        "false_positive_rate_guard": False,
        "resource_budget_guard": False,
        "drift_guard": False,
    }
    if not checks["consecutive_evaluations"]:
        return PromotionDecision(
            False, PROMOTION_BLOCKED,
            f"cần {policy.required_consecutive_evaluations} evaluation liên tiếp, hiện có {len(recent)}",
            checks,
        )

    checks["minimum_outcomes"] = all(
        min(int(row.active_evaluated), int(row.candidate_evaluated)) >= policy.minimum_outcomes
        for row in recent
    )
    checks["mae_improved"] = all(
        row.active_mae is not None and row.candidate_mae is not None
        and row.candidate_mae <= row.active_mae - policy.minimum_mae_improvement
        for row in recent
    )
    checks["smape_improved"] = all(
        row.active_smape is not None and row.candidate_smape is not None
        and row.candidate_smape <= row.active_smape - policy.minimum_smape_improvement
        for row in recent
    )
    checks["false_positive_rate_guard"] = all(
        (
            row.active_false_positive_rate is None
            and row.candidate_false_positive_rate is None
        )
        or (
            row.active_false_positive_rate is not None
            and row.candidate_false_positive_rate is not None
            and row.candidate_false_positive_rate
            <= row.active_false_positive_rate + policy.max_false_positive_rate_increase
        )
        for row in recent
    )
    evidence = [_promotion_evidence(row) for row in recent]
    checks["resource_budget_guard"] = all(
        _resource_budget_ok(payload, policy)
        for payload in evidence
    )
    checks["drift_guard"] = all(
        _drift_guard_ok(payload, policy)
        for payload in evidence
    )
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        return PromotionDecision(
            False, PROMOTION_BLOCKED,
            "guard bị chặn: " + ", ".join(failed), checks,
        )
    return PromotionDecision(
        True, PROMOTION_REQUESTED,
        "candidate đạt đủ dữ liệu, MAE/SMAPE tốt hơn liên tục và không tăng false-positive rate",
        checks,
    )


def _scope_model_identity(comparison) -> tuple[str, str, str, str]:
    if comparison.scope_type not in REGISTRY_SCOPE_TYPES or not comparison.scope_key:
        raise ValueError("shadow comparison thiếu scope registry")
    if comparison.scope_type == "NODE_RESOURCE":
        name, schema = "node-resource-forecast", "node-resource-v1"
    else:
        name, schema = "volume-forecast", "volume-v1"
    return comparison.scope_type, comparison.scope_key, name, schema


def _comparison_scope(comparison) -> ForecastScope | None:
    """Parse a comparison scope without inventing missing dimensions."""

    horizon = getattr(comparison, "horizon_hours", None) or 1
    return parse_legacy_scope(
        comparison.scope_type, comparison.scope_key, horizon_hours=int(horizon),
    )


def _scope_ready(row: ForecastModelRegistry) -> bool:
    """Promotion requires explicit, migrated dimensions; never guess legacy scope."""

    return bool(
        row.scope_schema == SCOPE_SCHEMA
        and row.cluster_id
        and row.entity_type
        and row.entity_id
        and row.metric
        and row.horizon_hours
        and (row.entity_type != "node" or row.host)
    )


def _version(algorithm: str, window_hours: int) -> str:
    return f"{algorithm}:{int(window_hours)}h"[:32]


def ensure_shadow_model_pair(session, comparison, *, now: datetime | None = None) -> tuple[
    ForecastModelRegistry, ForecastModelRegistry
]:
    """Register the current runtime active baseline and a shadow candidate.

    This is bootstrap-only: once a scope has a registry ACTIVE row, a
    different runtime active identity is rejected rather than silently
    promoted.  Only the explicit approval path may change selected state.
    """
    scope_type, scope_key, name, schema = _scope_model_identity(comparison)
    when = now or utc_now()
    active = register_candidate(
        session, scope_type=scope_type, scope_key=scope_key, name=name,
        version=_version(comparison.active_algorithm, comparison.active_window_hours),
        algorithm=comparison.active_algorithm, feature_schema=schema,
        training_window_hours=comparison.active_window_hours, scope=_comparison_scope(comparison), now=when,
    )
    current_active = session.query(ForecastModelRegistry).filter_by(
        scope_type=scope_type, scope_key=scope_key, status="ACTIVE",
    ).one_or_none()
    if current_active is None:
        set_status(session, active, status="ACTIVE", reason="initial guarded-promotion baseline", now=when)
        current_active = active
    elif current_active.id != active.id:
        raise ValueError("runtime active model differs from registry ACTIVE model")

    candidate = register_candidate(
        session, scope_type=scope_type, scope_key=scope_key, name=name,
        version=_version(comparison.candidate_algorithm, comparison.candidate_window_hours),
        algorithm=comparison.candidate_algorithm, feature_schema=schema,
        training_window_hours=comparison.candidate_window_hours, scope=_comparison_scope(comparison), now=when,
    )
    if candidate.status == "CANDIDATE":
        set_status(session, candidate, status="SHADOW", reason="persisted shadow evaluation", now=when)
    return current_active, candidate


def record_shadow_evaluation(session, *, active_model, candidate_model, comparison,
                             now: datetime | None = None) -> ForecastModelEvaluation | None:
    """Persist one new target timestamp; repeated worker polls are idempotent."""
    target_at = comparison.latest_target_at
    if target_at is None:
        return None
    existing = session.query(ForecastModelEvaluation).filter_by(
        candidate_model_id=candidate_model.id,
        active_model_id=active_model.id,
        target_at=target_at,
    ).one_or_none()
    if existing is not None:
        return None
    evidence = {
        "execution_mode": comparison.execution_mode,
        "active_algorithm": comparison.active_algorithm,
        "candidate_algorithm": comparison.candidate_algorithm,
        "status": comparison.status,
        "candidate_drift_status": getattr(comparison, "candidate_drift_status", None),
        "candidate_drift_score": getattr(comparison, "candidate_drift_score", 0.0),
        "resource_budget_ok": getattr(comparison, "resource_budget_ok", True),
        "poll_latency_ms": getattr(comparison, "poll_latency_ms", None),
    }
    row = ForecastModelEvaluation(
        candidate_model_id=candidate_model.id,
        active_model_id=active_model.id,
        target_at=target_at,
        evaluated_at=now or utc_now(),
        active_evaluated=comparison.active_evaluated,
        candidate_evaluated=comparison.candidate_evaluated,
        active_mae=comparison.active_mae, candidate_mae=comparison.candidate_mae,
        active_rmse=comparison.active_rmse, candidate_rmse=comparison.candidate_rmse,
        active_smape=comparison.active_smape, candidate_smape=comparison.candidate_smape,
        active_bias=comparison.active_bias, candidate_bias=comparison.candidate_bias,
        active_false_positive_rate=comparison.active_false_positive_rate,
        candidate_false_positive_rate=comparison.candidate_false_positive_rate,
        status=comparison.status, reason=comparison.reason,
        evidence_json=json.dumps(evidence, ensure_ascii=False, sort_keys=True),
    )
    session.add(row)
    session.flush()
    return row


def _audit(session, *, candidate_model_id: str, previous_active_model_id: str | None,
           event_type: str, actor: str, reason: str, evidence: dict | None,
           now: datetime | None = None) -> ForecastModelPromotionAudit:
    row = ForecastModelPromotionAudit(
        candidate_model_id=candidate_model_id,
        previous_active_model_id=previous_active_model_id,
        event_type=event_type,
        actor=(actor or "").strip() or "unknown",
        reason=reason,
        evidence_json=json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=str),
        created_at=now or utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def _select_runtime_state(session, row: ForecastModelRegistry, *, selected: bool) -> None:
    if row.scope_type == "NODE_RESOURCE":
        parts = row.scope_key.split("|", 2)
        if len(parts) != 3:
            raise ValueError("invalid NODE_RESOURCE registry scope key")
        cluster_name, host, metric = parts
        states = session.query(NodeResourceModelState).filter_by(
            cluster_name=cluster_name, host=host, metric=metric,
        ).all()
    elif row.scope_type == "VOLUME":
        parts = row.scope_key.split("|", 3)
        if len(parts) != 4:
            raise ValueError("invalid VOLUME registry scope key")
        cluster_id, pool, image, metric = parts
        states = session.query(VolumeModelState).filter_by(
            cluster_id=cluster_id, pool=pool, image=image, metric=metric,
        ).all()
    else:
        raise ValueError("unsupported registry scope type")
    target = next(
        (state for state in states
         if state.algorithm == row.algorithm and state.window_hours == row.training_window_hours),
        None,
    )
    if target is None:
        raise ValueError("registry model has no matching runtime model state")
    for state in states:
        state.selected = bool(selected and state.id == target.id)


def _latest_evaluations(session, candidate: ForecastModelRegistry,
                        active: ForecastModelRegistry) -> list[ForecastModelEvaluation]:
    return session.query(ForecastModelEvaluation).filter_by(
        candidate_model_id=candidate.id, active_model_id=active.id,
    ).order_by(ForecastModelEvaluation.target_at).all()


def request_promotion(session, *, candidate_id: str, actor: str,
                      policy: PromotionPolicy | None = None,
                      now: datetime | None = None) -> PromotionDecision:
    """Create a promotion request only when every guard passes.

    The candidate remains SHADOW until ``approve_promotion`` is called by a
    logged-in operator.  A blocked request is audited but does not poison the
    candidate with a permanent BLOCKED status.
    """
    candidate = session.get(ForecastModelRegistry, candidate_id)
    if candidate is None:
        raise ValueError("candidate model does not exist")
    active = session.query(ForecastModelRegistry).filter_by(
        scope_type=candidate.scope_type, scope_key=candidate.scope_key, status="ACTIVE",
    ).one_or_none()
    if active is None:
        raise ValueError("scope has no active model")
    if not _scope_ready(candidate):
        reason = "promotion blocked: model registry scope is UNKNOWN_SCOPE or missing dimensions"
        _audit(
            session, candidate_model_id=candidate.id, previous_active_model_id=active.id,
            event_type=PROMOTION_BLOCKED, actor=actor, reason=reason,
            evidence={"scope_schema": candidate.scope_schema}, now=now,
        )
        return PromotionDecision(False, PROMOTION_BLOCKED, reason, {"scope_dimensions": False})
    decision = evaluate_guarded_promotion(_latest_evaluations(session, candidate, active), policy=policy)
    _audit(
        session, candidate_model_id=candidate.id, previous_active_model_id=active.id,
        event_type=decision.status, actor=actor, reason=decision.reason,
        evidence={"checks": decision.checks}, now=now,
    )
    return decision


def block_candidate(session, *, candidate_id: str, actor: str, reason: str,
                    now: datetime | None = None) -> ForecastModelRegistry:
    """Block a shadow candidate explicitly without touching the active model."""

    candidate = session.get(ForecastModelRegistry, candidate_id)
    if candidate is None:
        raise ValueError("candidate model does not exist")
    if candidate.status not in {"CANDIDATE", "SHADOW"}:
        raise ValueError("only a CANDIDATE or SHADOW model can be blocked")
    if not (reason or "").strip():
        raise ValueError("blocking reason is required")
    active = session.query(ForecastModelRegistry).filter_by(
        scope_type=candidate.scope_type, scope_key=candidate.scope_key, status="ACTIVE",
    ).one_or_none()
    set_status(session, candidate, status="BLOCKED", reason=reason, now=now)
    _audit(
        session,
        candidate_model_id=candidate.id,
        previous_active_model_id=active.id if active else None,
        event_type=PROMOTION_BLOCKED,
        actor=actor,
        reason=reason.strip(),
        evidence={"operator_block": True},
        now=now,
    )
    return candidate


def approve_promotion(session, *, candidate_id: str, actor: str,
                      policy: PromotionPolicy | None = None,
                      now: datetime | None = None) -> ForecastModelRegistry:
    """Promote only after a fresh guard evaluation and explicit operator approval."""
    candidate = session.get(ForecastModelRegistry, candidate_id)
    if candidate is None:
        raise ValueError("candidate model does not exist")
    if candidate.status not in {"CANDIDATE", "SHADOW"}:
        raise ValueError("only a CANDIDATE or SHADOW model can be promoted")
    active = session.query(ForecastModelRegistry).filter_by(
        scope_type=candidate.scope_type, scope_key=candidate.scope_key, status="ACTIVE",
    ).one_or_none()
    if active is None:
        raise ValueError("scope has no active model")
    if not _scope_ready(candidate):
        reason = "promotion blocked: model registry scope is UNKNOWN_SCOPE or missing dimensions"
        _audit(
            session, candidate_model_id=candidate.id, previous_active_model_id=active.id,
            event_type=PROMOTION_BLOCKED, actor=actor, reason=reason,
            evidence={"scope_schema": candidate.scope_schema}, now=now,
        )
        raise ValueError(reason)
    decision = evaluate_guarded_promotion(_latest_evaluations(session, candidate, active), policy=policy)
    if not decision.allowed:
        _audit(
            session, candidate_model_id=candidate.id, previous_active_model_id=active.id,
            event_type=PROMOTION_BLOCKED, actor=actor, reason=decision.reason,
            evidence={"checks": decision.checks}, now=now,
        )
        raise ValueError(decision.reason)
    try:
        _select_runtime_state(session, candidate, selected=True)
    except ValueError as exc:
        _audit(
            session, candidate_model_id=candidate.id, previous_active_model_id=active.id,
            event_type=PROMOTION_BLOCKED, actor=actor, reason=str(exc), evidence={}, now=now,
        )
        raise
    set_status(session, active, status="RETIRED", reason=f"replaced by {candidate.version}", now=now)
    set_status(session, candidate, status="ACTIVE", reason="operator-approved guarded promotion", now=now)
    _audit(
        session, candidate_model_id=candidate.id, previous_active_model_id=active.id,
        event_type=PROMOTED, actor=actor, reason=decision.reason,
        evidence={"checks": decision.checks}, now=now,
    )
    return candidate


def rollback_promotion(session, *, candidate_id: str, actor: str,
                       now: datetime | None = None) -> ForecastModelRegistry:
    """Restore the exact previous active model recorded by the last promotion."""
    candidate = session.get(ForecastModelRegistry, candidate_id)
    if candidate is None:
        raise ValueError("model does not exist")
    if candidate.status != "ACTIVE":
        raise ValueError("only the current ACTIVE model can be rolled back")
    audit = session.query(ForecastModelPromotionAudit).filter_by(
        candidate_model_id=candidate.id, event_type=PROMOTED,
    ).order_by(ForecastModelPromotionAudit.created_at.desc()).first()
    previous = session.get(ForecastModelRegistry, audit.previous_active_model_id) if audit else None
    if previous is None or previous.status != "RETIRED":
        _audit(
            session, candidate_model_id=candidate.id,
            previous_active_model_id=previous.id if previous else None,
            event_type=ROLLBACK_BLOCKED, actor=actor,
            reason="không tìm thấy active model trước đó để rollback", evidence={}, now=now,
        )
        raise ValueError("previous active model is unavailable")
    _select_runtime_state(session, previous, selected=True)
    set_status(session, candidate, status="RETIRED", reason=f"rollback về {previous.version}", now=now)
    set_status(session, previous, status="ACTIVE", reason="operator-approved rollback", now=now)
    _audit(
        session, candidate_model_id=candidate.id, previous_active_model_id=previous.id,
        event_type=ROLLED_BACK, actor=actor,
        reason=f"rollback về model {previous.version}", evidence={}, now=now,
    )
    return previous


def register_candidate(
    session, *, scope_type: str, scope_key: str, name: str, version: str,
    algorithm: str, feature_schema: str, training_window_hours: int,
    scope: ForecastScope | None = None,
    now: datetime | None = None,
) -> ForecastModelRegistry:
    """Create or return an immutable-identity candidate registration."""
    values = {
        "scope_type": (scope_type or "").strip().upper(),
        "scope_key": (scope_key or "").strip(),
        "name": (name or "").strip(),
        "version": (version or "").strip(),
        "algorithm": (algorithm or "").strip(),
        "feature_schema": (feature_schema or "").strip(),
    }
    if values["scope_type"] not in REGISTRY_SCOPE_TYPES:
        raise ValueError("unsupported model registry scope type")
    if any(not values[key] for key in ("scope_key", "name", "version", "algorithm", "feature_schema")):
        raise ValueError("model registry identity fields are required")
    if int(training_window_hours) <= 0:
        raise ValueError("training_window_hours must be positive")
    explicit_scope = scope or parse_legacy_scope(
        values["scope_type"], values["scope_key"], horizon_hours=int(training_window_hours),
    )
    dimensions = {}
    if explicit_scope is not None:
        dimensions = {
            "scope_schema": SCOPE_SCHEMA,
            "cluster_id": explicit_scope.cluster_id,
            "entity_type": explicit_scope.entity_type,
            "entity_id": explicit_scope.entity_id,
            "host": explicit_scope.host,
            "metric": explicit_scope.metric,
            "horizon_hours": explicit_scope.horizon_hours,
        }
    existing = session.query(ForecastModelRegistry).filter_by(**values).one_or_none()
    if existing is not None:
        if existing.training_window_hours != int(training_window_hours):
            raise ValueError("model registry identity already exists with a different training window")
        if dimensions and not _scope_ready(existing):
            for key, value in dimensions.items():
                setattr(existing, key, value)
            existing.updated_at = now or utc_now()
            session.flush()
        return existing
    row = ForecastModelRegistry(
        **values,
        **dimensions,
        training_window_hours=int(training_window_hours),
        status="CANDIDATE",
        created_at=now or utc_now(),
        updated_at=now or utc_now(),
    )
    session.add(row)
    session.flush()
    return row


def set_status(
    session, row: ForecastModelRegistry, *, status: str,
    reason: str | None = None, now: datetime | None = None,
) -> ForecastModelRegistry:
    """Record registry lifecycle metadata without performing promotion."""
    status = (status or "").strip().upper()
    if status not in MODEL_STATUSES:
        raise ValueError("invalid model registry status")
    if status == "BLOCKED" and not (reason or "").strip():
        raise ValueError("blocked model requires a reason")
    if status == "ACTIVE":
        other = (
            session.query(ForecastModelRegistry)
            .filter(
                ForecastModelRegistry.scope_type == row.scope_type,
                ForecastModelRegistry.scope_key == row.scope_key,
                ForecastModelRegistry.status == "ACTIVE",
                ForecastModelRegistry.id != row.id,
            )
            .first()
        )
        if other is not None:
            raise ValueError("model registry scope already has an active model")
    when = now or utc_now()
    row.status = status
    row.updated_at = when
    if status == "ACTIVE":
        row.active_since = row.active_since or when
        row.retired_at = None
        row.promotion_reason = (reason or "").strip() or row.promotion_reason
        row.blocked_reason = None
    elif status == "RETIRED":
        row.retired_at = row.retired_at or when
    elif status == "BLOCKED":
        row.blocked_reason = (reason or "").strip()
    elif reason:
        row.promotion_reason = reason.strip()
    session.flush()
    return row


def list_scope(session, *, scope_type: str, scope_key: str) -> list[ForecastModelRegistry]:
    scope_type = (scope_type or "").strip().upper()
    return (
        session.query(ForecastModelRegistry)
        .filter_by(scope_type=scope_type, scope_key=scope_key)
        .order_by(ForecastModelRegistry.created_at, ForecastModelRegistry.version)
        .all()
    )
