"""Read-only visibility into supervised AI/forecast learning quality."""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from shared.time import utc_now

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func

from config.settings import settings
from dashboard.cluster_scope import cluster_selection
from dashboard.routes.auth import is_admin_user, require_login
from dashboard.templating import make_templates
from shared import (
    canary,
    db,
    forecast_feedback,
    learning_runtime,
    model_quality_report,
    model_registry,
    online_learning_controls,
    remediation_feedback,
)
from shared.models import (
    Action,
    Cluster,
    ChangeRiskAssessment,
    ForecastModelEvaluation,
    Incident,
    LogFaultStat,
    LogFinding,
    LogLearningSample,
    NodeResourceForecastRun,
    NodeResourceForecastAlert,
    NodeResourceForecastTransition,
    NodeResourceModelState,
    ForecastModelPromotionAudit,
    ForecastModelRegistry,
    OnlineLearnerAudit,
    OnlineLearnerCycleAudit,
    PlaybookStat,
    RemediationCase,
    VolumeEarlyForecast,
    VolumeForecastRun,
    VolumeModelState,
    HostMetricSample,
)
from watcher.forecast_replay import evaluate_shadow

router = APIRouter()
templates = make_templates()

_LARGE_OMAP_ACTION = "reshard_rgw_bucket"
_LARGE_OMAP_EVIDENCE_RE = re.compile(r"observed_at=([^\s]+)")
_REPLAY_COLUMNS = {
    "cpu": HostMetricSample.cpu_percent,
    "ram": HostMetricSample.mem_percent,
}


def _parse_replay_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=422, detail=f"{field_name} là bắt buộc.")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"{field_name} không đúng ISO datetime.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _replay_options(cluster_id: str) -> dict:
    with db.SessionLocal() as session:
        hosts = [row[0] for row in session.query(HostMetricSample.host).filter(
            HostMetricSample.cluster_id == cluster_id,
        ).distinct().order_by(HostMetricSample.host).all()]
        minimum, maximum = session.query(
            func.min(HostMetricSample.collected_at), func.max(HostMetricSample.collected_at),
        ).filter(HostMetricSample.cluster_id == cluster_id).one()
    return {"hosts": hosts, "minimum_at": minimum, "maximum_at": maximum}


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
    now = utc_now()
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
def _interval_chart(
    current: float | None,
    predicted: float | None,
    predicted_low: float | None,
    predicted_high: float | None,
) -> dict[str, float] | None:
    """Return normalized positions for a bounded forecast interval visual."""

    if predicted_low is None or predicted_high is None:
        return None
    values = [current, predicted, predicted_low, predicted_high]
    if any(value is None for value in values):
        return None
    low = min(float(predicted_low), float(predicted_high))
    high = max(float(predicted_low), float(predicted_high))
    span = max(high - low, 0.0)
    padding = max(span * 0.08, 1e-9)
    lower = low - padding
    upper = high + padding
    scale = max(upper - lower, 1e-9)

    def position(value: float) -> float:
        return round(max(0.0, min(100.0, (float(value) - lower) / scale * 100.0)), 3)

    interval_low = position(low)
    interval_high = position(high)
    return {
        "low": interval_low,
        "high": interval_high,
        "width": round(max(0.0, interval_high - interval_low), 3),
        "current": position(float(current)),
        "predicted": position(float(predicted)),
    }


