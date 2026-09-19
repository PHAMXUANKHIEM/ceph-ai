"""Read-only evidence report for one predictive-forecast canary stream.

The report never enables learning, changes an alert, or promotes a model. It
only joins persisted forecast outcomes with append-only lifecycle transitions
so a canary decision is based on measured evidence instead of the current
state of one mutable alert row.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from statistics import mean

from config.settings import settings
from shared.learning_runtime import canary_scope_allows
from shared.models import (
    NodeResourceForecastAlert,
    NodeResourceForecastRun,
    OnlineLearnerAudit,
    OnlineLearnerCycleAudit,
)

try:
    from shared.models import NodeResourceForecastAlertEvent
    _EVENT_HAS_SCOPE_COLUMNS = True
except ImportError:
    # Production may still be on the earlier lifecycle table. Keep the
    # read-only canary report compatible with both schemas while the
    # migration from ``transitions`` to ``alert_events`` is staged.
    from shared.models import NodeResourceForecastTransition as NodeResourceForecastAlertEvent
    _EVENT_HAS_SCOPE_COLUMNS = False


def _utc_naive(value: datetime) -> datetime:
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _configured_scope() -> dict:
    metrics = sorted({
        item.strip().lower()
        for item in str(settings.online_learning_canary_metrics or "").split(",")
        if item.strip()
    })
    return {
        "enabled": bool(settings.online_learning_canary_enabled),
        "cluster_id": str(settings.online_learning_canary_cluster_id or "").strip() or None,
        "host": str(settings.online_learning_canary_host or "").strip() or None,
        "metrics": metrics,
        "complete": bool(
            settings.online_learning_canary_cluster_id
            and settings.online_learning_canary_host
            and metrics
        ),
    }


def build_canary_report(
    session,
    *,
    cluster_id: str,
    cluster_name: str,
    host: str | None = None,
    metric: str | None = None,
    lookback_hours: int = 72,
    now: datetime | None = None,
) -> dict:
    """Return measured canary evidence for one ``cluster/host/metric``.

    A warning event is counted as a false positive only after its configured
    forecast horizon has elapsed without an evaluated actual breach. Events
    still inside that horizon are reported as ``pending_outcome`` instead of
    being misclassified.
    """
    reference = _utc_naive(now or datetime.now(timezone.utc))
    lookback = max(1, int(lookback_hours))
    start = reference - timedelta(hours=lookback)
    selected_host = (host or settings.online_learning_canary_host or "").strip()
    selected_metric = (metric or "cpu").strip().lower()
    scope = _configured_scope()
    scope_match = canary_scope_allows(cluster_id, selected_host, selected_metric)

    runs = session.query(NodeResourceForecastRun).filter(
        NodeResourceForecastRun.cluster_name == cluster_name,
        NodeResourceForecastRun.host == selected_host,
        NodeResourceForecastRun.metric == selected_metric,
        NodeResourceForecastRun.predicted_at >= start,
        NodeResourceForecastRun.predicted_at <= reference,
    ).order_by(NodeResourceForecastRun.predicted_at).all()
    if _EVENT_HAS_SCOPE_COLUMNS:
        events_query = session.query(NodeResourceForecastAlertEvent).filter(
            NodeResourceForecastAlertEvent.cluster_name == cluster_name,
            NodeResourceForecastAlertEvent.host == selected_host,
            NodeResourceForecastAlertEvent.metric == selected_metric,
            NodeResourceForecastAlertEvent.occurred_at >= start,
            NodeResourceForecastAlertEvent.occurred_at <= reference,
        )
        event_time = lambda row: row.occurred_at
        event_state = lambda row: row.to_state
        events = events_query.order_by(NodeResourceForecastAlertEvent.occurred_at).all()
    else:
        # Older production schema stores scope on the parent alert and the
        # transition timestamp/state under changed_at/new_state.
        events_query = session.query(NodeResourceForecastAlertEvent).join(
            NodeResourceForecastAlert,
            NodeResourceForecastAlert.id == NodeResourceForecastAlertEvent.alert_id,
        ).filter(
            NodeResourceForecastAlert.cluster_name == cluster_name,
            NodeResourceForecastAlert.host == selected_host,
            NodeResourceForecastAlert.metric == selected_metric,
            NodeResourceForecastAlertEvent.changed_at >= start,
            NodeResourceForecastAlertEvent.changed_at <= reference,
        )
        event_time = lambda row: row.changed_at
        event_state = lambda row: row.new_state
        events = events_query.order_by(NodeResourceForecastAlertEvent.changed_at).all()

    evaluated = [row for row in runs if row.status == "EVALUATED" and row.absolute_error is not None]
    errors = [float(row.absolute_error) for row in evaluated]
    state_counts = Counter(row.status for row in runs)
    event_counts = Counter(event_state(row) for row in events)

    threshold = float(settings.node_resource_forecast_trigger_threshold_percent)
    horizon_hours = max(1, int(settings.node_resource_forecast_horizon_hours))
    signal_events = [
        row for row in events
        if event_state(row) in {"WARNING", "CRITICAL"}
    ]
    detections = []
    false_positives = 0
    pending_outcome = 0
    for event in signal_events:
        matching = next(
            (
                run for run in evaluated
                if run.target_at >= event_time(event)
                and run.target_at <= event_time(event) + timedelta(hours=horizon_hours)
                and float(run.actual_percent) >= threshold
            ),
            None,
        )
        if matching is not None:
            detections.append(
                (matching.target_at - event_time(event)).total_seconds() / 3600
            )
        elif event_time(event) + timedelta(hours=horizon_hours) > reference:
            pending_outcome += 1
        else:
            false_positives += 1

    learner_cluster_key = str(cluster_id or "__default__")
    learner_audits = session.query(OnlineLearnerAudit).filter(
        OnlineLearnerAudit.cluster_key == learner_cluster_key,
        OnlineLearnerAudit.host == selected_host,
        OnlineLearnerAudit.metric == selected_metric,
        OnlineLearnerAudit.observed_at >= start,
        OnlineLearnerAudit.observed_at <= reference,
    ).all()
    updates_applied = sum(bool(row.update_applied) for row in learner_audits)
    cycles = session.query(OnlineLearnerCycleAudit).filter(
        OnlineLearnerCycleAudit.cluster_key == learner_cluster_key,
        OnlineLearnerCycleAudit.host == selected_host,
        OnlineLearnerCycleAudit.metric == selected_metric,
        OnlineLearnerCycleAudit.created_at >= start,
        OnlineLearnerCycleAudit.created_at <= reference,
    ).all()
    cpu_time_ms = sum(float(row.cpu_time_ms or 0.0) for row in cycles)

    return {
        "scope": {
            "cluster_id": cluster_id,
            "cluster_name": cluster_name,
            "host": selected_host or None,
            "metric": selected_metric,
            "configured": scope,
            "matches_configured_canary": scope_match,
        },
        "window": {
            "lookback_hours": lookback,
            "from": start,
            "to": reference,
        },
        "forecast_quality": {
            "run_count": len(runs),
            "status_counts": dict(state_counts),
            "evaluated_count": len(evaluated),
            "mae": round(mean(errors), 4) if errors else None,
            "data_quality_count": sum(
                row.drift_status == "DRIFT" or row.status == "UNMEASURABLE"
                for row in runs
            ),
        },
        "alert_lifecycle": {
            "event_count": len(events),
            "state_counts": dict(event_counts),
            "warning_events": sum(event_counts[state] for state in ("WARNING", "CRITICAL")),
            "critical_events": event_counts["CRITICAL"],
            "data_quality_events": event_counts["DATA_QUALITY"],
            "recovery_events": sum(
                event_counts[state] for state in ("RECOVERING", "RECOVERED")
            ),
        },
        "early_detection": {
            "detected_breaches": len(detections),
            "average_lead_time_hours": round(mean(detections), 3) if detections else None,
            "false_positive_events": false_positives,
            "pending_outcome_events": pending_outcome,
            "false_positive_rate": (
                round(false_positives / max(1, false_positives + len(detections)), 4)
                if false_positives + len(detections) else None
            ),
            "threshold_percent": threshold,
            "horizon_hours": horizon_hours,
        },
        "resource_cost": {
            "audit_sample_count": len(learner_audits),
            "updates_applied": updates_applied,
            "cycle_count": len(cycles),
            "cpu_time_ms": round(cpu_time_ms, 3) if cycles else None,
            "elapsed_ms": round(sum(float(row.elapsed_ms or 0.0) for row in cycles), 3)
            if cycles else None,
            "available": bool(cycles),
            "reason": (
                "Đo bằng process CPU time của bounded learner cycle."
                if cycles else "Chưa có learner cycle telemetry trong cửa sổ này."
            ),
        },
        "ready_for_canary_review": bool(
            scope_match and len(evaluated) >= settings.forecast_promotion_min_outcomes
        ),
        "next_step": (
            "Chọn đúng scope và bật SHADOW_ONLY sau khi operator phê duyệt."
            if not settings.online_learning_canary_enabled
            else "Tiếp tục thu thập đủ một chu kỳ 24–72 giờ trước khi đánh giá mở rộng."
            if pending_outcome or len(evaluated) < settings.forecast_promotion_min_outcomes
            else "Operator review alert volume, false-positive, lead time và CPU cost trước khi mở rộng."
        ),
    }
