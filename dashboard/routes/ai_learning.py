"""Read-only visibility into supervised AI/forecast learning quality."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func

from config.settings import settings
from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db, remediation_feedback
from shared.models import (
    LogFaultStat,
    LogFinding,
    LogLearningSample,
    NodeResourceForecastRun,
    NodeResourceModelState,
    VolumeEarlyForecast,
    VolumeForecastRun,
    VolumeModelState,
)

router = APIRouter()
templates = make_templates()


def _quality(mae: float | None, outcomes: int) -> tuple[str, str, float | None]:
    if mae is None or outcomes < settings.node_resource_learning_min_outcomes:
        return "COLLECTING", "Chưa đủ outcome tối thiểu để đánh giá.", None
    accuracy = round(max(0.0, min(100.0, 100.0 - mae)), 2)
    if outcomes >= 20 and accuracy >= 80:
        return "RELIABLE", "Đã có ít nhất 20 outcome và sai số trung bình không quá 20 điểm %.", accuracy
    if accuracy >= 80:
        return "PROMISING", "Kết quả ban đầu tốt nhưng số outcome còn ít.", accuracy
    return "NEEDS_IMPROVEMENT", "Sai số trung bình còn lớn hơn 20 điểm %.", accuracy


def _volume_quality(mape: float | None, outcomes: int) -> tuple[str, str, float | None]:
    if mape is None or outcomes < settings.volume_learning_min_outcomes:
        accuracy = round(max(0.0, 100.0 - mape), 2) if mape is not None else None
        return "COLLECTING", "Chưa đủ outcome tối thiểu để đánh giá ổn định.", accuracy
    accuracy = round(max(0.0, 100.0 - mape), 2)
    if outcomes >= 20 and accuracy >= 80:
        return "RELIABLE", "Đã có ít nhất 20 outcome và MAPE không quá 20%.", accuracy
    if accuracy >= 80:
        return "PROMISING", "Kết quả ban đầu tốt nhưng số outcome còn ít.", accuracy
    return "NEEDS_IMPROVEMENT", "MAPE còn lớn hơn 20%.", accuracy


def learning_status(cluster_id: str, cluster_name: str) -> dict:
    """Build a JSON-safe snapshot. No learning state is changed here."""
    with db.SessionLocal() as session:
        states = session.query(NodeResourceModelState).filter_by(cluster_name=cluster_name).all()
        run_counts = dict(
            session.query(NodeResourceForecastRun.status, func.count(NodeResourceForecastRun.id))
            .filter(NodeResourceForecastRun.cluster_name == cluster_name)
            .group_by(NodeResourceForecastRun.status)
            .all()
        )

        resource_models = []
        for state in sorted(states, key=lambda row: (row.host, row.metric, row.window_hours)):
            status, reason, accuracy = _quality(state.mean_absolute_error, state.evaluated_count)
            latest = (
                session.query(NodeResourceForecastRun)
                .filter_by(
                    cluster_name=cluster_name, host=state.host,
                    metric=state.metric, window_hours=state.window_hours,
                )
                .order_by(NodeResourceForecastRun.predicted_at.desc())
                .first()
            )
            resource_models.append({
                "host": state.host,
                "metric": state.metric.upper(),
                "window_hours": state.window_hours,
                "selected": state.selected,
                "evaluated_count": state.evaluated_count,
                "mae": round(state.mean_absolute_error, 3) if state.mean_absolute_error is not None else None,
                "last_error": round(state.last_absolute_error, 3) if state.last_absolute_error is not None else None,
                "accuracy_estimate": accuracy,
                "quality_status": status,
                "quality_reason": reason,
                "latest_confidence": round(latest.confidence, 3) if latest else None,
                "latest_prediction": round(latest.predicted_percent, 2) if latest else None,
                "latest_target_at": latest.target_at if latest else None,
                "updated_at": state.updated_at,
            })

        volume_run_counts = dict(
            session.query(VolumeForecastRun.status, func.count(VolumeForecastRun.id))
            .filter(VolumeForecastRun.cluster_id == cluster_id)
            .group_by(VolumeForecastRun.status).all()
        )
        volume_states = session.query(VolumeModelState).filter_by(cluster_id=cluster_id).all()
        latest_volume_times = (
            session.query(
                VolumeForecastRun.pool.label("pool"),
                VolumeForecastRun.image.label("image"),
                VolumeForecastRun.metric.label("metric"),
                VolumeForecastRun.window_hours.label("window_hours"),
                func.max(VolumeForecastRun.predicted_at).label("latest_at"),
            )
            .filter(VolumeForecastRun.cluster_id == cluster_id)
            .group_by(
                VolumeForecastRun.pool, VolumeForecastRun.image,
                VolumeForecastRun.metric, VolumeForecastRun.window_hours,
            ).subquery()
        )
        latest_volume_runs = {
            (row.pool, row.image, row.metric, row.window_hours): row
            for row in session.query(VolumeForecastRun).join(
                latest_volume_times,
                (VolumeForecastRun.pool == latest_volume_times.c.pool)
                & (VolumeForecastRun.image == latest_volume_times.c.image)
                & (VolumeForecastRun.metric == latest_volume_times.c.metric)
                & (VolumeForecastRun.window_hours == latest_volume_times.c.window_hours)
                & (VolumeForecastRun.predicted_at == latest_volume_times.c.latest_at),
            ).filter(VolumeForecastRun.cluster_id == cluster_id).all()
        }
        volume_models = []
        for state in sorted(
            volume_states,
            key=lambda row: (row.pool, row.image, row.metric, row.window_hours),
        ):
            status, reason, accuracy = _volume_quality(
                state.mean_percentage_error, state.evaluated_count
            )
            latest = latest_volume_runs.get(
                (state.pool, state.image, state.metric, state.window_hours)
            )
            volume_models.append({
                "pool": state.pool,
                "image": state.image,
                "metric": state.metric,
                "window_hours": state.window_hours,
                "selected": state.selected,
                "evaluated_count": state.evaluated_count,
                "mae": round(state.mean_absolute_error, 3) if state.mean_absolute_error is not None else None,
                "mape": round(state.mean_percentage_error, 3) if state.mean_percentage_error is not None else None,
                "last_error": round(state.last_absolute_error, 3) if state.last_absolute_error is not None else None,
                "accuracy_estimate": accuracy,
                "quality_status": status,
                "quality_reason": reason,
                "latest_prediction": round(latest.predicted_value, 3) if latest else None,
                "latest_confidence": round(latest.confidence, 3) if latest else None,
                "seasonal_scope": latest.seasonal_scope if latest else None,
                "training_samples": latest.training_samples if latest else None,
                "updated_at": state.updated_at,
            })

        latest_forecast_times = (
            session.query(
                VolumeEarlyForecast.pool.label("pool"),
                VolumeEarlyForecast.image.label("image"),
                VolumeEarlyForecast.metric.label("metric"),
                VolumeEarlyForecast.horizon_hours.label("horizon_hours"),
                func.max(VolumeEarlyForecast.generated_at).label("latest_at"),
            ).filter(VolumeEarlyForecast.cluster_id == cluster_id).group_by(
                VolumeEarlyForecast.pool, VolumeEarlyForecast.image,
                VolumeEarlyForecast.metric, VolumeEarlyForecast.horizon_hours,
            ).subquery()
        )
        latest_forecasts = session.query(VolumeEarlyForecast).join(
            latest_forecast_times,
            (VolumeEarlyForecast.pool == latest_forecast_times.c.pool)
            & (VolumeEarlyForecast.image == latest_forecast_times.c.image)
            & (VolumeEarlyForecast.metric == latest_forecast_times.c.metric)
            & (VolumeEarlyForecast.horizon_hours == latest_forecast_times.c.horizon_hours)
            & (VolumeEarlyForecast.generated_at == latest_forecast_times.c.latest_at),
        ).filter(VolumeEarlyForecast.cluster_id == cluster_id).order_by(
            VolumeEarlyForecast.status.desc(), VolumeEarlyForecast.target_at
        ).limit(200).all()
        forecast_rows = [{
            "pool": row.pool, "image": row.image, "metric": row.metric,
            "horizon_hours": row.horizon_hours,
            "current_value": round(row.current_value, 3),
            "predicted_value": round(row.predicted_value, 3),
            "threshold_type": row.threshold_type,
            "threshold_value": round(row.threshold_value, 3) if row.threshold_value is not None else None,
            "confidence": round(row.confidence, 3),
            "training_samples": row.training_samples,
            "training_window_hours": row.training_window_hours,
            "seasonal_scope": row.seasonal_scope,
            "model_version": row.model_version,
            "status": row.status, "reason": row.reason,
            "generated_at": row.generated_at, "target_at": row.target_at,
            "source_latest_at": row.source_latest_at,
        } for row in latest_forecasts]

        sample_query = session.query(LogLearningSample).filter(
            LogLearningSample.cluster_id == cluster_id
        )
        sample_count = sample_query.count()
        eligible = sample_query.filter(LogLearningSample.eligible_for_learning.is_(True)).count()
        state_counts = dict(
            session.query(LogLearningSample.state, func.count(LogLearningSample.id))
            .filter(LogLearningSample.cluster_id == cluster_id)
            .group_by(LogLearningSample.state).all()
        )
        label_counts = dict(
            session.query(LogLearningSample.label, func.count(LogLearningSample.id))
            .filter(LogLearningSample.cluster_id == cluster_id)
            .group_by(LogLearningSample.label).all()
        )
        blocker_rows = (
            session.query(LogLearningSample.exclusion_reason, func.count(LogLearningSample.id))
            .filter(
                LogLearningSample.cluster_id == cluster_id,
                LogLearningSample.eligible_for_learning.is_(False),
            )
            .group_by(LogLearningSample.exclusion_reason)
            .order_by(func.count(LogLearningSample.id).desc())
            .limit(8).all()
        )
        recent_rows = (
            session.query(LogLearningSample, LogFinding.title)
            .outerjoin(LogFinding, LogFinding.id == LogLearningSample.log_finding_id)
            .filter(LogLearningSample.cluster_id == cluster_id)
            .order_by(LogLearningSample.updated_at.desc())
            .limit(20)
            .all()
        )
        stats = (
            session.query(LogFaultStat)
            .filter(LogFaultStat.cluster_id == cluster_id)
            .order_by(LogFaultStat.trust_score.desc(), LogFaultStat.sample_count.desc())
            .all()
        )
        recent_samples = [{
            "id": sample.id,
            "title": title or sample.fault_family or "Finding không có tiêu đề",
            "daemon_type": sample.daemon_type,
            "fault_family": sample.fault_family,
            "host": sample.host,
            "state": sample.state,
            "label": sample.label,
            "eligible": sample.eligible_for_learning,
            "exclusion_reason": sample.exclusion_reason,
            "updated_at": sample.updated_at,
        } for sample, title in recent_rows]

        return {
            "remediation_feedback": remediation_feedback.summary(
                session, cluster_id=cluster_id
            ),
            "resource_learning": {
                "enabled": settings.node_resource_forecast_enabled,
                "evaluation_hours": settings.node_resource_learning_evaluation_hours,
                "minimum_outcomes": settings.node_resource_learning_min_outcomes,
                "candidate_windows": settings.node_resource_learning_candidate_hours,
                "run_counts": run_counts,
                "selected_models": [row for row in resource_models if row["selected"]],
                "candidate_models": resource_models,
            },
            "volume_learning": {
                "enabled": settings.volume_learning_enabled,
                "evaluation_hours": settings.volume_learning_evaluation_hours,
                "minimum_outcomes": settings.volume_learning_min_outcomes,
                "minimum_samples": settings.volume_learning_min_samples,
                "candidate_windows": settings.volume_learning_candidate_hours,
                "run_counts": volume_run_counts,
                "selected_models": [row for row in volume_models if row["selected"]],
                "candidate_models": volume_models,
                "early_forecasts": forecast_rows,
                "forecast_warning_count": sum(row["status"] == "WARNING" for row in forecast_rows),
                "forecast_horizons": settings.volume_forecast_horizons,
                "forecast_latency_slo_ms": settings.volume_forecast_latency_slo_ms,
            },
            "log_learning": {
                "sample_count": sample_count,
                "eligible_count": eligible,
                "blocked_count": sample_count - eligible,
                "state_counts": state_counts,
                "label_counts": label_counts,
                "top_blockers": [
                    {"reason": reason or "Không có lý do chặn", "count": count}
                    for reason, count in blocker_rows
                ],
                "fault_stats": [{
                    "daemon_type": row.daemon_type,
                    "fault_family": row.fault_family,
                    "playbook_id": row.playbook_id,
                    "sample_count": row.sample_count,
                    "verified_count": row.verified_count,
                    "success_count": row.success_count,
                    "failure_count": row.failure_count,
                    "inconclusive_count": row.inconclusive_count,
                    "trust_percent": round(row.trust_score * 100, 2),
                    "blocked_reason": row.promotion_blocked_reason,
                    "updated_at": row.updated_at,
                } for row in stats],
                "recent_samples": recent_samples,
                "mode": "AUDIT_ONLY",
            },
        }


@router.get("/api/ai-learning")
async def ai_learning_api(request: Request, _user: str = Depends(require_login)):
    _clusters, cluster = cluster_selection(request)
    return {"cluster_id": cluster.id, "cluster_name": cluster.name, **learning_status(cluster.id, cluster.name)}


@router.get("/ai-learning", response_class=HTMLResponse)
async def ai_learning_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    return templates.TemplateResponse(request, "ai_learning.html", {
        "user": user,
        "clusters": clusters,
        "cluster": cluster,
        **learning_status(cluster.id, cluster.name),
    })