def _model_quality_summary(evaluations: list[ForecastModelEvaluation]) -> dict:
    """Aggregate bounded model-quality evidence for the read-only dashboard."""

    def mean(name: str) -> float | None:
        values = [float(getattr(row, name)) for row in evaluations if getattr(row, name) is not None]
        return round(sum(values) / len(values), 4) if values else None

    evidence = []
    for row in evaluations:
        try:
            payload = json.loads(row.evidence_json or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            evidence.append(payload)
    return {
        "evaluations": len(evaluations),
        "paired_outcomes": sum(min(int(row.active_evaluated), int(row.candidate_evaluated)) for row in evaluations),
        "active_mae": mean("active_mae"),
        "candidate_mae": mean("candidate_mae"),
        "active_smape": mean("active_smape"),
        "candidate_smape": mean("candidate_smape"),
        "candidate_bias": mean("candidate_bias"),
        "drifted": sum(payload.get("candidate_drift_status") == "DRIFT" for payload in evidence),
        "resource_budget_failures": sum(payload.get("resource_budget_ok") is False for payload in evidence),
    }


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
                "scope_schema": row.scope_schema,
                "scope_ready": model_registry._scope_ready(row),
                "evaluation_count": len(evaluations),
                "quality": _model_quality_summary(evaluations),
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
        "counts": {
            "eligible": sum(1 for item in output if (item.get("guard") or {}).get("allowed")),
            "blocked": sum(1 for item in output if item.get("guard") and not item["guard"].get("allowed")),
            "active": sum(1 for item in output if item.get("status") == "ACTIVE"),
        },
        "models": output,
    }


