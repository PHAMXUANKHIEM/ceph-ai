"""Build and consume verified labels for online-learning samples."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from config.settings import settings
from shared.models import (
    Cluster,
    NodeResourceForecastRun,
    OnlineLearnerAudit,
    OnlineLearnerLabel,
)


READY = "READY"
CONSUMED = "CONSUMED"


def normalize_metric(metric: str) -> str:
    value = str(metric or "").strip().lower()
    return "ram" if value in {"ram", "memory", "mem"} else value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _cluster_key(session, cluster_name: str) -> str:
    cluster = session.scalar(select(Cluster).where(Cluster.name == cluster_name))
    return cluster.id if cluster is not None else cluster_name


def enqueue_verified_outcomes(
    session,
    *,
    cluster_name: str | None = None,
    host: str | None = None,
    metric: str | None = None,
    max_rows: int = 100,
    match_window_seconds: float | None = None,
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
    tolerance = float(match_window_seconds or max(
        30.0, settings.node_resource_learning_max_outcome_gap_hours * 3600,
    ))
    query = select(NodeResourceForecastRun).where(
        NodeResourceForecastRun.status == "EVALUATED",
        NodeResourceForecastRun.actual_percent.is_not(None),
    ).order_by(NodeResourceForecastRun.evaluated_at).limit(max_rows)
    if cluster_name:
        query = query.where(NodeResourceForecastRun.cluster_name == cluster_name)
    if host:
        query = query.where(NodeResourceForecastRun.host == host)
    if metric:
        query = query.where(NodeResourceForecastRun.metric == normalize_metric(metric))

    created = 0
    for run in session.scalars(query):
        if run.actual_percent is None or not math.isfinite(float(run.actual_percent)):
            continue
        if session.scalar(select(OnlineLearnerLabel.id).where(OnlineLearnerLabel.source_run_id == run.id)):
            continue
        cluster_key = _cluster_key(session, run.cluster_name)
        normalized_metric = normalize_metric(run.metric)
        audits = list(session.scalars(
            select(OnlineLearnerAudit).where(
                OnlineLearnerAudit.cluster_key == cluster_key,
                OnlineLearnerAudit.host == run.host,
                OnlineLearnerAudit.metric == normalized_metric,
            )
        ))
        target = _utc(run.target_at)
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
        if session.scalar(select(OnlineLearnerLabel.id).where(
            OnlineLearnerLabel.cluster_key == cluster_key,
            OnlineLearnerLabel.host == run.host,
            OnlineLearnerLabel.metric == normalized_metric,
            OnlineLearnerLabel.sample_id == candidate.sample_id,
        )):
            continue
        session.add(OnlineLearnerLabel(
            cluster_key=cluster_key,
            host=run.host,
            metric=normalized_metric,
            sample_id=candidate.sample_id,
            source_run_id=run.id,
            observed_at=candidate.observed_at,
            label_value=float(run.actual_percent),
            status=READY,
            reason=(
                f"forecast run {run.id} EVALUATED; actual telemetry matched "
                f"target within {distance:.1f}s"
            ),
            verified_at=run.evaluated_at or datetime.utcnow(),
        ))
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


def mark_consumed(label: OnlineLearnerLabel, *, now: datetime | None = None) -> None:
    label.status = CONSUMED
    label.consumed_at = now or datetime.utcnow()
