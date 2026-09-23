"""Bounded read-only 6/24-hour model evaluation summaries by exact cluster scope."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from shared import db
from shared.models import ForecastModelEvaluation, ForecastModelRegistry


MAX_MODELS = 100
MAX_EVALUATIONS = 5000


def _mean(rows, name: str) -> float | None:
    values = [float(value) for row in rows if (value := getattr(row, name)) is not None
              and math.isfinite(float(value))]
    return round(sum(values) / len(values), 4) if values else None


def quality_report(cluster_id: str, *, hours: int, now: datetime | None = None) -> dict:
    if hours not in (6, 24):
        raise ValueError("quality report supports 6 or 24 hours only")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    reference = reference.astimezone(timezone.utc)
    cutoff = (reference - timedelta(hours=hours)).replace(tzinfo=None)
    end = reference.replace(tzinfo=None)
    with db.SessionLocal() as session:
        models = session.query(ForecastModelRegistry).filter(
            ForecastModelRegistry.cluster_id == cluster_id,
            ForecastModelRegistry.scope_type.in_(("NODE_RESOURCE", "VOLUME")),
        ).order_by(ForecastModelRegistry.scope_type, ForecastModelRegistry.scope_key,
                   ForecastModelRegistry.id).limit(MAX_MODELS + 1).all()
        truncated_models = len(models) > MAX_MODELS
        models = models[:MAX_MODELS]
        ids = [row.id for row in models]
        evaluations = session.query(ForecastModelEvaluation).filter(
            ForecastModelEvaluation.candidate_model_id.in_(ids),
            ForecastModelEvaluation.evaluated_at >= cutoff,
            ForecastModelEvaluation.evaluated_at <= end,
        ).order_by(ForecastModelEvaluation.evaluated_at.desc(),
                   ForecastModelEvaluation.id.desc()).limit(MAX_EVALUATIONS + 1).all() if ids else []
        truncated_evaluations = len(evaluations) > MAX_EVALUATIONS
        evaluations = evaluations[:MAX_EVALUATIONS]
        grouped: dict[str, list] = {model_id: [] for model_id in ids}
        for row in evaluations:
            grouped[row.candidate_model_id].append(row)
        items = []
        for model in models:
            rows = grouped[model.id]
            items.append({
                "model_id": model.id, "scope_type": model.scope_type, "scope_key": model.scope_key,
                "cluster_id": model.cluster_id, "entity_type": model.entity_type,
                "entity_id": model.entity_id, "host": model.host, "metric": model.metric,
                "horizon_hours": model.horizon_hours, "feature_schema": model.feature_schema,
                "version": model.version, "status": model.status,
                "evaluation_count": len(rows), "latest_evaluated_at": (
                    rows[0].evaluated_at.isoformat() if rows else None),
                "active_mae": _mean(rows, "active_mae"),
                "candidate_mae": _mean(rows, "candidate_mae"),
                "active_smape": _mean(rows, "active_smape"),
                "candidate_smape": _mean(rows, "candidate_smape"),
                "holds": sum(row.status != "PROMISING" for row in rows),
                "quality": "NO_OUTCOMES" if not rows else "EVALUATED",
            })
    return {"cluster_id": cluster_id, "window_hours": hours, "generated_at": reference.isoformat(),
            "execution_mode": "READ_ONLY", "model_count": len(models),
            "evaluation_count": len(evaluations), "truncated_models": truncated_models,
            "truncated_evaluations": truncated_evaluations, "models": items}
