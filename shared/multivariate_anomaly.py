"""Bounded, side-effect-free multivariate anomaly candidates.

The functions in this module deliberately stop at candidate evidence.  They do
not create incidents, notifications, executor tasks, or remediation requests.
River HST is supported by the normal dependency set; RRCF is an optional
benchmark dependency and is never imported by the runtime watcher path.
"""

from __future__ import annotations

import bisect
import hashlib
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


FEATURE_NAMES: tuple[str, ...] = (
    "cpu",
    "ram",
    "read_iops",
    "write_iops",
    "read_latency",
    "write_latency",
    "osd_apply_latency",
    "pg_degraded_ratio",
)


@dataclass(frozen=True)
class MetricObservation:
    timestamp: datetime
    value: float
    quality_status: str = "OK"


@dataclass(frozen=True)
class MultivariateSample:
    timestamp: datetime
    values: Mapping[str, float]
    quality_status: str
    quality_by_metric: Mapping[str, str]
    missing_features: tuple[str, ...]
    coverage_ratio: float
    peer_class: str | None = None


@dataclass(frozen=True)
class FeatureBaseline:
    peer_class: str
    centers: Mapping[str, float]
    scales: Mapping[str, float]
    sample_count: int


@dataclass(frozen=True)
class NormalizedSample:
    timestamp: datetime
    values: Mapping[str, float]
    quality_status: str
    peer_class: str | None


@dataclass(frozen=True)
class DetectorScore:
    timestamp: datetime
    detector: str
    raw_score: float | None
    score: float | None
    quality_status: str
    top_features: tuple[str, ...]
    peer_class: str | None


@dataclass(frozen=True)
class AnomalyCandidate:
    timestamp: datetime
    detector: str
    score: float
    threshold: float
    quality_status: str
    top_features: tuple[str, ...]
    peer_class: str | None
    candidate_type: str = "ANOMALY_CANDIDATE"
    notification_allowed: bool = False
    remediation_requested: bool = False


@dataclass(frozen=True)
class IncidentWindow:
    incident_id: str
    detector: str
    start: datetime
    end: datetime
    peak_score: float
    sample_count: int
    top_features: tuple[str, ...]
    candidate_type: str = "ANOMALY_CANDIDATE"
    remediation_requested: bool = False


class OptionalDetectorUnavailable(RuntimeError):
    """Raised when an optional benchmark detector is not installed."""


def _utc(timestamp: datetime) -> datetime:
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _coalesced_timestamps(
    observations: Mapping[str, Sequence[MetricObservation]], tolerance: timedelta,
) -> list[datetime]:
    timestamps = sorted({_utc(item.timestamp) for rows in observations.values() for item in rows})
    result: list[datetime] = []
    for timestamp in timestamps:
        if not result or timestamp - result[-1] > tolerance:
            result.append(timestamp)
    return result


def _nearest(
    rows: Sequence[MetricObservation], timestamp: datetime, tolerance: timedelta,
) -> MetricObservation | None:
    if not rows:
        return None
    timestamps = [_utc(item.timestamp) for item in rows]
    index = bisect.bisect_left(timestamps, timestamp)
    choices = []
    if index < len(rows):
        choices.append(rows[index])
    if index:
        choices.append(rows[index - 1])
    if not choices:
        return None
    candidate = min(choices, key=lambda item: abs(_utc(item.timestamp) - timestamp))
    return candidate if abs(_utc(candidate.timestamp) - timestamp) <= tolerance else None


def build_feature_vectors(
    observations: Mapping[str, Iterable[MetricObservation]],
    *,
    required_features: Sequence[str] = FEATURE_NAMES,
    tolerance_seconds: int = 60,
    min_coverage: float = 1.0,
    peer_class: str | None = None,
) -> list[MultivariateSample]:
    """Align metric streams without zero-filling missing or poor-quality data."""

    required = tuple(dict.fromkeys(required_features))
    if not required:
        raise ValueError("required_features must not be empty")
    normalized: dict[str, list[MetricObservation]] = {}
    for name in required:
        normalized[name] = sorted(
            (
                MetricObservation(_utc(item.timestamp), float(item.value), item.quality_status)
                for item in observations.get(name, ())
                if _finite(item.value)
            ),
            key=lambda item: item.timestamp,
        )
    tolerance = timedelta(seconds=max(0, int(tolerance_seconds)))
    samples: list[MultivariateSample] = []
    for timestamp in _coalesced_timestamps(normalized, tolerance):
        values: dict[str, float] = {}
        quality: dict[str, str] = {}
        missing: list[str] = []
        for name in required:
            row = _nearest(normalized[name], timestamp, tolerance)
            if row is None:
                missing.append(name)
                quality[name] = "MISSING"
                continue
            quality[name] = row.quality_status
            if row.quality_status == "OK" and _finite(row.value):
                values[name] = float(row.value)
            else:
                missing.append(name)
        coverage = len(values) / len(required)
        if coverage == 1.0:
            status = "OK"
        elif coverage >= max(0.0, min(1.0, float(min_coverage))):
            status = "PARTIAL"
        else:
            status = "INSUFFICIENT_COMPONENTS"
        samples.append(
            MultivariateSample(
                timestamp=timestamp,
                values=values,
                quality_status=status,
                quality_by_metric=quality,
                missing_features=tuple(missing),
                coverage_ratio=coverage,
                peer_class=peer_class,
            )
        )
    return samples


