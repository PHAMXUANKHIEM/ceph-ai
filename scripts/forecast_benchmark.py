"""Bounded offline benchmark for Ceph resource predictive-alert candidates.

The input follows the NAB time-series shape: ``timestamp,value``.  An optional
third ``anomaly`` column may contain point labels.  Alternatively, labels can
be supplied as NAB-style ``window_start,window_end`` CSV rows.  The benchmark
is deliberately offline and side-effect free: it never opens an alert, writes
the database, sends a notification, or updates a production model.

River is the supported lightweight online detector.  PyOD is optional and is
only loaded when the benchmark extra is installed; production containers do
not need it.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median


@dataclass(frozen=True)
class Point:
    timestamp: datetime
    value: float
    anomaly: bool = False


@dataclass(frozen=True)
class BenchmarkResult:
    detector: str
    evaluated: int
    predicted_alerts: int
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None
    false_positive_rate: float | None
    event_recall: float | None
    mean_detection_delay_seconds: float | None
    cpu_time_ms: float


def _parse_timestamp(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_series(path: str | Path, labels_path: str | Path | None = None) -> list[Point]:
    """Load an anonymized NAB-like series and reject malformed samples."""
    rows: list[tuple[datetime, float, bool]] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                timestamp = _parse_timestamp(row["timestamp"])
                value = float(row["value"])
            except (KeyError, TypeError, ValueError):
                continue
            if not math.isfinite(value):
                continue
            raw_label = str(row.get("anomaly", "0")).strip().lower()
            rows.append((timestamp, value, raw_label in {"1", "true", "yes", "anomaly"}))

    windows: list[tuple[datetime, datetime]] = []
    if labels_path:
        with Path(labels_path).open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                try:
                    windows.append((_parse_timestamp(row["window_start"]), _parse_timestamp(row["window_end"])))
                except (KeyError, TypeError, ValueError):
                    continue
    deduped: dict[datetime, tuple[float, bool]] = {}
    for timestamp, value, anomaly in sorted(rows, key=lambda item: item[0]):
        in_window = any(start <= timestamp <= end for start, end in windows)
        deduped[timestamp] = (value, anomaly or in_window)
    return [Point(timestamp, value, anomaly) for timestamp, (value, anomaly) in deduped.items()]


def _events(points: list[Point]) -> list[list[Point]]:
    events: list[list[Point]] = []
    for point in points:
        if not point.anomaly:
            continue
        if events and (point.timestamp - events[-1][-1].timestamp).total_seconds() <= 300:
            events[-1].append(point)
        else:
            events.append([point])
    return events


def _score_result(
    name: str,
    points: list[Point],
    predicted: list[bool],
    cpu_ms: float,
    *,
    lead_window_seconds: float = 3600.0,
) -> BenchmarkResult:
    labels = [point.anomaly for point in points]
    tp = sum(alert and label for alert, label in zip(predicted, labels))
    fp = sum(alert and not label for alert, label in zip(predicted, labels))
    fn = sum((not alert) and label for alert, label in zip(predicted, labels))
    negatives = sum(not label for label in labels)
    predicted_count = sum(predicted)
    event_rows: list[float] = []
    detected_events = 0
    for event in _events(points):
        detections = [
            point.timestamp for point, alert in zip(points, predicted)
            if alert and event[0].timestamp - timedelta(seconds=lead_window_seconds)
            <= point.timestamp <= event[-1].timestamp
        ]
        if detections:
            detected_events += 1
            event_rows.append(max(0.0, (detections[0] - event[0].timestamp).total_seconds()))
    event_count = len(_events(points))
    return BenchmarkResult(
        detector=name,
        evaluated=len(points),
        predicted_alerts=predicted_count,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        precision=tp / predicted_count if predicted_count else None,
        recall=tp / (tp + fn) if tp + fn else None,
        false_positive_rate=fp / negatives if negatives else None,
        event_recall=detected_events / event_count if event_count else None,
        mean_detection_delay_seconds=sum(event_rows) / len(event_rows) if event_rows else None,
        cpu_time_ms=round(cpu_ms, 3),
    )


def _robust_baseline_scores(values: list[float], history_size: int) -> list[float | None]:
    scores: list[float | None] = []
    for index, value in enumerate(values):
        history = values[max(0, index - history_size):index]
        if len(history) < max(8, history_size // 2):
            scores.append(None)
            continue
        center = median(history)
        deviations = [abs(item - center) for item in history]
        mad = median(deviations)
        scale = max(1e-6, 1.4826 * mad)
        scores.append(abs(value - center) / scale)
    return scores


def _baseline(points: list[Point], *, threshold: float, history_size: int) -> list[bool]:
    started = time.process_time_ns()
    scores = _robust_baseline_scores([point.value for point in points], history_size)
    # Keep the CPU measurement meaningful even though the caller owns the
    # final result timing; this local variable makes the bounded work obvious.
    del started
    return [score is not None and score >= threshold for score in scores]


def _river(points: list[Point], *, threshold: float, history_size: int) -> list[bool]:
    try:
        from river import anomaly
    except ImportError as exc:  # pragma: no cover - exercised in minimal envs
        raise RuntimeError("River is required for the River benchmark") from exc
    model = anomaly.HalfSpaceTrees(seed=42)
    predictions: list[bool] = []
    previous = None
    for index, point in enumerate(points):
        features = {"value": point.value, "delta": 0.0 if previous is None else point.value - previous}
        score = float(model.score_one(features))
        predictions.append(index >= history_size and score >= threshold)
        model.learn_one(features)
        previous = point.value
    return predictions


def _pyod(points: list[Point], *, threshold: float, history_size: int) -> list[bool]:
    try:
        from pyod.models.iforest import IForest
    except ImportError as exc:
        raise RuntimeError("PyOD is optional; install the benchmark extra to run this detector") from exc
    predictions: list[bool] = []
    values = [point.value for point in points]
    for index, value in enumerate(values):
        history = values[max(0, index - history_size):index]
        if len(history) < max(16, history_size // 2):
            predictions.append(False)
            continue
        detector = IForest(contamination=0.05, random_state=42, n_estimators=64)
        detector.fit([[item] for item in history])
        # Use PyOD's calibrated binary decision instead of assuming a sign
        # convention for ``decision_function`` across PyOD releases.
        predictions.append(bool(detector.predict([[value]])[0] == 1))
    return predictions


def run_benchmark(points: list[Point], *, history_size: int = 24, threshold: float = 3.5) -> dict[str, object]:
    if len(points) <= history_size:
        raise ValueError("dataset is shorter than the benchmark history window")
    detectors = {
        "robust_baseline": lambda: _baseline(points, threshold=threshold, history_size=history_size),
        "river_half_space_trees": lambda: _river(points, threshold=0.8, history_size=history_size),
    }
    results: list[BenchmarkResult] = []
    unavailable: dict[str, str] = {}
    for name, detector in detectors.items():
        started = time.process_time_ns()
        try:
            predicted = detector()
        except RuntimeError as exc:
            unavailable[name] = str(exc)
            continue
        cpu_ms = (time.process_time_ns() - started) / 1_000_000
        results.append(_score_result(name, points, predicted, cpu_ms))
    started = time.process_time_ns()
    try:
        predicted = _pyod(points, threshold=threshold, history_size=history_size)
    except RuntimeError as exc:
        unavailable["pyod_iforest"] = str(exc)
    else:
        cpu_ms = (time.process_time_ns() - started) / 1_000_000
        results.append(_score_result("pyod_iforest", points, predicted, cpu_ms))
    return {
        "format": "NAB-like",
        "dataset_points": len(points),
        "anomaly_points": sum(point.anomaly for point in points),
        "anomaly_events": len(_events(points)),
        "history_size": history_size,
        "threshold": threshold,
        "results": [asdict(result) for result in results],
        "unavailable": unavailable,
        "side_effects": "read-only; no database, alert, notification, remediation, or model-state writes",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="NAB-like CSV: timestamp,value[,anomaly]")
    parser.add_argument("--labels", type=Path, help="optional NAB-style window_start,window_end CSV")
    parser.add_argument("--output", type=Path, help="write JSON evidence to this path")
    parser.add_argument("--history-size", type=int, default=24)
    parser.add_argument("--threshold", type=float, default=3.5)
    args = parser.parse_args()
    report = run_benchmark(load_series(args.input, args.labels), history_size=max(8, args.history_size), threshold=args.threshold)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
