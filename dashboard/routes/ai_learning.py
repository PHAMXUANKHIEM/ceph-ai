"""Read-only visibility into supervised AI/forecast learning quality."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func

from config.settings import settings
from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import (
    LogFaultStat,
    LogFinding,
    LogLearningSample,
    NodeResourceForecastRun,
    NodeResourceModelState,
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
            "resource_learning": {
                "enabled": settings.node_resource_forecast_enabled,
                "evaluation_hours": settings.node_resource_learning_evaluation_hours,
                "minimum_outcomes": settings.node_resource_learning_min_outcomes,
                "candidate_windows": settings.node_resource_learning_candidate_hours,
                "run_counts": run_counts,
                "selected_models": [row for row in resource_models if row["selected"]],
                "candidate_models": resource_models,
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
