#!/usr/bin/env python3
"""Collect read-only reliability evidence for a bounded soak window.

Run inside the Ceph-AI checkout with the production environment loaded.  The
script never restarts services, sends alerts, executes SSH/Ceph commands or
mutates application state; it only calls the local reliability collector and
writes a JSON report after the window ends.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from shared.reliability import collect_reliability


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-hours", type=float, default=24.0)
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.duration_hours <= 0 or args.interval_seconds <= 0:
        parser.error("duration-hours và interval-seconds phải lớn hơn 0")
    started = time.monotonic()
    samples = []
    deadline = started + args.duration_hours * 3600
    while True:
        sample = collect_reliability()
        samples.append({
            "at": datetime.now(timezone.utc).isoformat(),
            "status": sample.get("status"),
            "collection_duration_ms": sample.get("collection_duration_ms"),
            "alerts": sample.get("alerts", []),
            "snapshot_freshness": sample.get("snapshot_freshness"),
            "queues": sample.get("queues", []),
            "db_pool": sample.get("db_pool"),
            "api": {"p95_duration_ms": (sample.get("api") or {}).get("p95_duration_ms")},
            "collector": sample.get("collector", {}),
            "ssh": {key: (sample.get("ssh") or {}).get(key) for key in ("command_success_total", "command_failures_total", "connect_failures_total", "p95_duration_ms")},
            "resources": sample.get("resources", {}),
        })
        if time.monotonic() >= deadline:
            break
        time.sleep(min(args.interval_seconds, max(0.1, deadline - time.monotonic())))
    report = {
        "schema": "ceph-ai.reliability-soak.v1",
        "duration_hours_requested": args.duration_hours,
        "interval_seconds": args.interval_seconds,
        "started_at": samples[0]["at"],
        "finished_at": samples[-1]["at"],
        "sample_count": len(samples),
        "runtime_owner": (collect_reliability().get("runtime_owner") or {}),
        "slo": collect_reliability().get("slo", {}),
        "samples": samples,
        "exit_gate": "REVIEW_REQUIRED",
        "note": "Operator phải review alert/error-budget và ký acceptance; script không tự promote/rollback.",
    }
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
