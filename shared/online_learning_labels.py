"""Build and consume verified labels for online-learning samples."""

from __future__ import annotations

import math
import hashlib
import json
from datetime import datetime, timedelta, timezone
from shared.time import utc_now

from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from config.settings import settings
from shared.models import (
    Cluster,
    NodeResourceForecastRun,
    NodeResourceForecastAlert,
    OnlineLearnerAudit,
    OnlineLearnerLabel,
    OnlineLearnerLabelEvent,
)


READY = "READY"
CONSUMED = "CONSUMED"
VERIFIED_SUCCESS = "VERIFIED_SUCCESS"
VERIFIED_FAILED = "VERIFIED_FAILED"
INCONCLUSIVE = "INCONCLUSIVE"
REVOKED = "REVOKED"
EVENT_CREATED = "CREATED"
EVENT_BLOCKED = "BLOCKED"
EVENT_CONSUMED = "CONSUMED"
EVENT_REVOKED = "REVOKED"


def normalize_metric(metric: str) -> str:
    value = str(metric or "").strip().lower()
    return "ram" if value in {"ram", "memory", "mem"} else value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _telemetry_fingerprint(run: NodeResourceForecastRun, audit: OnlineLearnerAudit) -> str:
    """Bind a label to independent observed telemetry, not its prediction."""
    evidence = {
        "run_id": run.id,
        "cluster": run.cluster_name,
        "host": run.host,
        "metric": normalize_metric(run.metric),
        "horizon_hours": run.horizon_hours,
        "target_at": _utc(run.target_at).isoformat(),
        "sample_id": audit.sample_id,
        "observed_at": _utc(audit.observed_at).isoformat(),
        "observed_value": float(audit.value),
    }
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _cluster_key(session, cluster_name: str) -> str:
    cluster = session.scalar(select(Cluster).where(Cluster.name == cluster_name))
    return cluster.id if cluster is not None else cluster_name


def _record_event_once(
    session,
    *,
    action: str,
    actor: str,
    reason: str,
    label_id: str | None = None,
    source_run_id: str | None = None,
) -> OnlineLearnerLabelEvent | None:
    """Write one append-only event, deduplicating reconciliation decisions."""

    query = select(OnlineLearnerLabelEvent.id).where(
        OnlineLearnerLabelEvent.action == action,
    )
    if source_run_id is not None:
        query = query.where(OnlineLearnerLabelEvent.source_run_id == source_run_id)
    elif label_id is not None:
        query = query.where(OnlineLearnerLabelEvent.label_id == label_id)
    else:
        return None
    if session.scalar(query) is not None:
        return None
    event = OnlineLearnerLabelEvent(
        label_id=label_id,
        source_run_id=source_run_id,
        action=action,
        actor=(actor or "system").strip()[:64],
        reason=(reason or "").strip()[:4000],
    )
    session.add(event)
    return event