def learning_status(cluster_id: str, cluster_name: str) -> dict:
    """Build a JSON-safe snapshot. No learning state is changed here."""
    with db.SessionLocal() as session:
        states = session.query(NodeResourceModelState).filter_by(cluster_name=cluster_name).all()
        active_registry_rows = session.query(ForecastModelRegistry).filter(
            ForecastModelRegistry.status == "ACTIVE",
            ForecastModelRegistry.scope_type.in_(("NODE_RESOURCE", "VOLUME")),
        ).all()
        active_model_versions = {
            (row.scope_type, row.scope_key): row.version
            for row in active_registry_rows
        }
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
        now = utc_now()
        alert_rows = session.query(NodeResourceForecastAlert).filter_by(
            cluster_name=cluster_name,
        ).order_by(NodeResourceForecastAlert.last_detected_at.desc()).limit(100).all()
        alert_ids = [row.id for row in alert_rows]
        transition_rows = (
            session.query(NodeResourceForecastTransition)
            .filter(NodeResourceForecastTransition.alert_id.in_(alert_ids))
            .order_by(NodeResourceForecastTransition.changed_at.desc())
            .all()
            if alert_ids else []
        )
        transitions_by_alert: dict[str, list[NodeResourceForecastTransition]] = {}
        for transition in transition_rows:
            history = transitions_by_alert.setdefault(transition.alert_id, [])
            if len(history) < 10:
                history.append(transition)

        freshness_limit = max(120, settings.node_health_scan_interval_seconds * 2)

        def _freshness(observed_at: datetime | None) -> tuple[float | None, str]:
            if observed_at is None:
                return None, "UNKNOWN"
            age = max(0.0, (now - observed_at).total_seconds())
            return round(age, 1), "FRESH" if age <= freshness_limit else "STALE"

        node_alerts = []
        for alert in alert_rows:
            freshness_seconds, freshness_status = _freshness(alert.latest_observed_at)
            if alert.coverage_ratio is None and alert.max_gap_hours is None:
                quality_status = "LEGACY_NO_EVIDENCE"
                quality_reason = "Alert cũ chưa có quality evidence được lưu persistent."
            elif alert.coverage_ratio is not None and alert.coverage_ratio < settings.node_resource_forecast_min_coverage:
                quality_status = "LOW_COVERAGE"
                quality_reason = f"Coverage {alert.coverage_ratio:.3f} thấp hơn ngưỡng {settings.node_resource_forecast_min_coverage:.3f}."
            elif alert.max_gap_hours is not None and alert.max_gap_hours > settings.node_resource_forecast_max_gap_hours:
                quality_status = "GAP_DETECTED"
                quality_reason = f"Gap dài nhất {alert.max_gap_hours:.2f}h vượt ngưỡng {settings.node_resource_forecast_max_gap_hours:.2f}h."
            else:
                quality_status = "OK"
                quality_reason = "Window dữ liệu đạt quality gate."
            node_alerts.append({
                "id": alert.id,
                "host": alert.host,
                "metric": alert.metric.upper(),
                "status": alert.status,
                "lifecycle_state": alert.lifecycle_state or alert.status,
                "notification_state": alert.notification_state,
                "state_reason": alert.state_reason or "Chưa có lý do transition được lưu.",
                "evidence_version": alert.evidence_version,
                "state_changed_at": alert.state_changed_at,
                "first_detected_at": alert.first_detected_at,
                "last_detected_at": alert.last_detected_at,
                "current_percent": round(alert.current_percent, 2),
                "predicted_percent": round(alert.predicted_percent, 2),
                "predicted_low": round(alert.predicted_low, 2) if alert.predicted_low is not None else None,
                "predicted_high": round(alert.predicted_high, 2) if alert.predicted_high is not None else None,
                "confidence": round(alert.confidence, 3),
                "consensus_status": alert.consensus_status,
                "consensus_ratio": round(alert.consensus_ratio, 3) if alert.consensus_ratio is not None else None,
                "consensus_candidate_count": alert.consensus_candidate_count,
                "coverage_ratio": round(alert.coverage_ratio, 3) if alert.coverage_ratio is not None else None,
                "max_gap_hours": round(alert.max_gap_hours, 3) if alert.max_gap_hours is not None else None,
                "latest_observed_at": alert.latest_observed_at,
                "freshness_seconds": freshness_seconds,
                "freshness_status": freshness_status,
                "quality_status": quality_status,
                "quality_reason": quality_reason,
                "transitions": [{
                    "previous_state": transition.previous_state,
                    "new_state": transition.new_state,
                    "reason": transition.reason,
                    "evidence_version": transition.evidence_version,
                    "changed_at": transition.changed_at,
                } for transition in transitions_by_alert.get(alert.id, [])],
            })

        resource_models = []
        for state in sorted(states, key=lambda row: (row.host, row.metric, row.window_hours)):
            status, reason, accuracy = _quality(state.mean_absolute_error, state.evaluated_count)
            latest = (
                session.query(NodeResourceForecastRun)
                .filter_by(
                    cluster_name=cluster_name, host=state.host,
                    metric=state.metric, algorithm=state.algorithm,
                    window_hours=state.window_hours,
                )
                .order_by(NodeResourceForecastRun.predicted_at.desc())
                .first()
            )
            latest_quality = (
                latest.status
                if latest and latest.status in {"DATA_QUALITY", "LOW_CONFIDENCE"}
                else latest.consensus_status
                if latest and latest.consensus_status in {"DATA_QUALITY", "LOW_CONFIDENCE"}
                else ("OK" if latest else "NO_FORECAST")
            )
            node_scope = f"{cluster_name}|{state.host}|{state.metric.lower()}"
            resource_models.append({
                "host": state.host,
                "metric": state.metric.upper(),
                "window_hours": state.window_hours,
                "algorithm": latest.algorithm if latest else state.algorithm,
                "model_version": active_model_versions.get(("NODE_RESOURCE", node_scope)),
                "selected": state.selected,
                "evaluated_count": state.evaluated_count,
                "mae": round(state.mean_absolute_error, 3) if state.mean_absolute_error is not None else None,
                "rolling_mae": round(
                    state.rolling_mae if state.rolling_mae is not None else state.mean_absolute_error, 3
                ) if (state.rolling_mae is not None or state.mean_absolute_error is not None) else None,
                "rolling_smape": round(state.rolling_smape, 3) if state.rolling_smape is not None else None,
                "last_error": round(state.last_absolute_error, 3) if state.last_absolute_error is not None else None,
                "accuracy_estimate": accuracy,
                "quality_status": status,
                "quality_reason": reason,
                "data_quality": latest_quality,
                "latest_status": latest.status if latest else None,
                "latest_current": round(latest.current_percent, 2) if latest else None,
                "latest_confidence": round(latest.confidence, 3) if latest else None,
                "latest_prediction": round(latest.predicted_percent, 2) if latest else None,
                "latest_actual": round(latest.actual_percent, 2) if latest and latest.actual_percent is not None else None,
                "predicted_low": round(latest.predicted_low, 2) if latest and latest.predicted_low is not None else None,
                "predicted_high": round(latest.predicted_high, 2) if latest and latest.predicted_high is not None else None,
                "interval_chart": _interval_chart(
                    latest.current_percent if latest else None,
                    latest.predicted_percent if latest else None,
                    latest.predicted_low if latest else None,
                    latest.predicted_high if latest else None,
                ),
                "consensus_status": latest.consensus_status if latest else None,
                "consensus_ratio": round(latest.consensus_ratio, 3) if latest and latest.consensus_ratio is not None else None,
                "consensus_candidate_count": latest.consensus_candidate_count if latest else None,
                "latest_predicted_at": latest.predicted_at if latest else None,
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
                "algorithm": latest.algorithm if latest else state.algorithm,
                "model_version": active_model_versions.get((
                    "VOLUME", f"{cluster_id}|{state.pool}|{state.image}|{state.metric}"
                )),
                "selected": state.selected,
                "evaluated_count": state.evaluated_count,
                "mae": round(state.mean_absolute_error, 3) if state.mean_absolute_error is not None else None,
                "mape": round(state.mean_percentage_error, 3) if state.mean_percentage_error is not None else None,
                "last_error": round(state.last_absolute_error, 3) if state.last_absolute_error is not None else None,
                "accuracy_estimate": accuracy,
                "quality_status": status,
                "quality_reason": reason,
                "rolling_mae": round(
                    state.rolling_mae if state.rolling_mae is not None else state.mean_absolute_error, 3
                ) if (state.rolling_mae is not None or state.mean_absolute_error is not None) else None,
                "rolling_smape": round(state.rolling_smape, 3) if state.rolling_smape is not None else None,
                "latest_prediction": round(latest.predicted_value, 3) if latest else None,
                "latest_confidence": round(latest.confidence, 3) if latest else None,
                "latest_actual": round(latest.actual_value, 3) if latest and latest.actual_value is not None else None,
                "seasonal_scope": latest.seasonal_scope if latest else None,
                "training_samples": latest.training_samples if latest else None,
                "latest_status": latest.status if latest else None,
                "updated_at": state.updated_at,
            })
        volume_model_metrics = {
            (model["pool"], model["image"], model["metric"]): model
            for model in volume_models
        }

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
            "actual_value": None,
            "predicted_low": round(row.predicted_low, 3) if row.predicted_low is not None else None,
            "predicted_high": round(row.predicted_high, 3) if row.predicted_high is not None else None,
            "threshold_type": row.threshold_type,
            "threshold_value": round(row.threshold_value, 3) if row.threshold_value is not None else None,
            "confidence": round(row.confidence, 3),
            "consensus_status": row.consensus_status,
            "consensus_ratio": round(row.consensus_ratio, 3) if row.consensus_ratio is not None else None,
            "consensus_candidate_count": row.consensus_candidate_count,
            "training_samples": row.training_samples,
            "training_window_hours": row.training_window_hours,
            "seasonal_scope": row.seasonal_scope,
            "model_version": row.model_version,
            "algorithm": "seasonal_baseline",
            "data_quality": row.status if row.status == "DATA_QUALITY" else "OK",
            "rolling_mae": volume_model_metrics.get((row.pool, row.image, row.metric), {}).get("rolling_mae"),
            "rolling_smape": volume_model_metrics.get((row.pool, row.image, row.metric), {}).get("rolling_smape"),
            "status": row.status, "reason": row.reason,
            "generated_at": row.generated_at, "target_at": row.target_at,
            "source_latest_at": row.source_latest_at,
            "interval_chart": _interval_chart(
                row.current_value,
                row.predicted_value,
                row.predicted_low,
                row.predicted_high,
            ),
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

        canary_metric = next(
            (item.strip().lower() for item in str(settings.online_learning_canary_metrics or "").split(",")
             if item.strip()),
            "cpu",
        )
        runtime = learning_runtime.evaluate(
            session,
            cluster_id,
            host=settings.online_learning_canary_host or None,
            metric=canary_metric,
        ).as_dict()
        learner_cluster_key = str(cluster_id)
        learner_sample_count = session.query(OnlineLearnerAudit).filter(
            OnlineLearnerAudit.cluster_key == learner_cluster_key,
        ).count()
        learner_quality_rows = session.query(
            OnlineLearnerAudit.quality_status, func.count(OnlineLearnerAudit.id),
        ).filter(
            OnlineLearnerAudit.cluster_key == learner_cluster_key,
        ).group_by(OnlineLearnerAudit.quality_status).all()
        learner_quality_counts = {status: count for status, count in learner_quality_rows}
        latest_learner = session.query(OnlineLearnerAudit).filter(
            OnlineLearnerAudit.cluster_key == learner_cluster_key,
        ).order_by(OnlineLearnerAudit.created_at.desc()).first()
        latest_applied = session.query(OnlineLearnerAudit).filter(
            OnlineLearnerAudit.cluster_key == learner_cluster_key,
            OnlineLearnerAudit.update_applied.is_(True),
        ).order_by(OnlineLearnerAudit.created_at.desc()).first()
        latest_cycle = session.query(OnlineLearnerCycleAudit).filter(
            OnlineLearnerCycleAudit.cluster_key == learner_cluster_key,
        ).order_by(OnlineLearnerCycleAudit.created_at.desc()).first()
        canary_host = str(settings.online_learning_canary_host or "").strip()
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
        drift_rows = session.query(
            NodeResourceForecastRun.drift_status, func.count(NodeResourceForecastRun.id),
        ).filter(
            NodeResourceForecastRun.cluster_name == cluster_name,
        ).group_by(NodeResourceForecastRun.drift_status).all()
        online_learning = {
            "feature_enabled": bool(settings.online_learning_enabled),
            "configured_mode": str(settings.online_learning_mode or ""),
            "effective_mode": runtime["mode"],
            "runtime_reason": runtime["reason"],
            "canary_enabled": bool(settings.online_learning_canary_enabled),
            "canary_scope": {
                "cluster_id": settings.online_learning_canary_cluster_id or None,
                "host": canary_host or None,
                "metrics": canary_metric,
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
            "runtime": runtime,
            "model_version": latest_learner.model_version if latest_learner else None,
            "sample_count": learner_sample_count,
            "quality_gate_counts": learner_quality_counts,
            "drift_state_counts": {status: count for status, count in drift_rows},
            "verified_feedback_count": feedback_summary.get("labeled_count", 0),
            "last_sample_at": latest_learner.created_at if latest_learner else None,
            "last_learned_at": latest_applied.created_at if latest_applied else None,
            "latest_cycle": {
                "processed": latest_cycle.processed,
                "applied": latest_cycle.applied,
                "failed": latest_cycle.failed,
                "skipped": latest_cycle.skipped,
                "elapsed_ms": round(latest_cycle.elapsed_ms, 2),
                "cpu_time_ms": round(latest_cycle.cpu_time_ms, 2),
                "reason": latest_cycle.reason,
                "runtime_mode": latest_cycle.runtime_mode,
                "created_at": latest_cycle.created_at,
            } if latest_cycle else None,
        }

        return {
            "online_learning": online_learning,
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
            "node_alerts": node_alerts,
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


def _require_selected_cluster(request: Request, cluster_id: str):
    """Reject cross-cluster mutations instead of trusting a posted id."""
    requested = str(cluster_id or "").strip()
    if not requested:
        raise HTTPException(status_code=422, detail="cluster_id là bắt buộc.")
    _clusters, selected = cluster_selection(request)
    if requested != selected.id:
        raise HTTPException(
            status_code=409,
            detail=(
                "Thao tác chỉ được phép trên cụm đang được chọn; "
                "hãy tải lại trang sau khi chuyển cụm."
            ),
        )
    return selected


def _model_belongs_to_cluster(row: ForecastModelRegistry, cluster) -> bool:
    """Match the registry's historical scope key to the selected cluster."""
    scope_key = str(row.scope_key or "")
    if row.scope_type == "VOLUME":
        return scope_key.split("|", 1)[0] == cluster.id
    if row.scope_type == "NODE_RESOURCE":
        return scope_key.split("|", 1)[0] == cluster.name
    return False


def _require_model_scope(session, model_id: str, cluster) -> ForecastModelRegistry:
    row = session.get(ForecastModelRegistry, model_id)
    if row is None or not _model_belongs_to_cluster(row, cluster):
        raise HTTPException(
            status_code=404,
            detail="Không tìm thấy model trong cụm đang được chọn.",
        )
    return row


@router.get("/api/ai-learning")
async def ai_learning_api(request: Request, _user: str = Depends(require_login)):
    _clusters, cluster = cluster_selection(request)
    return {"cluster_id": cluster.id, "cluster_name": cluster.name, **learning_status(cluster.id, cluster.name), "large_omap_readiness": large_omap_readiness(cluster.id)}


@router.get("/api/ai-learning/model-quality-report")
async def model_quality_report_api(request: Request, hours: int = 24,
                                   _user: str = Depends(require_login)):
    """Summarize persisted evaluations; never train or promote during HTTP reads."""
    _clusters, cluster = cluster_selection(request)
    try:
        return model_quality_report.quality_report(cluster.id, hours=hours)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/ai-learning/canary")
async def ai_learning_canary_api(request: Request, _user: str = Depends(require_login)):
    """Return read-only canary acceptance evidence for the selected cluster."""
    _clusters, cluster = cluster_selection(request)
    with db.SessionLocal() as session:
        return canary.build_canary_report(
            session, cluster_id=cluster.id, cluster_name=cluster.name,
        )


@router.post("/api/ai-learning/replay")
async def ai_learning_replay(request: Request, _user: str = Depends(require_login)):
    """Run a bounded, read-only historical replay; never persists or remediates."""
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Replay payload không hợp lệ.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="Replay payload phải là object JSON.")

    _clusters, selected_cluster = cluster_selection(request)
    cluster_id = str(payload.get("cluster_id") or selected_cluster.id).strip()
    if cluster_id != selected_cluster.id:
        raise HTTPException(
            status_code=409,
            detail="Replay chỉ được phép trên cụm đang được chọn.",
        )
    host = str(payload.get("host") or "").strip()
    metric = str(payload.get("metric") or "").strip().lower()
    if metric not in _REPLAY_COLUMNS:
        raise HTTPException(status_code=422, detail="Metric replay chỉ hỗ trợ cpu hoặc ram.")
    if not host or len(host) > 255:
        raise HTTPException(status_code=422, detail="Host replay không hợp lệ.")
    start_at = _parse_replay_datetime(payload.get("start_at"), "start_at")
    end_at = _parse_replay_datetime(payload.get("end_at"), "end_at")
    if end_at <= start_at:
        raise HTTPException(status_code=422, detail="end_at phải sau start_at.")
    if end_at - start_at > timedelta(days=31):
        raise HTTPException(status_code=422, detail="Replay tối đa 31 ngày mỗi lần chạy.")

    try:
        horizon_hours = int(payload.get("horizon_hours", 1))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="horizon_hours phải là số nguyên.") from exc
    if not 1 <= horizon_hours <= 168:
        raise HTTPException(status_code=422, detail="horizon_hours phải nằm trong khoảng 1–168.")

    raw_windows = payload.get("window_hours", [6, 24, 72])
    if isinstance(raw_windows, str):
        raw_windows = raw_windows.split(",")
    if not isinstance(raw_windows, list):
        raise HTTPException(status_code=422, detail="window_hours phải là danh sách.")
    try:
        windows = sorted({int(value) for value in raw_windows})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="window_hours chứa giá trị không hợp lệ.") from exc
    if not windows or len(windows) > 8 or any(window < 1 or window > 720 for window in windows):
        raise HTTPException(status_code=422, detail="window_hours phải có 1–8 giá trị trong khoảng 1–720.")

    with db.SessionLocal() as session:
        if session.get(Cluster, cluster_id) is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy cluster.")
        rows = session.query(HostMetricSample).filter(
            HostMetricSample.cluster_id == cluster_id,
            HostMetricSample.host == host,
            HostMetricSample.collected_at >= start_at,
            HostMetricSample.collected_at <= end_at,
        ).order_by(HostMetricSample.collected_at.desc()).limit(
            settings.learning_job_max_batch_size
        ).all()

    if not rows:
        raise HTTPException(status_code=422, detail="Không có host metric trong khoảng thời gian đã chọn.")

    # The pure replay helper works on hourly points. Aggregate raw host scans
    # into hourly means so horizon_hours remains a time horizon, not a row
    # count, while keeping the operation read-only and bounded.
    buckets: dict[datetime, list[float]] = {}
    value_column = _REPLAY_COLUMNS[metric]
    for row in rows:
        bucket = row.collected_at.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(bucket, []).append(float(getattr(row, value_column.key)))
    points = [
        (bucket, round(sum(values) / len(values), 4))
        for bucket, values in sorted(buckets.items())
    ]
    minimum_points = max(6, settings.node_resource_forecast_min_samples + horizon_hours)
    if len(points) < minimum_points:
        raise HTTPException(
            status_code=422,
            detail=f"Cần ít nhất {minimum_points} điểm theo giờ; hiện có {len(points)}.",
        )

    replay = evaluate_shadow(
        points, metric, horizon_hours=horizon_hours,
        window_hours=windows, minimum_evaluated=1,
    )
    return {
        "read_only": True,
        "remediation_executed": False,
        "cluster_id": cluster_id,
        "host": host,
        "metric": metric,
        "start_at": start_at,
        "end_at": end_at,
        "source_rows": len(rows),
        "hourly_points": len(points),
        "horizon_hours": horizon_hours,
        "window_hours": windows,
        "metrics": {name: asdict(metrics) for name, metrics in replay["metrics"].items()},
        "comparison": asdict(replay["comparison"]),
    }


