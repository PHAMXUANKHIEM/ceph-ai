"""Read-only offline evaluator for scoped online-learning evidence.

The module deliberately uses plain Python data structures. It can be run in a
staging/evaluation image with Evidently added later, without putting an
evaluation dependency or database write path into the Watcher runtime.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


DATASET_SCHEMA = "ceph-ai-online-eval-v1"
SCOPE_FIELDS = ("cluster_id", "host", "metric")


def _utc(value: object) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("observed_at must be an ISO timestamp")
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class EvaluationRecord:
    cluster_id: str
    host: str
    metric: str
    observed_at: datetime
    sample_id: str
    predicted: float | None
    actual: float | None
    alert: bool
    quality_status: str
    model_role: str = "unknown"

    @property
    def scope_key(self) -> str:
        return f"{self.cluster_id}|{self.host}|{self.metric}"

    def as_dict(self) -> dict[str, object]:
        return {
            "cluster_id": self.cluster_id,
            "host": self.host,
            "metric": self.metric,
            "observed_at": self.observed_at.isoformat(),
            "sample_id": self.sample_id,
            "predicted": self.predicted,
            "actual": self.actual,
            "alert": self.alert,
            "quality_status": self.quality_status,
            "model_role": self.model_role,
        }


@dataclass(frozen=True)
class DataQualityReport:
    row_count: int
    valid_scope_rows: int
    missing_fields: dict[str, int]
    duplicate_count: int
    scope_leakage_count: int
    out_of_range_count: int
    gap_count: int
    stale_row_count: int
    max_gap_hours: float
    freshness_hours: float | None
    status: str

    @property
    def promotion_safe(self) -> bool:
        return self.status == "PASS"

    def as_dict(self) -> dict[str, object]:
        return {
            **self.__dict__,
            "promotion_safe": self.promotion_safe,
        }


@dataclass(frozen=True)
class ScopeMetrics:
    scope_key: str
    evaluated: int
    mae: float | None
    rmse: float | None
    smape: float | None
    bias: float | None
    false_positive_rate: float | None
    alert_volume: int
    status: str

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class EvaluationReport:
    dataset_schema: str
    generated_at: datetime
    quality: DataQualityReport
    scopes: tuple[ScopeMetrics, ...]
    comparisons: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_schema": self.dataset_schema,
            "generated_at": self.generated_at.isoformat(),
            "quality": self.quality.as_dict(),
            "scopes": [item.as_dict() for item in self.scopes],
            "comparisons": list(self.comparisons),
        }


def _raw_value(row: Mapping[str, object], *names: str) -> object:
    for name in names:
        if name in row:
            return row[name]
    return None


def evaluate_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    now: datetime | None = None,
    max_age_hours: float = 24.0,
    max_gap_hours: float = 6.0,
    minimum_samples: int = 10,
) -> EvaluationReport:
    """Build fixed-schema evidence and evaluate every scope independently."""

    reference_now = _utc(now or datetime.now(timezone.utc))
    raw_rows = list(rows)
    records: list[EvaluationRecord] = []
    missing = {field: 0 for field in (*SCOPE_FIELDS, "observed_at", "sample_id")}
    out_of_range = 0
    for row in raw_rows:
        values = {
            "cluster_id": str(_raw_value(row, "cluster_id", "cluster_key") or "").strip(),
            "host": str(_raw_value(row, "host", "entity_id") or "").strip(),
            "metric": str(_raw_value(row, "metric") or "").strip().lower(),
            "sample_id": str(_raw_value(row, "sample_id", "id") or "").strip(),
        }
        for field, value in values.items():
            if not value:
                missing[field] += 1
        try:
            observed_at = _utc(_raw_value(row, "observed_at", "target_at"))
        except (TypeError, ValueError):
            missing["observed_at"] += 1
            continue
        if not all(values[field] for field in (*SCOPE_FIELDS, "sample_id")):
            continue
        predicted = _finite(_raw_value(row, "predicted", "predicted_percent"))
        actual = _finite(_raw_value(row, "actual", "actual_percent", "value"))
        for value in (predicted, actual):
            if value is not None and not 0.0 <= value <= 100.0:
                out_of_range += 1
        records.append(EvaluationRecord(
            cluster_id=values["cluster_id"], host=values["host"], metric=values["metric"],
            observed_at=observed_at, sample_id=values["sample_id"], predicted=predicted,
            actual=actual, alert=bool(_raw_value(row, "alert", "is_alert")),
            quality_status=str(_raw_value(row, "quality_status") or "UNKNOWN"),
            model_role=str(_raw_value(row, "model_role", "model_state", "role") or "unknown").lower(),
        ))

    seen: set[tuple[str, str, str, str, str, str]] = set()
    duplicate_count = 0
    sample_scopes: dict[str, set[str]] = {}
    grouped: dict[str, list[EvaluationRecord]] = {}
    scope_grouped: dict[str, list[EvaluationRecord]] = {}
    for record in records:
        identity = (
            *record.scope_key.split("|"), record.observed_at.isoformat(),
            record.sample_id, record.model_role,
        )
        if identity in seen:
            duplicate_count += 1
        seen.add(identity)
        sample_scopes.setdefault(record.sample_id, set()).add(record.scope_key)
        role_key = record.scope_key if record.model_role == "unknown" else f"{record.scope_key}|{record.model_role}"
        grouped.setdefault(role_key, []).append(record)
        scope_grouped.setdefault(record.scope_key, []).append(record)
    scope_leakage = sum(max(0, len(scopes) - 1) for scopes in sample_scopes.values())
    gap_count = 0
    for scope_records in scope_grouped.values():
        ordered = sorted(scope_records, key=lambda item: item.observed_at)
        gap_count += sum(
            (right.observed_at - left.observed_at) > timedelta(hours=max_gap_hours)
            for left, right in zip(ordered, ordered[1:])
        )
    stale = sum(
        reference_now - record.observed_at > timedelta(hours=max_age_hours)
        for record in records
    )
    latest = max((record.observed_at for record in records), default=None)
    freshness = (reference_now - latest).total_seconds() / 3600 if latest else None
    status = "PASS" if not (
        any(missing.values()) or duplicate_count or scope_leakage or out_of_range
        or gap_count or stale
    ) else "FAIL"
    quality = DataQualityReport(
        row_count=len(raw_rows), valid_scope_rows=len(records), missing_fields=missing,
        duplicate_count=duplicate_count, scope_leakage_count=scope_leakage,
        out_of_range_count=out_of_range, gap_count=gap_count, stale_row_count=stale,
        max_gap_hours=max_gap_hours, freshness_hours=freshness, status=status,
    )
    scopes = tuple(_scope_metrics(scope, grouped[scope], minimum_samples) for scope in sorted(grouped))
    comparisons: list[dict[str, object]] = []
    for scope in sorted(scope_grouped):
        active = grouped.get(f"{scope}|active")
        candidate = grouped.get(f"{scope}|candidate")
        if active is None or candidate is None:
            continue
        active_metrics = _scope_metrics(scope, active, minimum_samples)
        candidate_metrics = _scope_metrics(scope, candidate, minimum_samples)
        comparisons.append({
            "scope_key": scope,
            "status": "READY" if active_metrics.status == candidate_metrics.status == "PASS" else "INSUFFICIENT_DATA",
            "active": active_metrics.as_dict(),
            "candidate": candidate_metrics.as_dict(),
            "mae_delta": (
                candidate_metrics.mae - active_metrics.mae
                if candidate_metrics.mae is not None and active_metrics.mae is not None else None
            ),
            "alert_volume_delta": candidate_metrics.alert_volume - active_metrics.alert_volume,
        })
    return EvaluationReport(DATASET_SCHEMA, reference_now, quality, scopes, tuple(comparisons))


def _scope_metrics(scope: str, records: list[EvaluationRecord], minimum: int) -> ScopeMetrics:
    pairs = [(record.predicted, record.actual) for record in records
             if record.predicted is not None and record.actual is not None]
    if len(pairs) < max(1, minimum):
        return ScopeMetrics(
            scope, len(pairs), None, None, None, None, None,
            sum(record.alert for record in records), "INSUFFICIENT_DATA",
        )
    errors = [predicted - actual for predicted, actual in pairs]
    absolute = [abs(error) for error in errors]
    smape = [0.0 if abs(predicted) + abs(actual) == 0 else
             200.0 * abs(predicted - actual) / (abs(predicted) + abs(actual))
             for predicted, actual in pairs]
    negatives = sum(actual < 90.0 for _predicted, actual in pairs)
    false_positives = sum(predicted >= 90.0 and actual < 90.0 for predicted, actual in pairs)
    return ScopeMetrics(
        scope, len(pairs), statistics.fmean(absolute),
        math.sqrt(statistics.fmean(error * error for error in errors)),
        statistics.fmean(smape), statistics.fmean(errors),
        false_positives / negatives if negatives else None,
        sum(record.alert for record in records), "PASS",
    )


def write_artifact(report: EvaluationReport, path: str) -> None:
    """Write an immutable-style JSON report; this function has no DB access."""

    with open(path, "x", encoding="utf-8") as handle:
        json.dump(report.as_dict(), handle, indent=2, sort_keys=True)
