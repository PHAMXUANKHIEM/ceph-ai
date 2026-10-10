#!/usr/bin/env python3
"""Record whether the natural-language canary gate has passed.

The gate needs both MINIMUM_HOURS of monitoring and MINIMUM_SAMPLES answers
carrying a natural-language context (scripts/report_natural_language_rollout).
The first version only checked the hours and ticked the plan's checklist
itself, inside the deployed checkout: it closed the canary after 497 hours
with 0 answers recorded, and any such write leaves the checkout dirty, which
blocks the next deploy. The result now goes to a state file only; the plan
checkbox is changed through a reviewed branch like any other plan change.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from scripts.report_natural_language_rollout import _monitoring_window, _window_start, count_samples
from shared.time import utc_now

GATE_PATH = "/var/lib/ceph-ai/nl-rollout-gate.json"


def gate_status(*, state_path: str | Path, samples: int, now: datetime | None = None) -> dict:
    """{"status": "passed" | "waiting", "monitoring_window": {...}} for the given answer count."""
    window = _monitoring_window(now=now or utc_now(), state_path=state_path, samples=samples)
    return {"status": "passed" if window["ready_for_close"] else "waiting", "monitoring_window": window}


def record_gate(result: dict, gate_path: str | Path) -> None:
    path = Path(gate_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        # Printing the result (the unit's log) still records it.
        pass


def main() -> None:
    from shared import db

    parser = argparse.ArgumentParser()
    parser.add_argument("--state-file", default="/var/lib/ceph-ai/nl-rollout-monitoring-start.json")
    parser.add_argument("--gate-file", default=GATE_PATH)
    parser.add_argument("--cluster-id", default=None)
    args = parser.parse_args()
    now = utc_now()
    window = _monitoring_window(now=now, state_path=args.state_file)
    with db.SessionLocal() as session:
        samples = count_samples(session, since=_window_start(window), cluster_id=args.cluster_id)
    result = gate_status(state_path=args.state_file, samples=samples, now=now)
    record_gate(result, args.gate_file)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
