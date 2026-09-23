"""Read-only acceptance report for a forecast canary scope.

The report is deliberately evidence-only. It does not select a model, change
registry state, enable online learning, send notifications, or execute
remediation. The selected Dashboard cluster is merely the report scope.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from shared.time import utc_now
from statistics import mean

from config.settings import settings
from shared.learning_runtime import evaluate as evaluate_learning_runtime
from shared.model_registry import default_promotion_policy, evaluate_guarded_promotion
from shared.models import (
    ForecastModelEvaluation,
    ForecastModelRegistry,
    NodeResourceForecastAlert,
    NodeResourceForecastFeedback,
    NodeResourceForecastRun,
    NodeResourceForecastTransition,
    OnlineLearnerCycleAudit,
    VolumeEarlyForecast,
)


def _average(values: list[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return round(sum(usable) / len(usable), 6) if usable else None
def _forecast_freshness(
    session,
    *,
    cluster_name: str,
    host: str,
    metric: str,
    now: datetime | None = None,
) -> dict:
    latest = session.query(NodeResourceForecastRun).filter(
        NodeResourceForecastRun.cluster_name == cluster_name,
        NodeResourceForecastRun.host == host,
        NodeResourceForecastRun.metric == metric,
    ).order_by(NodeResourceForecastRun.predicted_at.desc()).first()
    if latest is None:
        return {"status": "NO_DATA", "age_seconds": None, "max_age_seconds": None, "observed_at": None}
    reference = now or utc_now()
    age_seconds = max(0.0, (reference - latest.predicted_at).total_seconds())
    max_age_seconds = max(300, int(settings.node_health_scan_interval_seconds) * 2)
    return {
        "status": "FRESH" if age_seconds <= max_age_seconds else "STALE",
        "age_seconds": round(age_seconds, 1),

        "max_age_seconds": max_age_seconds,
        "observed_at": latest.predicted_at.isoformat() if latest.predicted_at else None,
    }


def _node_scope_metrics(
    session, scope_key: str, since: datetime, cluster_id: str,
) -> dict:
    cluster_name, host, metric = scope_key.split("|", 2)
    runs = session.query(NodeResourceForecastRun).filter(
        NodeResourceForecastRun.cluster_name == cluster_name,
        NodeResourceForecastRun.host == host,
        NodeResourceForecastRun.metric == metric,
        NodeResourceForecastRun.created_at >= since,
    ).all()
    quality_rows = [row for row in runs if row.status in {"DATA_QUALITY", "UNMEASURABLE"}]
    freshness = _forecast_freshness(session, cluster_name=cluster_name, host=host, metric=metric)
    transitions = session.query(NodeResourceForecastTransition).join(
        NodeResourceForecastAlert,
        NodeResourceForecastAlert.id == NodeResourceForecastTransition.alert_id,
    ).filter(
        NodeResourceForecastAlert.cluster_name == cluster_name,
        NodeResourceForecastAlert.host == host,
        NodeResourceForecastAlert.metric == metric,
        NodeResourceForecastTransition.changed_at >= since,
    ).order_by(NodeResourceForecastTransition.changed_at).all()
    signal_transitions = [
        row for row in transitions if row.new_state in {"WARNING", "CRITICAL"}
    ]
    ground_truth = [
        row for row in runs
        if row.status == "EVALUATED"
        and row.actual_percent is not None
        and row.actual_percent >= 90.0
    ]
    lead_times = []
    pending_outcome = 0
    for transition in signal_transitions:
        matching = next(
            (
                row for row in ground_truth
                if row.target_at >= transition.changed_at
                and row.target_at <= transition.changed_at + timedelta(hours=168)
            ),
            None,
        )
        if matching is not None:
            lead_times.append(
                (matching.target_at - transition.changed_at).total_seconds() / 3600
            )
        elif transition.changed_at + timedelta(hours=168) >= utc_now():
            pending_outcome += 1

    feedback_rows = session.query(NodeResourceForecastFeedback).join(
        NodeResourceForecastAlert,
        NodeResourceForecastAlert.id == NodeResourceForecastFeedback.alert_id,
    ).filter(
        NodeResourceForecastAlert.cluster_name == cluster_name,
        NodeResourceForecastAlert.host == host,
        NodeResourceForecastAlert.metric == metric,
        NodeResourceForecastFeedback.created_at >= since,
    ).all()
    true_positives = sum(row.verdict == "TRUE_POSITIVE" for row in feedback_rows)
    false_positives = sum(row.verdict == "FALSE_POSITIVE" for row in feedback_rows)
    cycles = session.query(OnlineLearnerCycleAudit).filter(
        OnlineLearnerCycleAudit.cluster_key == cluster_id,
        OnlineLearnerCycleAudit.host == host,
        OnlineLearnerCycleAudit.metric == metric,
        OnlineLearnerCycleAudit.created_at >= since,
    ).all()
    return {
        "scope_type": "NODE_RESOURCE",
        "scope_key": scope_key,
        "freshness": freshness,
        "raw_forecast_runs": len(runs),
        "data_quality_rate": round(len(quality_rows) / len(runs), 6) if runs else None,
        "alert_volume": len(signal_transitions),
        "transition_count": len(transitions),
        "transition_states": {
            state: sum(row.new_state == state for row in transitions)
            for state in sorted({row.new_state for row in transitions})
        },
        "precision": round(true_positives / (true_positives + false_positives), 6)
        if true_positives + false_positives else None,
        "recall": None,
        "early_detection_hours": round(mean(lead_times), 6) if lead_times else None,
        "pending_outcome": pending_outcome,
        "feedback_count": len(feedback_rows),
        "resource_cost": {
            "available": bool(cycles),
            "cycle_count": len(cycles),
            "cpu_time_ms": round(sum(float(row.cpu_time_ms or 0.0) for row in cycles), 3)
            if cycles else None,
            "elapsed_ms": round(sum(float(row.elapsed_ms or 0.0) for row in cycles), 3)
            if cycles else None,
            "reason": (
                "Đo bằng process CPU time của bounded learner cycle."
                if cycles else "Chưa có learner cycle telemetry trong cửa sổ này."
            ),
        },
        "metric_note": (
            "Alert volume lấy từ append-only lifecycle transitions; precision lấy từ operator feedback. "
            "Recall cần mapping incident/ground-truth độc lập."
        ),
    }


def _volume_scope_metrics(session, scope_key: str, since: datetime) -> dict:
    cluster_id, pool, image, metric = scope_key.split("|", 3)
    rows = session.query(VolumeEarlyForecast).filter(
        VolumeEarlyForecast.cluster_id == cluster_id,
        VolumeEarlyForecast.pool == pool,
        VolumeEarlyForecast.image == image,
        VolumeEarlyForecast.metric == metric,
        VolumeEarlyForecast.created_at >= since,
    ).all()
    quality_rows = [row for row in rows if row.status in {"DATA_QUALITY", "LOW_CONFIDENCE"}]
    return {
        "scope_type": "VOLUME",
        "scope_key": scope_key,
        "raw_forecast_runs": len(rows),
        "data_quality_rate": round(len(quality_rows) / len(rows), 6) if rows else None,
        "alert_volume": sum(row.status == "WARNING" for row in rows),
        "precision": None,
        "recall": None,
        "early_detection_hours": None,
        "metric_note": "Precision/recall và early detection cần outcome volume đã gán nhãn.",
    }


def build_canary_report(
    session,
    *,
    cluster_id: str,
    cluster_name: str,
    since: datetime | None = None,
) -> dict:
    """Build an acceptance report for shadow candidates in one cluster."""

    since = since or utc_now() - timedelta(days=7)
    policy = default_promotion_policy()
    rows = session.query(ForecastModelRegistry).filter(
        ((ForecastModelRegistry.scope_type == "NODE_RESOURCE")
         & ForecastModelRegistry.scope_key.startswith(f"{cluster_name}|"))
        | ((ForecastModelRegistry.scope_type == "VOLUME")
           & ForecastModelRegistry.scope_key.startswith(f"{cluster_id}|"))
    ).order_by(ForecastModelRegistry.scope_type, ForecastModelRegistry.scope_key).all()

    scopes = []
    for candidate in (row for row in rows if row.status in {"CANDIDATE", "SHADOW"}):
        active = session.query(ForecastModelRegistry).filter_by(
            scope_type=candidate.scope_type,
            scope_key=candidate.scope_key,
            status="ACTIVE",
        ).one_or_none()
        if active is None:
            continue
        evaluations = session.query(ForecastModelEvaluation).filter_by(
            candidate_model_id=candidate.id,
            active_model_id=active.id,
        ).order_by(ForecastModelEvaluation.target_at).all()
        decision = evaluate_guarded_promotion(evaluations, policy=policy)
        comparison = {
            "evaluation_count": len(evaluations),
            "active_mae": _average([row.active_mae for row in evaluations]),
            "candidate_mae": _average([row.candidate_mae for row in evaluations]),
            "active_smape": _average([row.active_smape for row in evaluations]),
            "candidate_smape": _average([row.candidate_smape for row in evaluations]),
            "active_false_positive_rate": _average(
                [row.active_false_positive_rate for row in evaluations]
            ),
            "candidate_false_positive_rate": _average(
                [row.candidate_false_positive_rate for row in evaluations]
            ),
            "promotion_guard": {
                "allowed": decision.allowed,
                "status": decision.status,
                "reason": decision.reason,
                "checks": decision.checks,
            },
        }
        metrics = (
            _node_scope_metrics(session, candidate.scope_key, since, cluster_id)
            if candidate.scope_type == "NODE_RESOURCE"
            else _volume_scope_metrics(session, candidate.scope_key, since)
        )
        scopes.append({
            "active_model": {
                "id": active.id,
                "version": active.version,
                "algorithm": active.algorithm,
            },
            "candidate_model": {
                "id": candidate.id,
                "version": candidate.version,
                "algorithm": candidate.algorithm,
                "status": candidate.status,
            },
            "comparison": comparison,
            "operational_metrics": metrics,
        })

    configured_host = str(settings.online_learning_canary_host or "").strip()
    configured_metric = str(settings.online_learning_canary_metrics or "cpu").split(",")[0].strip().lower()
    configured_freshness = _forecast_freshness(
        session, cluster_name=cluster_name, host=configured_host, metric=configured_metric,
    ) if configured_host else {"status": "NO_DATA", "age_seconds": None, "max_age_seconds": None, "observed_at": None}
    configured_scope_matches = str(settings.online_learning_canary_cluster_id or "").strip() == str(cluster_id) and configured_host != ""
    candidate_evidence_ready = any(
        scope["candidate_model"]["status"] in {"CANDIDATE", "SHADOW"}
        and scope["comparison"]["evaluation_count"] >= policy.minimum_outcomes
        for scope in scopes
    )
    runtime = evaluate_learning_runtime(
        session,
        cluster_id,
        host=(settings.online_learning_canary_host or None),
        metric=(settings.online_learning_canary_metrics or "cpu").split(",")[0].strip(),
    ).as_dict()
    ready_for_operator_acceptance = bool(
        configured_scope_matches
        and settings.online_learning_canary_enabled
        and configured_freshness["status"] == "FRESH"
        and candidate_evidence_ready
    )
    return {
        "read_only": True,
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "canary_scope": {"cluster_id": settings.online_learning_canary_cluster_id, "host": configured_host, "metric": configured_metric},
        "canary_freshness": configured_freshness,
        "ready_for_operator_acceptance": ready_for_operator_acceptance,
        "since": since,
        "scopes": scopes,
        "candidate_count": len(scopes),
        "operator_approval_required": True,
        "auto_promotion": False,
        "remediation_executed": False,
        "online_learning_mode": runtime["mode"],
        "runtime": runtime,
        "note": "Đây là báo cáo nghiệm thu canary; không thay đổi active model hoặc policy remediation.",
    }