def fit_peer_baselines(
    samples: Iterable[MultivariateSample], *, min_samples: int = 8,
) -> dict[str, FeatureBaseline]:
    """Fit robust per-device-class baselines from quality-approved samples."""

    grouped: dict[str, list[MultivariateSample]] = {}
    for sample in samples:
        if sample.quality_status != "OK":
            continue
        grouped.setdefault(sample.peer_class or "global", []).append(sample)
    baselines: dict[str, FeatureBaseline] = {}
    for peer, rows in grouped.items():
        if len(rows) < max(1, int(min_samples)):
            continue
        centers: dict[str, float] = {}
        scales: dict[str, float] = {}
        for feature in FEATURE_NAMES:
            values = [row.values[feature] for row in rows if _finite(row.values.get(feature))]
            if len(values) < max(3, int(min_samples // 2)):
                continue
            center = statistics.median(values)
            mad = statistics.median(abs(value - center) for value in values)
            centers[feature] = center
            scales[feature] = max(1e-6, 1.4826 * mad)
        if centers:
            baselines[peer] = FeatureBaseline(peer, centers, scales, len(rows))
    return baselines


def standardize_samples(
    samples: Iterable[MultivariateSample], baselines: Mapping[str, FeatureBaseline],
) -> list[NormalizedSample]:
    """Return robust z-score vectors; incomplete samples remain visibly blocked."""

    result: list[NormalizedSample] = []
    for sample in samples:
        baseline = baselines.get(sample.peer_class or "global") or baselines.get("global")
        if sample.quality_status != "OK" or baseline is None:
            result.append(NormalizedSample(sample.timestamp, {}, "BLOCKED", sample.peer_class))
            continue
        values = {
            feature: (value - baseline.centers[feature]) / baseline.scales[feature]
            for feature, value in sample.values.items()
            if feature in baseline.centers and _finite(value)
        }
        status = "OK" if len(values) == len(sample.values) == len(baseline.centers) else "BLOCKED"
        result.append(NormalizedSample(sample.timestamp, values if status == "OK" else {}, status, sample.peer_class))
    return result


def top_contributing_features(sample: NormalizedSample, *, limit: int = 3) -> tuple[str, ...]:
    return tuple(
        feature
        for feature, _ in sorted(
            ((name, abs(float(value))) for name, value in sample.values.items()),
            key=lambda item: (-item[1], item[0]),
        )[: max(1, int(limit))]
    )


def run_river_hst(
    samples: Sequence[NormalizedSample], *, n_trees: int = 10, height: int = 8,
    window_size: int = 250, seed: int = 42, warmup: int = 24,
) -> list[DetectorScore]:
    """Run bounded River Half-Space Trees over quality-approved vectors."""

    try:
        from river import anomaly
    except ImportError as exc:  # pragma: no cover - dependency is project runtime
        raise RuntimeError("River is required for HST benchmark") from exc
    model = anomaly.HalfSpaceTrees(
        n_trees=max(1, int(n_trees)), height=max(1, int(height)),
        window_size=max(8, int(window_size)), seed=int(seed),
    )
    result: list[DetectorScore] = []
    usable = 0
    for sample in samples:
        if sample.quality_status != "OK":
            result.append(DetectorScore(sample.timestamp, "river_hst", None, None, sample.quality_status, (), sample.peer_class))
            continue
        raw = float(model.score_one(dict(sample.values)))
        model.learn_one(dict(sample.values))
        usable += 1
        result.append(
            DetectorScore(
                sample.timestamp,
                "river_hst",
                raw if usable > max(0, int(warmup)) else None,
                None,
                "OK" if usable > max(0, int(warmup)) else "WARMUP",
                top_contributing_features(sample),
                sample.peer_class,
            )
        )
    return result


def run_rrcf(
    samples: Sequence[NormalizedSample], *, n_trees: int = 25, tree_size: int = 256,
    seed: int = 42, warmup: int = 24,
) -> list[DetectorScore]:
    """Run optional Robust Random Cut Forest benchmark with bounded eviction."""

    try:
        import rrcf  # type: ignore[import-not-found]
    except ImportError as exc:
        raise OptionalDetectorUnavailable("RRCF benchmark is optional; install rrcf separately") from exc
    trees = [rrcf.RCTree(random_state=int(seed) + index) for index in range(max(1, int(n_trees)))]
    result: list[DetectorScore] = []
    usable = 0
    limit = max(8, int(tree_size))
    for index, sample in enumerate(samples):
        if sample.quality_status != "OK":
            result.append(DetectorScore(sample.timestamp, "rrcf", None, None, sample.quality_status, (), sample.peer_class))
            continue
        point = list(sample.values.values())
        scores: list[float] = []
        for tree in trees:
            tree.insert_point(point, index=index)
            scores.append(float(tree.codisp(index)))
            old_index = index - limit
            if old_index >= 0:
                tree.forget_point(old_index)
        usable += 1
        raw = sum(scores) / len(scores)
        result.append(
            DetectorScore(
                sample.timestamp,
                "rrcf",
                raw if usable > max(0, int(warmup)) else None,
                None,
                "OK" if usable > max(0, int(warmup)) else "WARMUP",
                top_contributing_features(sample),
                sample.peer_class,
            )
        )
    return result


def normalize_scores(raw_scores: Iterable[float | None], reference_scores: Iterable[float]) -> list[float | None]:
    """Map detector-specific scores to a common empirical percentile [0, 1]."""

    reference = sorted(float(item) for item in reference_scores if _finite(item))
    if not reference:
        return [None for _ in raw_scores]
    denominator = max(1, len(reference) - 1)
    return [
        None
        if raw is None or not _finite(raw)
        else round(min(1.0, max(0.0, bisect.bisect_left(reference, float(raw)) / denominator)), 6)
        for raw in raw_scores
    ]


def calibrate_threshold(scores: Iterable[float | None], *, quantile: float = 0.995, minimum: float = 0.8) -> float:
    """Calibrate a conservative percentile threshold from baseline scores."""

    values = sorted(float(item) for item in scores if item is not None and _finite(item))
    if len(values) < 8:
        return float(minimum)
    position = min(len(values) - 1, max(0, math.ceil(len(values) * min(1.0, max(0.0, quantile))) - 1))
    return max(float(minimum), min(1.0, values[position]))


def candidate_scores(
    detector_scores: Sequence[DetectorScore], *, threshold: float,
) -> list[AnomalyCandidate]:
    """Convert scores to advisory candidates with remediation hard-disabled."""

    candidates: list[AnomalyCandidate] = []
    for item in detector_scores:
        if item.score is None or item.quality_status != "OK" or item.score < threshold:
            continue
        candidates.append(
            AnomalyCandidate(
                timestamp=item.timestamp,
                detector=item.detector,
                score=float(item.score),
                threshold=float(threshold),
                quality_status=item.quality_status,
                top_features=item.top_features,
                peer_class=item.peer_class,
            )
        )
    return candidates


def cluster_incident_windows(
    candidates: Iterable[AnomalyCandidate], *, max_gap_seconds: int = 300,
) -> list[IncidentWindow]:
    """Collapse one sustained anomaly into one candidate incident window."""

    ordered = sorted(candidates, key=lambda item: (item.detector, item.peer_class or "", item.timestamp))
    groups: list[list[AnomalyCandidate]] = []
    for candidate in ordered:
        if (
            groups
            and candidate.detector == groups[-1][-1].detector
            and candidate.peer_class == groups[-1][-1].peer_class
            and candidate.timestamp - groups[-1][-1].timestamp <= timedelta(seconds=max(0, int(max_gap_seconds)))
        ):
            groups[-1].append(candidate)
        else:
            groups.append([candidate])
    result: list[IncidentWindow] = []
    for group in groups:
        first = group[0]
        signature = f"{first.detector}|{first.peer_class or 'global'}|{group[0].timestamp.isoformat()}"
        incident_id = "anomaly-candidate:" + hashlib.sha256(signature.encode()).hexdigest()[:16]
        features = tuple(dict.fromkeys(feature for item in group for feature in item.top_features))
        result.append(
            IncidentWindow(
                incident_id=incident_id,
                detector=first.detector,
                start=group[0].timestamp,
                end=group[-1].timestamp,
                peak_score=max(item.score for item in group),
                sample_count=len(group),
                top_features=features,
            )
        )
    return result


def candidate_evidence(windows: Iterable[IncidentWindow]) -> list[dict[str, object]]:
    """Serialize advisory evidence; fields make forbidden side effects explicit."""

    return [
        {
            "type": window.candidate_type,
            "incident_id": window.incident_id,
            "detector": window.detector,
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "peak_score": window.peak_score,
            "sample_count": window.sample_count,
            "top_features": list(window.top_features),
            "notification_allowed": False,
            "remediation_requested": False,
        }
        for window in windows
    ]