def enqueue_verified_outcomes(
    session,
    *,
    cluster_name: str | None = None,
    host: str | None = None,
    metric: str | None = None,
    max_rows: int = 100,
    match_window_seconds: float | None = None,
    source_run_ids: list[str] | None = None,
) -> int:
    """Create READY labels only from evaluated forecast ground truth.

    A forecast must have an actual value and status ``EVALUATED``.  The value
    is matched to an existing audit sample near the forecast target time; no
    label is created when the sample is absent or the timestamp is too far
    away.  This is deliberately telemetry-only and does not trust an LLM or
    an operator note as a numeric label.
    """

    if max_rows < 1:
        return 0
    if source_run_ids is not None and not source_run_ids:
        return 0
    tolerance = float(match_window_seconds or max(
        30.0, settings.node_resource_learning_max_outcome_gap_hours * 3600,
    ))
    current_time = _utc(utc_now())
    query = select(NodeResourceForecastRun).where(
        NodeResourceForecastRun.status == "EVALUATED",
        NodeResourceForecastRun.actual_percent.is_not(None),
        NodeResourceForecastRun.evaluated_at >= (current_time - timedelta(days=30)).replace(tzinfo=None),
    ).order_by(NodeResourceForecastRun.evaluated_at.desc()).limit(max_rows)
    if cluster_name:
        query = query.where(NodeResourceForecastRun.cluster_name == cluster_name)
    if host:
        query = query.where(NodeResourceForecastRun.host == host)
    if metric:
        query = query.where(NodeResourceForecastRun.metric == normalize_metric(metric))
    if source_run_ids is not None:
        query = query.where(NodeResourceForecastRun.id.in_(source_run_ids[:max_rows]))

    created = 0
    minimum_evidence = max(1, int(settings.online_learning_min_verified_evidence))
    tolerance_percent = max(0.0, float(settings.online_learning_label_tolerance_percent))
    rate_limit = max(1, int(settings.online_learning_label_rate_limit))
    rate_window = timedelta(seconds=max(60, int(settings.online_learning_label_rate_window_seconds)))
    rate_window_start = current_time - rate_window
    source_actor = "forecast-evaluator"
    for run in session.scalars(query):
        if run.actual_percent is None or not math.isfinite(float(run.actual_percent)):
            continue
        if not 0 <= float(run.actual_percent) <= 100:
            continue
        if run.evaluated_at is None or _utc(run.evaluated_at) < _utc(run.target_at):
            continue
        if _utc(run.evaluated_at) > current_time + timedelta(minutes=5):
            continue
        if _utc(run.target_at) < _utc(run.predicted_at):
            continue
        if current_time - _utc(run.evaluated_at) > timedelta(days=30):
            continue
        predicted = run.predicted_percent
        if predicted is None or not math.isfinite(float(predicted)):
            continue
        if session.scalar(select(OnlineLearnerLabel.id).where(OnlineLearnerLabel.source_run_id == run.id)):
            continue
        open_alert = session.scalar(select(NodeResourceForecastAlert.id).where(
            NodeResourceForecastAlert.cluster_name == run.cluster_name,
            NodeResourceForecastAlert.host == run.host,
            NodeResourceForecastAlert.metric == normalize_metric(run.metric),
            NodeResourceForecastAlert.status == "OPEN",
        ))
        if open_alert is not None:
            _record_event_once(
                session,
                action=EVENT_BLOCKED,
                actor="label-policy",
                source_run_id=run.id,
                reason="forecast alert is still OPEN; final label is deferred until lifecycle closes",
            )
            continue
        recent_count = session.scalar(select(func.count(OnlineLearnerLabel.id)).where(
            OnlineLearnerLabel.source_actor == source_actor,
            OnlineLearnerLabel.created_at >= rate_window_start,
        )) or 0
        if recent_count + created >= rate_limit:
            _record_event_once(
                session,
                action=EVENT_BLOCKED,
                actor="label-policy",
                source_run_id=run.id,
                reason=(
                    f"label rate limit reached for {source_actor}: "
                    f"{recent_count + created}/{rate_limit} in {rate_window.total_seconds():.0f}s"
                ),
            )
            break
        cluster_key = _cluster_key(session, run.cluster_name)
        normalized_metric = normalize_metric(run.metric)
        target = _utc(run.target_at)
        lower = (target - timedelta(seconds=tolerance)).replace(tzinfo=None)
        upper = (target + timedelta(seconds=tolerance)).replace(tzinfo=None)
        audits = list(session.scalars(
            select(OnlineLearnerAudit).where(
                OnlineLearnerAudit.cluster_key == cluster_key,
                OnlineLearnerAudit.host == run.host,
                OnlineLearnerAudit.metric == normalized_metric,
                OnlineLearnerAudit.observed_at >= lower,
                OnlineLearnerAudit.observed_at <= upper,
            ).order_by(OnlineLearnerAudit.observed_at.desc()).limit(512)
        ))
        candidate = min(
            audits,
            key=lambda item: abs((_utc(item.observed_at) - target).total_seconds()),
            default=None,
        )
        if candidate is None:
            continue
        distance = abs((_utc(candidate.observed_at) - target).total_seconds())
        if distance > tolerance:
            continue
        # An evaluator row and a nearby audit timestamp are not sufficient:
        # the independent observed value must agree with the outcome.  This
        # rejects model-self-labels and synthetic/incorrectly paired samples.
        if (
            candidate.value is None
            or not math.isfinite(float(candidate.value))
            or not 0 <= float(candidate.value) <= 100
            or abs(float(candidate.value) - float(run.actual_percent)) > 0.01
            or candidate.label is not None
            or candidate.update_applied
        ):
            _record_event_once(
                session, action=EVENT_BLOCKED, actor="label-policy",
                source_run_id=run.id,
                reason="independent audit value missing, mismatched, or already model-labeled",
            )
            continue
        if session.scalar(select(OnlineLearnerLabel.id).where(
            OnlineLearnerLabel.cluster_key == cluster_key,
            OnlineLearnerLabel.host == run.host,
            OnlineLearnerLabel.metric == normalized_metric,
            OnlineLearnerLabel.sample_id == candidate.sample_id,
        )):
            continue
        evidence_count = session.scalar(select(func.count(NodeResourceForecastRun.id)).where(
            NodeResourceForecastRun.cluster_name == run.cluster_name,
            NodeResourceForecastRun.host == run.host,
            NodeResourceForecastRun.metric == run.metric,
            NodeResourceForecastRun.status == "EVALUATED",
            NodeResourceForecastRun.actual_percent.is_not(None),
            NodeResourceForecastRun.target_at <= run.target_at,
        )) or 0
        if evidence_count < minimum_evidence:
            # Do not enqueue a weak label. The forecast remains EVALUATED and
            # will be reconsidered on the next reconciliation cycle after the
            # stream accumulates enough independent outcomes.
            continue
        absolute_error = abs(float(run.actual_percent) - float(predicted))
        outcome = VERIFIED_SUCCESS if absolute_error <= tolerance_percent else VERIFIED_FAILED
        ready_reason = (
            f"forecast run {run.id} {outcome}; actual telemetry matched "
            f"target within {distance:.1f}s; evidence={evidence_count}/{minimum_evidence}; "
            f"absolute_error={absolute_error:.2f}"
        )
        label_row = OnlineLearnerLabel(
            cluster_key=cluster_key,
            host=run.host,
            metric=normalized_metric,
            sample_id=candidate.sample_id,
            source_run_id=run.id,
            observed_at=candidate.observed_at,
            label_value=float(run.actual_percent),
            predicted_value=float(predicted),
            absolute_error=absolute_error,
            outcome=outcome,
            status=READY,
            reason=ready_reason,
            evidence_count=int(evidence_count),
            source_actor="forecast-evaluator",
            verified_at=run.evaluated_at or utc_now(),
            evidence_fingerprint=_telemetry_fingerprint(run, candidate),
            source_model_version=(
                f"{run.algorithm}:w{run.window_hours}:h{run.horizon_hours}"
            ),
            outcome_observed_at=candidate.observed_at,
        )
        session.add(label_row)
        session.flush()
        _record_event_once(
            session,
            action=EVENT_CREATED,
            actor=source_actor,
            label_id=label_row.id,
            source_run_id=run.id,
            reason=ready_reason,
        )
        created += 1
    return created


