#!/usr/bin/env python3
"""Offline River-vs-VW contextual-bandit benchmark.

This file is evaluation-only. It must run in an isolated environment with
Vowpal Wabbit installed and is never imported by Watcher or worker runtime.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import resource
import time

os.environ.setdefault("OMP_NUM_THREADS", "1")


ACTIONS = ("OBSERVE", "COLLECT_DIAGNOSTICS", "OPEN_TICKET")


def _cost(severity: int, action: int) -> tuple[float, bool]:
    optimal = 2 if severity else 0
    cost = 0.1 if action == optimal else (0.35 if action == 1 else 0.8)
    false_positive = action == 2 and severity == 0
    return cost, false_positive


def _metrics(costs: list[float], false_positives: int, actions: list[int], optimal_cost: float) -> dict[str, object]:
    regret = sum(costs) - optimal_cost
    switches = sum(left != right for left, right in zip(actions, actions[1:]))
    return {
        "regret": round(regret, 6),
        "false_positive_rate": round(false_positives / len(actions), 6),
        "stability_switch_rate": round(switches / max(1, len(actions) - 1), 6),
        "samples": len(actions),
    }


def run_benchmark(samples: int = 200) -> dict[str, object]:
    try:
        from river import stats
        from vowpalwabbit import Workspace
    except ImportError as exc:  # pragma: no cover - evaluation venv only
        raise RuntimeError("VW benchmark requires the isolated evaluation extra") from exc

    baseline = stats.Mean()
    baseline_costs: list[float] = []
    baseline_actions: list[int] = []
    baseline_fp = 0
    vw_costs: list[float] = []
    vw_actions: list[int] = []
    vw_fp = 0
    optimal_cost = 0.1 * samples
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    baseline_wall_ms = baseline_cpu_ms = 0.0
    vw_wall_ms = vw_cpu_ms = 0.0
    baseline_rss_delta = vw_rss_delta = 0
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        vw = Workspace("--quiet --cb_explore 3 --epsilon 0.2")
        for index in range(samples):
            severity = index % 2
            # River mean is deliberately a transparent non-policy baseline:
            # it always recommends observation and learns the observed cost.
            baseline_wall_started = time.perf_counter()
            baseline_cpu_started = time.process_time()
            baseline_rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            baseline_action = 0
            base_cost, base_fp = _cost(severity, baseline_action)
            baseline_actions.append(baseline_action)
            baseline_costs.append(base_cost)
            baseline_fp += int(base_fp)
            baseline.update(base_cost)
            baseline_wall_ms += (time.perf_counter() - baseline_wall_started) * 1000
            baseline_cpu_ms += (time.process_time() - baseline_cpu_started) * 1000
            baseline_rss_delta = max(
                baseline_rss_delta,
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - baseline_rss_before,
            )

            vw_wall_started = time.perf_counter()
            vw_cpu_started = time.process_time()
            vw_rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            context = vw.parse(f"| x severity:{severity}")
            probabilities = vw.predict(context)
            selected = max(range(len(probabilities)), key=lambda item: probabilities[item])
            vw.finish_example(context)
            selected_cost, selected_fp = _cost(severity, selected)
            probability = max(float(probabilities[selected]), 1e-6)
            learned = vw.parse(
                f"{selected + 1}:{selected_cost}:{probability} | x severity:{severity}"
            )
            vw.learn(learned)
            vw.finish_example(learned)
            vw_actions.append(selected)
            vw_costs.append(selected_cost)
            vw_fp += int(selected_fp)
            vw_wall_ms += (time.perf_counter() - vw_wall_started) * 1000
            vw_cpu_ms += (time.process_time() - vw_cpu_started) * 1000
            vw_rss_delta = max(
                vw_rss_delta,
                resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - vw_rss_before,
            )
        vw.finish()
    after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {
        "schema": "ceph-ai.vw-bandit-benchmark.v1",
        "vowpal_wabbit_version": "9.11.2",
        "samples": samples,
        "baseline": _metrics(baseline_costs, baseline_fp, baseline_actions, optimal_cost),
        "vowpal_wabbit": _metrics(vw_costs, vw_fp, vw_actions, optimal_cost),
        "river_baseline_state": {"mean_cost": baseline.get(), "explainability": "scalar mean"},
        "vw_explainability": "action distribution only; no Ceph command authority",
        "resource_by_backend": {
            "river": {
                "wall_ms": round(baseline_wall_ms, 3),
                "cpu_ms": round(baseline_cpu_ms, 3),
                "rss_delta_kib": max(0, baseline_rss_delta),
            },
            "vowpal_wabbit": {
                "wall_ms": round(vw_wall_ms, 3),
                "cpu_ms": round(vw_cpu_ms, 3),
                "rss_delta_kib": max(0, vw_rss_delta),
            },
        },
        "wall_ms": round((time.perf_counter() - wall_started) * 1000, 3),
        "cpu_ms": round((time.process_time() - cpu_started) * 1000, 3),
        "rss_delta_kib": max(0, after - before),
        "production_dependency": False,
        "executable_recommendations": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    report = run_benchmark()
    with open(args.output, "x", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