@router.post("/api/ai-learning/models/{model_id}/promotion-request")
async def request_model_promotion(
    model_id: str, request: Request, user: str = Depends(require_login),
):
    """Ask for promotion; this never changes the active model."""
    _clusters, cluster = cluster_selection(request)
    with db.SessionLocal() as session:
        _require_model_scope(session, model_id, cluster)
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
    model_id: str, request: Request, user: str = Depends(require_login),
):
    """Explicit operator approval required before a model becomes active."""
    _require_admin(user)
    _clusters, cluster = cluster_selection(request)
    with db.SessionLocal() as session:
        _require_model_scope(session, model_id, cluster)
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
    model_id: str, request: Request, user: str = Depends(require_login),
):
    """Restore the exact previous active model recorded in promotion audit."""
    _require_admin(user)
    _clusters, cluster = cluster_selection(request)
    with db.SessionLocal() as session:
        _require_model_scope(session, model_id, cluster)
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
    request: Request,
    cluster_id: str = Form(...), host: str = Form(...), metric: str = Form(...),
    reason: str = Form(...), user: str = Depends(require_login),
):
    """Pause exactly one learner stream; no global flag or model is changed."""
    _require_admin(user)
    _require_selected_cluster(request, cluster_id)
    _control_scope(cluster_id, host, metric)
    with db.SessionLocal() as session:
        try:
            row = online_learning_controls.set_status(
                session, cluster_id=cluster_id, host=host, metric=metric,
                status=online_learning_controls.PAUSED, actor=user, reason=reason,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": row.status, "host": row.host, "metric": row.metric, "reason": row.reason}


@router.post("/api/ai-learning/learner/resume")
async def resume_online_learner(
    request: Request,
    cluster_id: str = Form(...), host: str = Form(...), metric: str = Form(...),
    reason: str = Form(...), user: str = Depends(require_login),
):
    """Resume exactly one paused stream after explicit operator approval."""
    _require_admin(user)
    _require_selected_cluster(request, cluster_id)
    _control_scope(cluster_id, host, metric)
    with db.SessionLocal() as session:
        try:
            row = online_learning_controls.set_status(
                session, cluster_id=cluster_id, host=host, metric=metric,
                status=online_learning_controls.RUNNING, actor=user, reason=reason,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": row.status, "host": row.host, "metric": row.metric, "reason": row.reason}


@router.post("/api/ai-learning/learner/reset")
async def reset_online_learner(
    request: Request,
    cluster_id: str = Form(...), host: str = Form(...), metric: str = Form(...),
    reason: str = Form(...), confirmation: str = Form(...),
    user: str = Depends(require_login),
):
    """Reset one durable state only after explicit RESET confirmation."""
    _require_admin(user)
    if confirmation.strip().upper() != "RESET":
        raise HTTPException(status_code=422, detail="Nhập chính xác RESET để xác nhận.")
    _require_selected_cluster(request, cluster_id)
    _control_scope(cluster_id, host, metric)
    with db.SessionLocal() as session:
        try:
            removed = online_learning_controls.reset_state(
                session, cluster_id=cluster_id, host=host, metric=metric,
                actor=user, reason=reason,
            )
            session.commit()
        except ValueError as exc:
            session.rollback()
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"status": "RESET", "removed_states": removed, "host": host, "metric": metric}


@router.get("/api/ai-learning/operator-audit")
async def online_learner_operator_audit(
    request: Request, _user: str = Depends(require_login),
):
    cluster_id = str(request.query_params.get("cluster_id") or "").strip()
    _require_selected_cluster(request, cluster_id)
    try:
        limit = int(request.query_params.get("limit") or 100)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="limit không hợp lệ.") from exc
    with db.SessionLocal() as session:
        rows = online_learning_controls.list_audit(session, cluster_id=cluster_id, limit=limit)
        return [{
            "action": row.action, "actor": row.actor, "host": row.host,
            "metric": row.metric, "reason": row.reason,
            "target_id": row.target_id, "created_at": row.created_at,
        } for row in rows]


