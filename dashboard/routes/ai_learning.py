"""Read-only visibility into supervised AI/forecast learning quality."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func

from config.settings import settings
from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import is_admin_user, require_login
from dashboard.templating import make_templates
from shared import (
    db,
    forecast_canary,
    forecast_feedback,
    learning_runtime,
    model_registry,
    online_learning_controls,
    remediation_feedback,
)
from shared.models import (
    Action,
    ChangeRiskAssessment,
    ForecastModelEvaluation,
    Incident,
    LogFaultStat,
    LogFinding,
    LogLearningSample,
    NodeResourceForecastRun,
    NodeResourceForecastAlert,
    NodeResourceForecastFeedback,
    NodeResourceModelState,
    OnlineLearnerCycleAudit,
    ForecastModelPromotionAudit,
    ForecastModelRegistry,
    PlaybookStat,
    RemediationCase,
    VolumeEarlyForecast,
    VolumeForecastRun,
    VolumeModelState,
)

router = APIRouter()
templates = make_templates()

_LARGE_OMAP_ACTION = "reshard_rgw_bucket"
_LARGE_OMAP_EVIDENCE_RE = re.compile(r"observed_at=([^\s]+)")


def _parse_observed_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def large_omap_readiness(cluster_id: str) -> dict:
    """Return a read-only commissioning report for LARGE_OMAP_OBJECTS.

    This report deliberately reads persisted evidence/cases only. It never
    changes policy, promotes a playbook, or creates a synthetic learning case.
    """
    now = datetime.utcnow()
    allowlisted_buckets = sorted({
        item.strip() for item in settings.large_omap_autoremediation_buckets.split(",")
        if item.strip()
    })
    max_age_hours = max(1, int(settings.large_omap_evidence_max_age_hours))

    with db.SessionLocal() as session:
        incident = session.query(Incident).filter(
            Incident.cluster_id == cluster_id,
            Incident.ceph_code == "LARGE_OMAP_OBJECTS",
        ).order_by(Incident.created_at.desc(), Incident.id.desc()).first()
        action = None
        case = None
        assessment = None
        if incident is not None:
            action = session.query(Action).filter_by(incident_id=incident.id).order_by(
                Action.created_at.desc(), Action.id.desc()
            ).first()
            if action is not None:
                case = session.query(RemediationCase).filter_by(action_id=action.id).one_or_none()
                assessment = session.query(ChangeRiskAssessment).filter_by(action_id=action.id).one_or_none()

        stats = session.query(PlaybookStat).filter(
            PlaybookStat.playbook_id == _LARGE_OMAP_ACTION,
            PlaybookStat.scope_key.like(f"cluster={cluster_id}|%"),
        ).order_by(PlaybookStat.updated_at.desc()).all()

    evidence_text = "\n".join(filter(None, [
        incident.log_excerpt if incident is not None else None,
        incident.signal_evidence_json if incident is not None else None,
    ]))
    observed_values = [
        parsed for parsed in (
            _parse_observed_at(raw) for raw in _LARGE_OMAP_EVIDENCE_RE.findall(evidence_text)
        ) if parsed is not None
    ]
    observed_at = max(observed_values) if observed_values else None
    age_hours = round((now - observed_at).total_seconds() / 3600, 2) if observed_at else None
    evidence_fresh = bool(
        observed_at is not None and age_hours is not None
        and age_hours >= -0.084  # tolerate at most five minutes of clock skew
        and age_hours <= max_age_hours
    )

    blockers: list[str] = []
    if not settings.large_omap_autoremediation_enabled:
        blockers.append("LARGE_OMAP_AUTOREMEDIATION_ENABLED đang tắt")
    if not allowlisted_buckets:
        blockers.append("chưa allowlist bucket RGW cụ thể")
    if observed_at is None:
        blockers.append("chưa có evidence LARGE_OMAP có observed_at")
    elif not evidence_fresh:
        blockers.append(f"evidence đã quá {max_age_hours} giờ hoặc lệch thời gian")
    if not stats:
        blockers.append("chưa có RemediationCase đủ provenance để tạo trust scope")
    elif not any(row.maturity_level == "L3" and not row.auto_disabled_reason for row in stats):
        blockers.append("playbook chưa được admin duyệt lên L3")
    if not settings.autopilot_enabled:
        blockers.append("Autopilot toàn cục đang tắt")

    latest_action = None
    if action is not None:
        latest_action = {
            "id": action.id,
            "action_id": action.action_id,
            "classification": action.classification,
            "status": action.status,
            "created_at": action.created_at,
            "case_outcome": case.outcome if case is not None else None,
            "case_id": case.id if case is not None else None,
            "risk_level": assessment.risk_level if assessment is not None else None,
            "risk_samples": assessment.sample_count if assessment is not None else 0,
        }

    trust_scopes = [{
        "id": row.id,
        "scope_key": row.scope_key,
        "playbook_version": row.playbook_version,
        "maturity_level": row.maturity_level,
        "proposed_count": row.proposed_count,
        "executed_count": row.executed_count,
        "verified_count": row.verified_count,
        "success_count": row.success_count,
        "failure_count": row.failure_count,
        "trust_score": round(row.trust_score, 4),
        "promotion_candidate_at": row.promotion_candidate_at,
        "promotion_blocked_reason": row.promotion_blocked_reason,
        "auto_disabled_reason": row.auto_disabled_reason,
        "updated_at": row.updated_at,
    } for row in stats]

    return {
        "fault_family": "LARGE_OMAP_OBJECTS",
        "autopilot_enabled": settings.autopilot_enabled,
        "autoremediation_enabled": settings.large_omap_autoremediation_enabled,
        "bootstrap_requires_approval": settings.large_omap_bootstrap_requires_approval,
        "allowlisted_buckets": allowlisted_buckets,
        "evidence_max_age_hours": max_age_hours,
        "latest_evidence": {
            "observed_at": observed_at,
            "age_hours": age_hours,
            "fresh": evidence_fresh,
            "incident_id": incident.id if incident is not None else None,
            "incident_status": incident.status if incident is not None else None,
        },
        "latest_action": latest_action,
        "trust_scopes": trust_scopes,
        "blockers": blockers,
        "ready_for_autonomy": not blockers,
        "next_step": (
            "Thu thập evidence mới có bucket/object/key count và PG trước khi allowlist production."
            if not evidence_fresh
            else "Duyệt Action bootstrap đầu tiên; sau đó chờ post-check và operator verdict VERIFIED_SUCCESS."
            if stats and not any(row.maturity_level == "L3" for row in stats)
            else "Xem các blocker ở trên; readiness report không tự thay đổi policy."
        ),
    }


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


def model_promotion_status(cluster_id: str, cluster_name: str) -> dict:
    """Return registry state and guarded-promotion evidence for the UI.

    This is read-only.  It never changes selected models or creates audit
    rows; only the explicit request/approve/rollback endpoints can do that.
    """
    node_prefix = f"{cluster_name}|"
    volume_prefix = f"{cluster_id}|"
    with db.SessionLocal() as session:
        rows = session.query(ForecastModelRegistry).filter(
            ((ForecastModelRegistry.scope_type == "NODE_RESOURCE")
             & ForecastModelRegistry.scope_key.startswith(node_prefix))
            | ((ForecastModelRegistry.scope_type == "VOLUME")
               & ForecastModelRegistry.scope_key.startswith(volume_prefix))
        ).order_by(ForecastModelRegistry.scope_type, ForecastModelRegistry.scope_key,
                   ForecastModelRegistry.created_at).all()
        output = []
        for row in rows:
            evaluations = session.query(ForecastModelEvaluation).filter_by(
                candidate_model_id=row.id,
            ).order_by(ForecastModelEvaluation.target_at.desc()).limit(20).all()
            audits = session.query(ForecastModelPromotionAudit).filter_by(
                candidate_model_id=row.id,
            ).order_by(ForecastModelPromotionAudit.created_at.desc()).limit(5).all()
            active = session.query(ForecastModelRegistry).filter_by(
                scope_type=row.scope_type, scope_key=row.scope_key, status="ACTIVE",
            ).one_or_none()
            decision = model_registry.evaluate_guarded_promotion(
                list(reversed(evaluations)),
            ) if row.status in {"CANDIDATE", "SHADOW"} and active else None
            output.append({
                "id": row.id,
                "scope_type": row.scope_type,
                "scope_key": row.scope_key,
                "version": row.version,
                "algorithm": row.algorithm,
                "training_window_hours": row.training_window_hours,
                "status": row.status,
                "promotion_reason": row.promotion_reason,
                "blocked_reason": row.blocked_reason,
                "evaluation_count": len(evaluations),
                "latest_evaluation": {
                    "target_at": evaluations[0].target_at,
                    "status": evaluations[0].status,
                    "reason": evaluations[0].reason,
                } if evaluations else None,
                "guard": {
                    "status": decision.status,
                    "allowed": decision.allowed,
                    "reason": decision.reason,
                    "checks": decision.checks,
                } if decision else None,
                "recent_audits": [{
                    "event_type": audit.event_type,
                    "actor": audit.actor,
                    "reason": audit.reason,
                    "created_at": audit.created_at,
                } for audit in audits],
                "can_rollback": any(audit.event_type == model_registry.PROMOTED for audit in audits),
            })
    return {
        "policy": {
            "minimum_outcomes": settings.forecast_promotion_min_outcomes,
            "required_consecutive_evaluations": settings.forecast_promotion_required_evaluations,
            "max_false_positive_rate_increase": settings.forecast_promotion_max_false_positive_rate_increase,
        },
        "models": output,
    }


def learning_status(cluster_id: str, cluster_name: str) -> dict:
    """Build a JSON-safe snapshot. No learning state is changed here."""
    with db.SessionLocal() as session:
        canary_metric = next(
            (item.strip().lower() for item in str(settings.online_learning_canary_metrics or "").split(",")
             if item.strip()),
            "cpu",
        )
        canary_host = str(settings.online_learning_canary_host or "").strip()
        online_learning_runtime = learning_runtime.evaluate(
            session, cluster_id, host=canary_host or None, metric=canary_metric,
        )
        canary_control = (
            online_learning_controls.get_control(
                session,
                cluster_id=cluster_id,
                host=canary_host,
                metric=canary_metric,
            ) if canary_host else None
        )
        operator_audits = online_learning_controls.list_audit(
            session, cluster_id=cluster_id, limit=50,
        )
        learner_cycles_query = session.query(OnlineLearnerCycleAudit).filter_by(
            cluster_key=str(cluster_id),
            host=canary_host,
            metric=canary_metric,
        ) if canary_host else session.query(OnlineLearnerCycleAudit).filter_by(
            cluster_key=str(cluster_id),
            host="",
            metric=canary_metric,
        )
        learner_cycle_count = learner_cycles_query.count()
        learner_cycle_totals = learner_cycles_query.with_entities(
            func.coalesce(func.sum(OnlineLearnerCycleAudit.processed), 0),
            func.coalesce(func.sum(OnlineLearnerCycleAudit.applied), 0),
            func.coalesce(func.sum(OnlineLearnerCycleAudit.failed), 0),
            func.avg(OnlineLearnerCycleAudit.elapsed_ms),
            func.avg(OnlineLearnerCycleAudit.cpu_time_ms),
        ).one()
        recent_learner_cycles = learner_cycles_query.order_by(
            OnlineLearnerCycleAudit.created_at.desc()
        ).limit(12).all()
        canary_report = forecast_canary.build_canary_report(
            session,
            cluster_id=cluster_id,
            cluster_name=cluster_name,
            host=canary_host or None,
            metric=canary_metric,
            lookback_hours=72,
        )
        states = session.query(NodeResourceModelState).filter_by(cluster_name=cluster_name).all()
        run_counts = dict(
            session.query(NodeResourceForecastRun.status, func.count(NodeResourceForecastRun.id))
            .filter(NodeResourceForecastRun.cluster_name == cluster_name)
            .group_by(NodeResourceForecastRun.status)
            .all()
        )
        forecast_alert_count = session.query(NodeResourceForecastAlert).filter_by(
            cluster_name=cluster_name,
        ).count()
        feedback_summary = forecast_feedback.summarize_feedback(
            session,
            cluster_name=cluster_name,
            trigger_threshold=settings.node_resource_forecast_trigger_threshold_percent,
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
            "online_learning": {
                **online_learning_runtime.as_dict(),
                "canary_scope": {
                    "host": canary_host or None,
                    "metric": canary_metric,
                },
                "control": {
                    "status": canary_control.status,
                    "reason": canary_control.reason,
                    "updated_by": canary_control.updated_by,
                    "updated_at": canary_control.updated_at,
                } if canary_control else {
                    "status": online_learning_controls.RUNNING,
                    "reason": "no explicit operator pause",
                    "updated_by": None,
                    "updated_at": None,
                },
                "operator_audits": [{
                    "action": row.action,
                    "actor": row.actor,
                    "host": row.host,
                    "metric": row.metric,
                    "reason": row.reason,
                    "target_id": row.target_id,
                    "created_at": row.created_at,
                } for row in operator_audits],
                "telemetry": {
                    "has_data": learner_cycle_count > 0,
                    "cycle_count": learner_cycle_count,
                    "processed": int(learner_cycle_totals[0] or 0),
                    "applied": int(learner_cycle_totals[1] or 0),
                    "failed": int(learner_cycle_totals[2] or 0),
                    "average_elapsed_ms": round(float(learner_cycle_totals[3]), 2)
                    if learner_cycle_totals[3] is not None else None,
                    "average_cpu_time_ms": round(float(learner_cycle_totals[4]), 2)
                    if learner_cycle_totals[4] is not None else None,
                    "recent_cycles": [{
                        "created_at": row.created_at,
                        "reason": row.reason,
                        "runtime_mode": row.runtime_mode,
                        "processed": row.processed,
                        "applied": row.applied,
                        "failed": row.failed,
                        "elapsed_ms": row.elapsed_ms,
                        "cpu_time_ms": row.cpu_time_ms,
                    } for row in recent_learner_cycles],
                },
                "evidence_report": canary_report,
            },
            "forecast_feedback": {
                **feedback_summary,
                "coverage": (
                    round(feedback_summary["labeled_count"] / max(1, forecast_alert_count), 4)
                    if forecast_alert_count else 0.0
                ),
            },
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
            "model_promotion": model_promotion_status(cluster_id, cluster_name),
        }


def _require_admin(user: str) -> None:
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin mới được điều khiển online learner.")


def _control_scope(cluster_id: str, host: str, metric: str) -> tuple[str, str, str]:
    try:
        return online_learning_controls.normalize_scope(
            cluster_id=cluster_id, host=host, metric=metric,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/ai-learning")
async def ai_learning_api(request: Request, _user: str = Depends(require_login)):
    _clusters, cluster = cluster_selection(request)
    return {"cluster_id": cluster.id, "cluster_name": cluster.name, **learning_status(cluster.id, cluster.name), "large_omap_readiness": large_omap_readiness(cluster.id)}


@router.post("/api/ai-learning/models/{model_id}/promotion-request")
async def request_model_promotion(
    model_id: str, user: str = Depends(require_login),
):
    """Ask for promotion; this never changes the active model."""
    with db.SessionLocal() as session:
        try:
            decision = model_registry.request_promotion(
                session, candidate_id=model_id, actor=user,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "allowed": decision.allowed,
            "status": decision.status,
            "reason": decision.reason,
            "checks": decision.checks,
        }


@router.post("/api/ai-learning/models/{model_id}/promote")
async def approve_model_promotion(
    model_id: str, user: str = Depends(require_login),
):
    """Explicit operator approval required before a model becomes active."""
    with db.SessionLocal() as session:
        try:
            row = model_registry.approve_promotion(
                session, candidate_id=model_id, actor=user,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": row.id, "status": row.status, "version": row.version}


@router.post("/api/ai-learning/models/{model_id}/rollback")
async def rollback_model_promotion(
    model_id: str, user: str = Depends(require_login),
):
    """Restore the exact previous active model recorded in promotion audit."""
    with db.SessionLocal() as session:
        try:
            row = model_registry.rollback_promotion(
                session, candidate_id=model_id, actor=user,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": row.id, "status": row.status, "version": row.version}


@router.post("/api/ai-learning/learner/pause")
async def pause_online_learner(
    cluster_id: str = Form(...),
    host: str = Form(...),
    metric: str = Form(...),
    reason: str = Form(...),
    user: str = Depends(require_login),
):
    """Pause exactly one learner stream; no global flag or model is changed."""

    _require_admin(user)
    _control_scope(cluster_id, host, metric)
    with db.SessionLocal() as session:
        try:
            row = online_learning_controls.set_status(
                session,
                cluster_id=cluster_id,
                host=host,
                metric=metric,
                status=online_learning_controls.PAUSED,
                actor=user,
                reason=reason,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": row.status, "host": row.host, "metric": row.metric, "reason": row.reason}


@router.post("/api/ai-learning/learner/resume")
async def resume_online_learner(
    cluster_id: str = Form(...),
    host: str = Form(...),
    metric: str = Form(...),
    reason: str = Form(...),
    user: str = Depends(require_login),
):
    """Resume exactly one previously paused stream after explicit approval."""

    _require_admin(user)
    _control_scope(cluster_id, host, metric)
    with db.SessionLocal() as session:
        try:
            row = online_learning_controls.set_status(
                session,
                cluster_id=cluster_id,
                host=host,
                metric=metric,
                status=online_learning_controls.RUNNING,
                actor=user,
                reason=reason,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": row.status, "host": row.host, "metric": row.metric, "reason": row.reason}


@router.post("/api/ai-learning/learner/reset")
async def reset_online_learner(
    cluster_id: str = Form(...),
    host: str = Form(...),
    metric: str = Form(...),
    reason: str = Form(...),
    confirmation: str = Form(...),
    user: str = Depends(require_login),
):
    """Reset one durable state only after an explicit RESET confirmation."""

    _require_admin(user)
    if confirmation.strip().upper() != "RESET":
        raise HTTPException(status_code=422, detail="Nhập chính xác RESET để xác nhận.")
    _control_scope(cluster_id, host, metric)
    with db.SessionLocal() as session:
        try:
            removed = online_learning_controls.reset_state(
                session,
                cluster_id=cluster_id,
                host=host,
                metric=metric,
                actor=user,
                reason=reason,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "RESET", "removed_states": removed, "host": host, "metric": metric}


@router.get("/api/ai-learning/operator-audit")
async def online_learner_operator_audit(
    request: Request,
    _user: str = Depends(require_login),
):
    cluster_id = str(request.query_params.get("cluster_id") or "").strip()
    if not cluster_id:
        raise HTTPException(status_code=422, detail="cluster_id là bắt buộc.")
    with db.SessionLocal() as session:
        rows = online_learning_controls.list_audit(
            session,
            cluster_id=cluster_id,
            limit=int(request.query_params.get("limit") or 100),
        )
        return [{
            "action": row.action,
            "actor": row.actor,
            "host": row.host,
            "metric": row.metric,
            "reason": row.reason,
            "target_id": row.target_id,
            "created_at": row.created_at,
        } for row in rows]


@router.post("/api/ai-learning/models/{model_id}/block")
async def block_model_candidate(
    model_id: str,
    reason: str = Form(...),
    user: str = Depends(require_login),
):
    """Block one candidate and append the guarded-promotion audit event."""

    _require_admin(user)
    with db.SessionLocal() as session:
        try:
            row = model_registry.block_candidate(
                session, candidate_id=model_id, actor=user, reason=reason,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": row.id, "status": row.status, "version": row.version}


@router.post("/api/ai-learning/node-alerts/{alert_id}/feedback")
async def node_forecast_feedback(
    alert_id: str,
    verdict: str = Form(...),
    note: str = Form(""),
    impact_percent: float | None = Form(None),
    incident_id: str = Form(""),
    remediation_case_id: str = Form(""),
    user: str = Depends(require_login),
):
    """Append an operator verdict; this never changes lifecycle or policy."""
    with db.SessionLocal() as session:
        feedback = forecast_feedback.add_feedback(
            session,
            alert_id=alert_id,
            verdict=verdict,
            submitted_by=user,
            note=note,
            impact_percent=impact_percent,
            incident_id=incident_id,
            remediation_case_id=remediation_case_id,
        )
        session.commit()
        return {
            "id": feedback.id,
            "alert_id": feedback.alert_id,
            "verdict": feedback.verdict,
            "submitted_by": feedback.submitted_by,
        }


@router.get("/ai-learning", response_class=HTMLResponse)
async def ai_learning_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    return templates.TemplateResponse(request, "ai_learning.html", {
        "user": user,
        "clusters": clusters,
        "cluster": cluster,
        **learning_status(cluster.id, cluster.name),
        "large_omap_readiness": large_omap_readiness(cluster.id),
    })
