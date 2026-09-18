"""Read-only acceptance report for a forecast canary scope.

The report is deliberately evidence-only. It does not select a model, change
registry state, enable online learning, send notifications, or execute
remediation. The selected Dashboard cluster is merely the report scope.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from shared.model_registry import default_promotion_policy, evaluate_guarded_promotion
from shared.models import (
    ForecastModelEvaluation,
    ForecastModelRegistry,
    NodeResourceForecastAlert,
    NodeResourceForecastRun,
    VolumeEarlyForecast,
)


def _average(values: list[float | None]) -> float | None:
    usable = [float(value) for value in values if value is not None]
    return round(sum(usable) / len(usable), 6) if usable else None


def _node_scope_metrics(session, scope_key: str, since: datetime) -> dict:
    cluster_name, host, metric = scope_key.split("|", 2)
    runs = session.query(NodeResourceForecastRun).filter(
        NodeResourceForecastRun.cluster_name == cluster_name,
        NodeResourceForecastRun.host == host,
        NodeResourceForecastRun.metric == metric,
        NodeResourceForecastRun.created_at >= since,
    ).all()
    quality_rows = [row for row in runs if row.status in {"DATA_QUALITY", "UNMEASURABLE"}]
    alerts = session.query(NodeResourceForecastAlert).filter(
        NodeResourceForecastAlert.cluster_name == cluster_name,
        NodeResourceForecastAlert.host == host,
        NodeResourceForecastAlert.metric == metric,
        NodeResourceForecastAlert.first_detected_at >= since,
    ).count()
    return {
        "scope_type": "NODE_RESOURCE",
        "scope_key": scope_key,
        "raw_forecast_runs": len(runs),
        "data_quality_rate": round(len(quality_rows) / len(runs), 6) if runs else None,
        "alert_volume": alerts,
        "precision": None,
        "recall": None,
        "early_detection_hours": None,
        "metric_note": "Precision/recall và early detection cần outcome incident đã gán nhãn.",
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

    since = since or datetime.utcnow() - timedelta(days=7)
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
            _node_scope_metrics(session, candidate.scope_key, since)
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

    return {
        "read_only": True,
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "since": since,
        "scopes": scopes,
        "candidate_count": len(scopes),
        "operator_approval_required": True,
        "auto_promotion": False,
        "remediation_executed": False,
        "online_learning_mode": "AUDIT_ONLY",
        "note": "Đây là báo cáo nghiệm thu canary; không thay đổi active model hoặc policy remediation.",
    }