@router.post("/api/ai-learning/models/{model_id}/block")
async def block_model_candidate(
    request: Request,
    model_id: str, reason: str = Form(...), user: str = Depends(require_login),
):
    """Block one candidate and append the guarded-promotion audit event."""
    _require_admin(user)
    _clusters, cluster = cluster_selection(request)
    with db.SessionLocal() as session:
        _require_model_scope(session, model_id, cluster)
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
    request: Request,
    alert_id: str,
    verdict: str = Form(...),
    note: str = Form(""),
    impact_percent: float | None = Form(None),
    incident_id: str = Form(""),
    remediation_case_id: str = Form(""),
    user: str = Depends(require_login),
):
    """Append an operator verdict; this never changes lifecycle or policy."""
    _clusters, selected_cluster = cluster_selection(request)
    with db.SessionLocal() as session:
        alert = session.get(NodeResourceForecastAlert, alert_id)
        if alert is not None and alert.cluster_name != selected_cluster.name:
            raise HTTPException(
                status_code=404,
                detail="Không tìm thấy forecast alert trong cụm đang được chọn.",
            )
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
        "replay_options": _replay_options(cluster.id),
    })


@router.get("/ai-learning/models/{model_id}", response_class=HTMLResponse)
async def ai_learning_model_detail(
    request: Request, model_id: str, user: str = Depends(require_login),
):
    """Render bounded promotion evidence for one model outside the main table."""
    clusters, cluster = cluster_selection(request)
    report = model_promotion_status(cluster.id, cluster.name)
    model = next((item for item in report["models"] if item["id"] == model_id), None)
    if model is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy model trong cụm đang được chọn.")
    return templates.TemplateResponse(request, "ai_learning_model_detail.html", {
        "user": user, "clusters": clusters, "cluster": cluster,
        "model": model, "policy": report["policy"],
    })
