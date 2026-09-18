"""Read-only replay/backtest helpers for resource forecast candidates.

Replay is intentionally pure: it reads an in-memory time series, never writes
model state, opens alerts, sends notifications, or executes remediation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from config.settings import settings
from shared.forecast_consensus import aggregate_forecasts
from shared.models import (
    NodeResourceForecastRun, NodeResourceModelState,
    VolumeForecastRun, VolumeModelState,
)
from watcher.node_resource_forecast import (
    ResourceForecast,
    _linear_forecast,
    _rolling_quantile_forecast,
    _window_points,
)


@dataclass(frozen=True)
class ReplayMetrics:
    algorithm: str
    evaluated: int
    skipped: int
    mae: float | None
    rmse: float | None
    smape: float | None
    bias: float | None
    precision: float | None
    false_positive_rate: float | None
    true_positives: int
    false_positives: int


@dataclass(frozen=True)
class ShadowComparison:
    """Evidence-only comparison between the active and a shadow candidate."""

    active_algorithm: str
    candidate_algorithm: str
    active_evaluated: int
    candidate_evaluated: int
    active_mae: float | None
    candidate_mae: float | None
    mae_delta: float | None
    active_false_positive_rate: float | None
    candidate_false_positive_rate: float | None
    status: str
    reason: str
    active_rmse: float | None = None
    candidate_rmse: float | None = None
    active_smape: float | None = None
    candidate_smape: float | None = None
    active_bias: float | None = None
    candidate_bias: float | None = None
    execution_mode: str = "SHADOW_ONLY"
    latest_target_at: datetime | None = None
    scope_type: str | None = None
    scope_key: str | None = None
    active_window_hours: int | None = None
    candidate_window_hours: int | None = None


def _smape(predicted: float, actual: float) -> float:
    denominator = abs(predicted) + abs(actual)
    return 0.0 if denominator == 0 else 200.0 * abs(predicted - actual) / denominator


def _empty(algorithm: str, skipped: int = 0) -> ReplayMetrics:
    return ReplayMetrics(
        algorithm=algorithm, evaluated=0, skipped=skipped,
        mae=None, rmse=None, smape=None, bias=None,
        precision=None, false_positive_rate=None,
        true_positives=0, false_positives=0,
    )


def _score(
    algorithm: str, pairs: list[tuple[float, float]], skipped: int,
    threshold: float | None = 90.0,
) -> ReplayMetrics:
    if not pairs:
        return _empty(algorithm, skipped)
    errors = [predicted - actual for predicted, actual in pairs]
    absolute = [abs(error) for error in errors]
    positives = (
        [(predicted >= threshold, actual >= threshold) for predicted, actual in pairs]
        if threshold is not None else []
    )
    true_positives = sum(predicted and actual for predicted, actual in positives)
    false_positives = sum(predicted and not actual for predicted, actual in positives)
    actual_negatives = sum(not actual for _predicted, actual in positives)
    predicted_positives = true_positives + false_positives
    return ReplayMetrics(
        algorithm=algorithm,
        evaluated=len(pairs),
        skipped=skipped,
        mae=sum(absolute) / len(absolute),
        rmse=math.sqrt(sum(error * error for error in errors) / len(errors)),
        smape=sum(_smape(predicted, actual) for predicted, actual in pairs) / len(pairs),
        bias=sum(errors) / len(errors),
        precision=(true_positives / predicted_positives if predicted_positives else None),
        false_positive_rate=(false_positives / actual_negatives if actual_negatives else None),
        true_positives=true_positives,
        false_positives=false_positives,
    )


def compare_shadow_metrics(
    metrics: dict[str, ReplayMetrics], *, active_algorithm: str = "linear",
    candidate_algorithm: str = "rolling_quantile", minimum_evaluated: int | None = None,
) -> ShadowComparison:
    """Compare a candidate without changing the active model or side effects.

    A candidate is only *promising* when it has enough paired outcomes, lower
    MAE, and no worse false-positive rate when both rates are measurable.
    This function deliberately returns evidence, never a promotion command.
    """
    active = metrics.get(active_algorithm)
    candidate = metrics.get(candidate_algorithm)
    minimum = max(1, minimum_evaluated or settings.node_resource_learning_min_outcomes)
    if active is None or candidate is None:
        return ShadowComparison(
            active_algorithm=active_algorithm, candidate_algorithm=candidate_algorithm,
            active_evaluated=active.evaluated if active else 0,
            candidate_evaluated=candidate.evaluated if candidate else 0,
            active_mae=active.mae if active else None,
            candidate_mae=candidate.mae if candidate else None,
            mae_delta=None,
            active_rmse=active.rmse if active else None,
            candidate_rmse=candidate.rmse if candidate else None,
            active_smape=active.smape if active else None,
            candidate_smape=candidate.smape if candidate else None,
            active_bias=active.bias if active else None,
            candidate_bias=candidate.bias if candidate else None,
            active_false_positive_rate=active.false_positive_rate if active else None,
            candidate_false_positive_rate=candidate.false_positive_rate if candidate else None,
            status="INSUFFICIENT_DATA", reason="active hoặc candidate chưa có metrics",
        )
    if active.evaluated < minimum or candidate.evaluated < minimum:
        return ShadowComparison(
            active_algorithm=active_algorithm, candidate_algorithm=candidate_algorithm,
            active_evaluated=active.evaluated, candidate_evaluated=candidate.evaluated,
            active_mae=active.mae, candidate_mae=candidate.mae,
            mae_delta=None if active.mae is None or candidate.mae is None else candidate.mae - active.mae,
            active_rmse=active.rmse, candidate_rmse=candidate.rmse,
            active_smape=active.smape, candidate_smape=candidate.smape,
            active_bias=active.bias, candidate_bias=candidate.bias,
            active_false_positive_rate=active.false_positive_rate,
            candidate_false_positive_rate=candidate.false_positive_rate,
            status="INSUFFICIENT_DATA",
            reason=f"cần tối thiểu {minimum} outcome cho cả active và candidate",
        )
    if active.mae is None or candidate.mae is None:
        status, reason = "INSUFFICIENT_DATA", "MAE chưa đủ để so sánh"
    else:
        fpr_not_worse = (
            active.false_positive_rate is None
            or candidate.false_positive_rate is None
            or candidate.false_positive_rate <= active.false_positive_rate
        )
        promising = candidate.mae < active.mae and fpr_not_worse
        status = "PROMISING" if promising else "HOLD"
        reason = (
            "candidate có MAE thấp hơn và false-positive rate không tăng"
            if promising else "candidate chưa chứng minh tốt hơn active"
        )
    return ShadowComparison(
        active_algorithm=active_algorithm, candidate_algorithm=candidate_algorithm,
        active_evaluated=active.evaluated, candidate_evaluated=candidate.evaluated,
        active_mae=active.mae, candidate_mae=candidate.mae,
        mae_delta=None if active.mae is None or candidate.mae is None else candidate.mae - active.mae,
        active_rmse=active.rmse, candidate_rmse=candidate.rmse,
        active_smape=active.smape, candidate_smape=candidate.smape,
        active_bias=active.bias, candidate_bias=candidate.bias,
        active_false_positive_rate=active.false_positive_rate,
        candidate_false_positive_rate=candidate.false_positive_rate,
        status=status, reason=reason,
    )


def replay_resource_forecasts(
    points: list[tuple[datetime, float]],
    metric: str,
    *,
    horizon_hours: int = 1,
    window_hours: list[int] | None = None,
) -> dict[str, ReplayMetrics]:
    """Replay linear, rolling-quantile and their consensus without side effects."""

    ordered = sorted(points, key=lambda item: item[0])
    windows = window_hours or [24, 72, 168, 720]
    minimum = max(3, settings.node_resource_forecast_min_samples)
    linear_pairs: list[tuple[float, float]] = []
    rolling_pairs: list[tuple[float, float]] = []
    consensus_pairs: list[tuple[float, float]] = []
    skipped_linear = skipped_rolling = skipped_consensus = 0

    for index in range(minimum - 1, len(ordered) - horizon_hours):
        training_end = ordered[index][0]
        target_index = index + horizon_hours
        target = ordered[target_index][1]
        history = ordered[:index + 1]
        linear_candidates: list[ResourceForecast] = []
        rolling_candidates: list[ResourceForecast] = []
        for window in windows:
            window_points = _window_points(history, window)
            linear = _linear_forecast(
                window_points, metric, horizon_hours=horizon_hours,
                training_window_hours=window,
            )
            rolling = _rolling_quantile_forecast(
                window_points, metric, horizon_hours=horizon_hours,
                training_window_hours=window,
            )
            if linear is not None:
                linear_candidates.append(linear)
            if rolling is not None:
                rolling_candidates.append(rolling)
        if linear_candidates:
            linear = linear_candidates[-1]
            linear_pairs.append((linear.predicted_percent, target))
        else:
            skipped_linear += 1
        if rolling_candidates:
            rolling = rolling_candidates[-1]
            rolling_pairs.append((rolling.predicted_percent, target))
        else:
            skipped_rolling += 1

        all_candidates = linear_candidates + rolling_candidates
        consensus = aggregate_forecasts(
            [candidate.predicted_percent for candidate in all_candidates],
            minimum_candidates=max(1, settings.node_resource_forecast_min_consensus_candidates),
            minimum_ratio=settings.node_resource_forecast_min_consensus_ratio,
            absolute_tolerance=settings.node_resource_forecast_consensus_tolerance_percent,
            relative_tolerance=settings.node_resource_forecast_consensus_relative_tolerance,
        )
        if consensus.usable:
            consensus_pairs.append((consensus.value, target))
        else:
            skipped_consensus += 1

    return {
        "linear": _score("linear", linear_pairs, skipped_linear),
        "rolling_quantile": _score("rolling_quantile", rolling_pairs, skipped_rolling),
        "consensus": _score("consensus", consensus_pairs, skipped_consensus),
    }


def evaluate_shadow(
    points: list[tuple[datetime, float]], metric: str, *,
    horizon_hours: int = 1, window_hours: list[int] | None = None,
    active_algorithm: str = "linear", candidate_algorithm: str = "rolling_quantile",
    minimum_evaluated: int | None = None,
) -> dict[str, object]:
    """Run a bounded, read-only shadow evaluation and return evidence only."""
    metrics = replay_resource_forecasts(
        points, metric, horizon_hours=horizon_hours, window_hours=window_hours,
    )
    comparison = compare_shadow_metrics(
        metrics, active_algorithm=active_algorithm,
        candidate_algorithm=candidate_algorithm, minimum_evaluated=minimum_evaluated,
    )
    return {"metrics": metrics, "comparison": comparison}


def compare_persisted_forecast_runs(session, *, minimum_evaluated: int | None = None) -> list[ShadowComparison]:
    """Compare persisted active/candidate outcomes without mutating state.

    The active identity comes from the existing ``selected`` model state. A
    candidate is paired by the same target timestamp, so the comparison uses
    the same observed future outcome rather than unrelated samples. The
    function intentionally performs no commit and has no notification or
    remediation dependency.
    """
    minimum = max(1, minimum_evaluated or settings.node_resource_learning_min_outcomes)
    results: list[ShadowComparison] = []

    node_states = session.query(NodeResourceModelState).filter_by(selected=True).all()
    for state in node_states:
        active_rows = session.query(NodeResourceForecastRun).filter_by(
            cluster_name=state.cluster_name, host=state.host, metric=state.metric,
            algorithm=state.algorithm, window_hours=state.window_hours, status="EVALUATED",
        ).filter(NodeResourceForecastRun.actual_percent.isnot(None)).all()
        active_by_target = {row.target_at: row for row in active_rows}
        candidates = session.query(NodeResourceForecastRun).filter(
            NodeResourceForecastRun.cluster_name == state.cluster_name,
            NodeResourceForecastRun.host == state.host,
            NodeResourceForecastRun.metric == state.metric,
            NodeResourceForecastRun.status == "EVALUATED",
            NodeResourceForecastRun.actual_percent.isnot(None),
        ).all()
        identities = sorted({(row.algorithm, row.window_hours) for row in candidates})
        for algorithm, window in identities:
            if algorithm == state.algorithm and window == state.window_hours:
                continue
            candidate_rows = [row for row in candidates if row.algorithm == algorithm and row.window_hours == window]
            paired_rows = [row for row in candidate_rows if row.target_at in active_by_target]
            pairs = [
                (row.predicted_percent, row.actual_percent)
                for row in paired_rows
            ]
            active_pairs = [
                (active_by_target[row.target_at].predicted_percent, row.actual_percent)
                for row in paired_rows
            ]
            metrics = {
                "active": _score("active", active_pairs, 0),
                "candidate": _score("candidate", pairs, 0),
            }
            result = compare_shadow_metrics(
                metrics, active_algorithm="active", candidate_algorithm="candidate",
                minimum_evaluated=minimum,
            )
            results.append(ShadowComparison(
                active_algorithm=f"{state.algorithm}:{state.window_hours}h",
                candidate_algorithm=f"{algorithm}:{window}h",
                active_evaluated=result.active_evaluated,
                candidate_evaluated=result.candidate_evaluated,
                active_mae=result.active_mae, candidate_mae=result.candidate_mae,
                mae_delta=result.mae_delta,
                active_rmse=result.active_rmse, candidate_rmse=result.candidate_rmse,
                active_smape=result.active_smape, candidate_smape=result.candidate_smape,
                active_bias=result.active_bias, candidate_bias=result.candidate_bias,
                active_false_positive_rate=result.active_false_positive_rate,
                candidate_false_positive_rate=result.candidate_false_positive_rate,
                status=result.status, reason=result.reason,
                latest_target_at=max((row.target_at for row in paired_rows), default=None),
                scope_type="NODE_RESOURCE",
                scope_key=f"{state.cluster_name}|{state.host}|{state.metric.lower()}",
                active_window_hours=state.window_hours,
                candidate_window_hours=window,
            ))

    volume_states = session.query(VolumeModelState).filter_by(selected=True).all()
    for state in volume_states:
        active_rows = session.query(VolumeForecastRun).filter_by(
            cluster_id=state.cluster_id, pool=state.pool, image=state.image,
            metric=state.metric, algorithm=state.algorithm,
            window_hours=state.window_hours, status="EVALUATED",
        ).filter(VolumeForecastRun.actual_value.isnot(None)).all()
        active_by_target = {row.target_at: row for row in active_rows}
        candidates = session.query(VolumeForecastRun).filter(
            VolumeForecastRun.cluster_id == state.cluster_id,
            VolumeForecastRun.pool == state.pool,
            VolumeForecastRun.image == state.image,
            VolumeForecastRun.metric == state.metric,
            VolumeForecastRun.status == "EVALUATED",
            VolumeForecastRun.actual_value.isnot(None),
        ).all()
        identities = sorted({(row.algorithm, row.window_hours) for row in candidates})
        for algorithm, window in identities:
            if algorithm == state.algorithm and window == state.window_hours:
                continue
            candidate_rows = [row for row in candidates if row.algorithm == algorithm and row.window_hours == window]
            paired_rows = [row for row in candidate_rows if row.target_at in active_by_target]
            pairs = [
                (row.predicted_value, row.actual_value)
                for row in paired_rows
            ]
            active_pairs = [
                (active_by_target[row.target_at].predicted_value, row.actual_value)
                for row in paired_rows
            ]
            metrics = {
                "active": _score("active", active_pairs, 0, threshold=None),
                "candidate": _score("candidate", pairs, 0, threshold=None),
            }
            result = compare_shadow_metrics(
                metrics, active_algorithm="active", candidate_algorithm="candidate",
                minimum_evaluated=minimum,
            )
            results.append(ShadowComparison(
                active_algorithm=f"{state.algorithm}:{state.window_hours}h",
                candidate_algorithm=f"{algorithm}:{window}h",
                active_evaluated=result.active_evaluated,
                candidate_evaluated=result.candidate_evaluated,
                active_mae=result.active_mae, candidate_mae=result.candidate_mae,
                mae_delta=result.mae_delta,
                active_rmse=result.active_rmse, candidate_rmse=result.candidate_rmse,
                active_smape=result.active_smape, candidate_smape=result.candidate_smape,
                active_bias=result.active_bias, candidate_bias=result.candidate_bias,
                active_false_positive_rate=result.active_false_positive_rate,
                candidate_false_positive_rate=result.candidate_false_positive_rate,
                status=result.status, reason=result.reason,
                latest_target_at=max((row.target_at for row in paired_rows), default=None),
                scope_type="VOLUME",
                scope_key=f"{state.cluster_id}|{state.pool}|{state.image}|{state.metric.lower()}",
                active_window_hours=state.window_hours,
                candidate_window_hours=window,
            ))
    return results
