#!/usr/bin/env python3
"""Bounded offline comparison of River HST and optional RRCF."""

from __future__ import annotations

import argparse
import json
import math
import resource
import time
from pathlib import Path

from scripts.forecast_benchmark import Point, _events, load_series


def _quantile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.inf
    return ordered[min(len(ordered) - 1, max(0, int(len(ordered) * fraction)))]


def _scores_to_metrics(name: str, points: list[Point], scores: list[float], cpu_ms: float, rss_kib: int) -> dict[str, object]:
    warmup = max(16, min(len(scores) // 2, 64))
    threshold = _quantile(scores[:warmup], 0.95)
    predicted = [index >= warmup and score >= threshold for index, score in enumerate(scores)]
    labels = [point.anomaly for point in points]
    tp = sum(alert and label for alert, label in zip(predicted, labels))
    fp = sum(alert and not label for alert, label in zip(predicted, labels))
    negatives = sum(not label for label in labels)
    event_delays: list[float] = []
    detected_events = 0
    for event in _events(points):
        hits = [index for index, alert in enumerate(predicted)
                if alert and points[index].timestamp >= event[0].timestamp - (event[0].timestamp - points[max(0, index - 1)].timestamp)]
        if hits:
            detected_events += 1
            event_delays.append(max(0.0, (points[hits[0]].timestamp - event[0].timestamp).total_seconds()))
    return {
        "detector": name,
        "evaluated": len(points),
        "threshold": threshold,
        "predicted_alerts": sum(predicted),
        "precision": tp / sum(predicted) if sum(predicted) else None,
        "recall": tp / sum(labels) if sum(labels) else None,
        "false_positive_rate": fp / negatives if negatives else None,
        "event_recall": detected_events / len(_events(points)) if _events(points) else None,
        "mean_detection_delay_seconds": sum(event_delays) / len(event_delays) if event_delays else None,
        "cpu_time_ms": round(cpu_ms, 3),
        "peak_rss_delta_kib": rss_kib,
        "bounded": True,
    }


def _hst(points: list[Point], *, trees: int, height: int, window: int, seed: int) -> list[float]:
    from river import anomaly

    model = anomaly.HalfSpaceTrees(n_trees=trees, height=height, window_size=window, seed=seed)
    scores: list[float] = []
    previous = None
    for point in points:
        features = {"value": point.value, "delta": 0.0 if previous is None else point.value - previous}
        scores.append(float(model.score_one(features)))
        model.learn_one(features)
        previous = point.value
    return scores


def _rrcf(points: list[Point], *, trees: int, tree_size: int, seed: int) -> list[float]:
    try:
        import rrcf
    except ImportError as exc:
        raise RuntimeError("RRCF is optional; install the benchmark-anomaly extra") from exc
    # RRCF 0.4.4 exposes a bounded tree by explicit FIFO eviction rather than
    # an ``index_capacity`` constructor argument.
    forest = [rrcf.RCTree(random_state=seed + index) for index in range(trees)]
    scores: list[float] = []
    previous = None
    for index, point in enumerate(points):
        vector = [point.value, 0.0 if previous is None else point.value - previous]
        values = []
        for tree in forest:
            if index >= tree_size:
                tree.forget_point(index - tree_size)
            tree.insert_point(vector, index=index)
            values.append(float(tree.codisp(index)))
        scores.append(sum(values) / len(values))
        previous = point.value
    return scores


def run(points: list[Point], *, trees: int, height: int, window: int, tree_size: int, seed: int) -> dict[str, object]:
    results: list[dict[str, object]] = []
    unavailable: dict[str, str] = {}
    for name, function in (
        ("river_hst", lambda: _hst(points, trees=trees, height=height, window=window, seed=seed)),
        ("rrcf", lambda: _rrcf(points, trees=trees, tree_size=tree_size, seed=seed)),
    ):
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        started = time.process_time()
        try:
            scores = function()
        except RuntimeError as exc:
            unavailable[name] = str(exc)
            continue
        results.append(_scores_to_metrics(
            name, points, scores, (time.process_time() - started) * 1000,
            max(0, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before)),
        ))
    return {
        "schema": "ceph-ai.hst-rrcf-benchmark.v1",
        "dataset_points": len(points),
        "features": ["value", "delta"],
        "config": {"trees": trees, "height": height, "window": window, "tree_size": tree_size, "seed": seed},
        "results": results,
        "unavailable": unavailable,
        "side_effects": "read-only; no database, alert, notification, remediation or model-state writes",
        "production_dependency": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trees", type=int, default=10)
    parser.add_argument("--height", type=int, default=8)
    parser.add_argument("--window", type=int, default=128)
    parser.add_argument("--tree-size", type=int, default=64)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if min(args.trees, args.height, args.window, args.tree_size) < 1:
        parser.error("bounded HST/RRCF parameters must be positive")
    report = run(load_series(args.input), trees=args.trees, height=args.height, window=args.window, tree_size=args.tree_size, seed=args.seed)
    with args.output.open("x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
