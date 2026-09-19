"""Fail-closed operator controls for bounded online-learning streams."""

from __future__ import annotations

from datetime import datetime

from shared.models import (
    OnlineLearnerControl,
    OnlineLearnerOperatorAudit,
    OnlineLearnerState,
)

RUNNING = "RUNNING"
PAUSED = "PAUSED"
VALID_STATUSES = frozenset({RUNNING, PAUSED})


def normalize_scope(*, cluster_id: str | None, host: str, metric: str) -> tuple[str, str, str]:
    cluster_key = str(cluster_id or "__default__").strip() or "__default__"
    normalized_host = str(host or "").strip()
    normalized_metric = str(metric or "").strip().lower()
    if not normalized_host or len(normalized_host) > 255:
        raise ValueError("learner host is required and must be at most 255 characters")
    if normalized_metric not in {"cpu", "ram"}:
        raise ValueError("learner metric must be cpu or ram")
    return cluster_key, normalized_host, normalized_metric


def get_control(session, *, cluster_id: str | None, host: str, metric: str):
    cluster_key, normalized_host, normalized_metric = normalize_scope(
        cluster_id=cluster_id, host=host, metric=metric,
    )
    return session.query(OnlineLearnerControl).filter_by(
        cluster_key=cluster_key, host=normalized_host, metric=normalized_metric,
    ).one_or_none()


def is_paused(session, *, cluster_id: str | None, host: str, metric: str) -> bool:
    """Return True only for an explicitly persisted PAUSED control.

    A missing control is RUNNING.  Database read failures are intentionally
    not swallowed by this helper; the caller's transaction must fail closed.
    """

    control = get_control(session, cluster_id=cluster_id, host=host, metric=metric)
    return bool(control and control.status == PAUSED)


def _audit(
    session, *, cluster_key: str, host: str, metric: str, action: str,
    actor: str, reason: str, target_id: str | None = None,
    now: datetime | None = None,
) -> OnlineLearnerOperatorAudit:
    row = OnlineLearnerOperatorAudit(
        cluster_key=cluster_key,
        host=host,
        metric=metric,
        action=action,
        actor=(actor or "unknown")[:64],
        reason=(reason or "operator control")[:4000],
        target_id=target_id,
        created_at=now or datetime.utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def set_status(
    session, *, cluster_id: str | None, host: str, metric: str,
    status: str, actor: str, reason: str,
    now: datetime | None = None,
) -> OnlineLearnerControl:
    cluster_key, normalized_host, normalized_metric = normalize_scope(
        cluster_id=cluster_id, host=host, metric=metric,
    )
    normalized_status = str(status or "").strip().upper()
    if normalized_status not in VALID_STATUSES:
        raise ValueError("learner control status must be RUNNING or PAUSED")
    if not str(reason or "").strip():
        raise ValueError("operator reason is required")
    when = now or datetime.utcnow()
    row = session.query(OnlineLearnerControl).filter_by(
        cluster_key=cluster_key, host=normalized_host, metric=normalized_metric,
    ).one_or_none()
    if row is None:
        row = OnlineLearnerControl(
            cluster_key=cluster_key,
            host=normalized_host,
            metric=normalized_metric,
            status=normalized_status,
            reason=reason.strip(),
            updated_by=(actor or "unknown")[:64],
            created_at=when,
            updated_at=when,
        )
        session.add(row)
    else:
        row.status = normalized_status
        row.reason = reason.strip()
        row.updated_by = (actor or "unknown")[:64]
        row.updated_at = when
    _audit(
        session,
        cluster_key=cluster_key,
        host=normalized_host,
        metric=normalized_metric,
        action="PAUSE" if normalized_status == PAUSED else "RESUME",
        actor=actor,
        reason=reason,
        now=when,
    )
    session.flush()
    return row


def reset_state(
    session, *, cluster_id: str | None, host: str, metric: str,
    actor: str, reason: str, now: datetime | None = None,
) -> int:
    """Delete only the selected durable learner state and audit the reset."""

    cluster_key, normalized_host, normalized_metric = normalize_scope(
        cluster_id=cluster_id, host=host, metric=metric,
    )
    if not str(reason or "").strip():
        raise ValueError("operator reason is required")
    rows = session.query(OnlineLearnerState).filter_by(
        cluster_key=cluster_key, host=normalized_host, metric=normalized_metric,
    ).all()
    for row in rows:
        session.delete(row)
    _audit(
        session,
        cluster_key=cluster_key,
        host=normalized_host,
        metric=normalized_metric,
        action="RESET_STATE",
        actor=actor,
        reason=reason,
        now=now,
    )
    session.flush()
    return len(rows)


def list_audit(session, *, cluster_id: str | None, limit: int = 100):
    cluster_key = str(cluster_id or "__default__").strip() or "__default__"
    bounded_limit = max(1, min(int(limit), 200))
    return session.query(OnlineLearnerOperatorAudit).filter_by(
        cluster_key=cluster_key,
    ).order_by(OnlineLearnerOperatorAudit.created_at.desc()).limit(bounded_limit).all()
