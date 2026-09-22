"""Offline benchmark for the multivariate anomaly candidate detectors.

CSV columns are ``timestamp``, the eight feature names from
``shared.multivariate_anomaly.FEATURE_NAMES``, optional ``peer_class`` and
optional ``anomaly``.  The command is read-only and never opens alerts or
remediation actions.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.multivariate_anomaly import (
    FEATURE_NAMES,
    MultivariateSample,
    calibrate_threshold,
    candidate_evidence,
    candidate_scores,
    cluster_incident_windows,
    fit_peer_baselines,
    normalize_scores,
    run_river_hst,
    run_rrcf,
    standardize_samples,
)


def _timestamp(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def load_dataset(path: str | Path) -> tuple[list[MultivariateSample], list[bool]]:
    samples: list[MultivariateSample] = []
    labels: list[bool] = []
    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            try:
                values = {feature: float(row[feature]) for feature in FEATURE_NAMES}
                timestamp = _timestamp(row["timestamp"])
            except (KeyError, TypeError, ValueError):
                continue
            samples.append(
                MultivariateSample(
                    timestamp=timestamp,
                    values=values,
                    quality_status="OK",
                    quality_by_metric={feature: "OK" for feature in FEATURE_NAMES},
                    missing_features=(),
                    coverage_ratio=1.0,
                    peer_class=(row.get("peer_class") or "global").strip() or "global",
                )
            )
            labels.append(str(row.get("anomaly", "0")).strip().lower() in {"1", "true", "yes", "anomaly"})
    if len(samples) != len(labels):
        raise ValueError("dataset labels and samples are not aligned")
    return samples, labels


def _events(labels: list[bool]) -> list[tuple[int, int]]:
    events: list[tuple[int, int]] = []
    start: int | None = None
    for index, label in enumerate(labels + [False]):
        if label and start is None:
            start = index
        elif not label and start is not None:
            events.append((start, index - 1))
            start = None
    return events


def _metrics(labels: list[bool], predicted: list[bool], *, detector: str, cpu_ms: float) -> dict[str, Any]:
    positives = sum(labels)
    predicted_count = sum(predicted)
    true_positives = sum(actual and found for actual, found in zip(labels, predicted))
    false_positives = sum((not actual) and found for actual, found in zip(labels, predicted))
    false_negatives = sum(actual and (not found) for actual, found in zip(labels, predicted))
    detected_events = sum(any(predicted[index] for index in range(start, end + 1)) for start, end in _events(labels))
    event_count = len(_events(labels))
    return {
        "detector": detector,
        "evaluated": len(labels),
        "predicted_alerts": predicted_count,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": true_positives / predicted_count if predicted_count else None,
        "recall": true_positives / positives if positives else None,
        "event_recall": detected_events / event_count if event_count else None,
        "cpu_time_ms": round(cpu_ms, 3),
    }


def _run_detector(name: str, scores: list, labels: list[bool], history_size: int) -> tuple[dict[str, Any], list]:
    raw = [item.raw_score for item in scores]
    reference = [item.raw_score for item, label in zip(scores, labels) if not label and item.raw_score is not None]
    normalized = normalize_scores(raw, reference)
    scored = [replace(item, score=value) for item, value in zip(scores, normalized)]
    threshold = calibrate_threshold(
        [item.score for item, label in zip(scored, labels) if not label],
        quantile=0.99,
        minimum=0.8,
    )
    started = time.process_time_ns()
    candidates = candidate_scores(scored, threshold=threshold)
    windows = cluster_incident_windows(candidates)
    predicted = [
        any(window.start <= sample.timestamp <= window.end for window in windows)
        for sample in [item for item in scored]
    ]
    result = _metrics(labels, predicted, detector=name, cpu_ms=(time.process_time_ns() - started) / 1_000_000)
    result.update({"threshold": threshold, "incident_windows": len(windows), "history_size": history_size})
    return result, candidate_evidence(windows)


def run_benchmark(samples: list[MultivariateSample], labels: list[bool], *, history_size: int = 24) -> dict[str, Any]:
    if len(samples) <= max(8, int(history_size)):
        raise ValueError("dataset is shorter than the benchmark warm-up")
    baselines = fit_peer_baselines(
        [sample for sample, label in zip(samples, labels) if not label][: max(8, int(history_size))],
        min_samples=max(8, int(history_size)),
    )
    normalized = standardize_samples(samples, baselines)
    results: list[dict[str, Any]] = []
    evidence: dict[str, list[dict[str, object]]] = {}
    unavailable: dict[str, str] = {}
    started = time.process_time_ns()
    hst_scores = run_river_hst(normalized, warmup=history_size)
    hst_result, hst_evidence = _run_detector("river_hst", hst_scores, labels, history_size)
    hst_result["cpu_time_ms"] = round((time.process_time_ns() - started) / 1_000_000, 3)
    results.append(hst_result)
    evidence["river_hst"] = hst_evidence
    try:
        started = time.process_time_ns()
        rrcf_scores = run_rrcf(normalized, warmup=history_size)
    except RuntimeError as exc:
        unavailable["rrcf"] = str(exc)
    else:
        rrcf_result, rrcf_evidence = _run_detector("rrcf", rrcf_scores, labels, history_size)
        rrcf_result["cpu_time_ms"] = round((time.process_time_ns() - started) / 1_000_000, 3)
        results.append(rrcf_result)
        evidence["rrcf"] = rrcf_evidence
    return {
        "format": "Ceph-multivariate-v1",
        "features": list(FEATURE_NAMES),
        "dataset_points": len(samples),
        "anomaly_points": sum(labels),
        "peer_baselines": {name: asdict(value) for name, value in baselines.items()},
        "results": results,
        "unavailable": unavailable,
        "candidate_evidence": evidence,
        "side_effects": "read-only; candidate evidence only; no alert, notification, database, executor, or remediation writes",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--history-size", type=int, default=24)
    args = parser.parse_args()
    report = run_benchmark(*load_dataset(args.input), history_size=max(8, args.history_size))
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
