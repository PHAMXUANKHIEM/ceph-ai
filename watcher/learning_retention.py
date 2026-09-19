"""Bounded retention for predictive-learning data.

The collector tables are append-only by design. This module removes only
time-series rows and terminal forecast/audit history; model state, active
alerts, and open operator work are never removed here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from threading import Lock

from sqlalchemy import select

from config.settings import settings
from shared import db
from shared.models import (
    ForecastModelEvaluation,
    ForecastModelPromotionAudit,
    HostMetricSample,
    LogLearningAudit,
    NodeResourceForecastAlert,
    NodeResourceForecastFeedback,
    NodeResourceForecastRun,
    NodeResourceForecastTransition,
    VolumeEarlyForecast,
    VolumeForecastRun,
    VolumeMetric,
)

logger = logging.getLogger(__name__)
_prune_lock = Lock()
_last_prune_at: datetime | None = None


def _zero_result() -> dict[str, int]:
    return {
        "host_metric_samples": 0,
        "volume_metrics": 0,
        "node_forecast_runs": 0,
        "volume_forecast_runs": 0,
        "volume_early_forecasts": 0,
        "forecast_transitions": 0,
        "forecast_feedback": 0,
        "model_evaluations": 0,
        "promotion_audits": 0,
        "learning_audit": 0,
    }


def prune_old_rows(now: datetime | None = None) -> dict[str, int]:
    """Prune old learning data at most once per configured interval.

    The operation is one transaction. If it fails, the timestamp is rolled
    back so the next scheduled tick can retry instead of silently disabling
    retention.
    """

    global _last_prune_at
    now = now or datetime.utcnow()
    with _prune_lock:
        if (
            _last_prune_at is not None
            and (now - _last_prune_at).total_seconds()
            < settings.learning_retention_interval_seconds
        ):
            return _zero_result()
        _last_prune_at = now

    raw_cutoff = now - timedelta(days=settings.learning_raw_sample_retention_days)
    forecast_cutoff = now - timedelta(days=settings.learning_forecast_retention_days)
    audit_cutoff = now - timedelta(days=settings.learning_audit_retention_days)
    result = _zero_result()

    try:
        with db.SessionLocal() as session:
            result["host_metric_samples"] = session.query(HostMetricSample).filter(
                HostMetricSample.collected_at < raw_cutoff,
            ).delete(synchronize_session=False)
            result["volume_metrics"] = session.query(VolumeMetric).filter(
                VolumeMetric.polled_at < raw_cutoff,
            ).delete(synchronize_session=False)

            # Forecast rows are evidence, not live state. PENDING rows older
            # than the retention horizon are stale and are intentionally
            # removed as well; model selector state lives in *_ModelState.
            result["node_forecast_runs"] = session.query(NodeResourceForecastRun).filter(
                NodeResourceForecastRun.created_at < forecast_cutoff,
            ).delete(synchronize_session=False)
            result["volume_forecast_runs"] = session.query(VolumeForecastRun).filter(
                VolumeForecastRun.created_at < forecast_cutoff,
            ).delete(synchronize_session=False)
            result["volume_early_forecasts"] = session.query(VolumeEarlyForecast).filter(
                VolumeEarlyForecast.created_at < forecast_cutoff,
            ).delete(synchronize_session=False)

            active_alerts = select(NodeResourceForecastAlert.id).where(
                NodeResourceForecastAlert.status != "RESOLVED",
            )
            result["forecast_transitions"] = session.query(
                NodeResourceForecastTransition
            ).filter(
                NodeResourceForecastTransition.changed_at < audit_cutoff,
                ~NodeResourceForecastTransition.alert_id.in_(active_alerts),
            ).delete(synchronize_session=False)
            result["forecast_feedback"] = session.query(NodeResourceForecastFeedback).filter(
                NodeResourceForecastFeedback.created_at < audit_cutoff,
            ).delete(synchronize_session=False)
            result["model_evaluations"] = session.query(ForecastModelEvaluation).filter(
                ForecastModelEvaluation.evaluated_at < audit_cutoff,
            ).delete(synchronize_session=False)
            result["promotion_audits"] = session.query(ForecastModelPromotionAudit).filter(
                ForecastModelPromotionAudit.created_at < audit_cutoff,
            ).delete(synchronize_session=False)
            result["learning_audit"] = session.query(LogLearningAudit).filter(
                LogLearningAudit.created_at < audit_cutoff,
            ).delete(synchronize_session=False)
            session.commit()
    except Exception:
        with _prune_lock:
            _last_prune_at = None
        logger.exception("learning retention sweep failed")
        return _zero_result()
    return result


def reset_for_tests() -> None:
    global _last_prune_at
    with _prune_lock:
        _last_prune_at = None