def ready_label_for_sample(
    session, *, cluster_key: str, host: str, metric: str, sample_id: str,
) -> OnlineLearnerLabel | None:
    return session.scalar(select(OnlineLearnerLabel).where(
        OnlineLearnerLabel.cluster_key == cluster_key,
        OnlineLearnerLabel.host == host,
        OnlineLearnerLabel.metric == normalize_metric(metric),
        OnlineLearnerLabel.sample_id == sample_id,
        OnlineLearnerLabel.status == READY,
    ))


def label_policy_paused(session, *, now: datetime | None = None) -> bool:
    """Fail closed for one rate-limit window after poisoning is detected."""

    cutoff = (now or utc_now()) - timedelta(
        seconds=max(60, int(settings.online_learning_label_rate_window_seconds)),
    )
    return session.scalar(select(OnlineLearnerLabelEvent.id).where(
        OnlineLearnerLabelEvent.action == EVENT_BLOCKED,
        OnlineLearnerLabelEvent.reason.like("label rate limit reached%"),
        OnlineLearnerLabelEvent.created_at >= cutoff,
    )) is not None


def mark_consumed(label: OnlineLearnerLabel, *, now: datetime | None = None) -> None:
    label.status = CONSUMED
    label.consumed_at = now or utc_now()
    session = object_session(label)
    if session is not None:
        _record_event_once(
            session,
            action=EVENT_CONSUMED,
            actor="online-learner",
            label_id=label.id,
            source_run_id=label.source_run_id,
            reason="verified label consumed by guarded online-learning update",
        )


def revoke_label(session, label: OnlineLearnerLabel, *, actor: str, reason: str) -> None:
    """Revoke a label without deleting history; every revocation is audited."""

    normalized_actor = (actor or "").strip()
    normalized_reason = (reason or "").strip()
    if not normalized_actor or not normalized_reason:
        raise ValueError("label revocation requires actor and reason")
    if label.status == REVOKED:
        return
    label.status = REVOKED
    label.consumed_at = None
    _record_event_once(
        session,
        action=EVENT_REVOKED,
        actor=normalized_actor,
        label_id=label.id,
        source_run_id=label.source_run_id,
        reason=normalized_reason,
    )
