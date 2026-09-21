#!/usr/bin/env python3
"""Measure the local River learner cost without touching Ceph or the database.

This is an offline acceptance aid. It intentionally reports ``db_writes=0``:
the persistence path is measured separately by the consumer tests, while this
benchmark isolates the model CPU, latency, memory and snapshot footprint.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import time

from shared.online_learning import RiverMeanLearner


def benchmark(streams: int, samples_per_stream: int) -> dict[str, float | int]:
    learners = [RiverMeanLearner() for _ in range(streams)]
    latencies_ms: list[float] = []
    before_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    started = time.perf_counter()
    cpu_started = time.process_time()
    for sample_index in range(samples_per_stream):
        for stream_index, learner in enumerate(learners):
            update_started = time.perf_counter_ns()
            learner.learn_one(float((sample_index + stream_index) % 100))
            latencies_ms.append((time.perf_counter_ns() - update_started) / 1_000_000)
    elapsed_ms = (time.perf_counter() - started) * 1000
    cpu_ms = (time.process_time() - cpu_started) * 1000
    after_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    state_bytes = sum(learner.resource_cost()["state_bytes"] for learner in learners)
    return {
        "streams": streams,
        "samples_per_stream": samples_per_stream,
        "updates": streams * samples_per_stream,
        "elapsed_ms": round(elapsed_ms, 3),
        "cpu_ms": round(cpu_ms, 3),
        "peak_rss_delta_kib": max(0, after_rss - before_rss),
        "p50_update_ms": round(statistics.median(latencies_ms), 6),
        "p95_update_ms": round(statistics.quantiles(latencies_ms, n=20)[18], 6),
        "state_bytes": int(state_bytes),
        "db_writes": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples-per-stream", type=int, default=100)
    args = parser.parse_args()
    if args.samples_per_stream < 1:
        parser.error("--samples-per-stream must be positive")
    print(json.dumps([
        benchmark(streams, args.samples_per_stream)
        for streams in (1, 10, 100)
    ], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
