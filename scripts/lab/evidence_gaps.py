#!/usr/bin/python3.11
"""Print the FL6.1 evidence-gap queue: fault families that keep lacking evidence.

Run where the database is reachable, e.g.:
    podman exec ceph-ai_worker_1 python -m scripts.lab.evidence_gaps --days 30
"""

from __future__ import annotations

import argparse
import json


def main() -> int:
    from shared import db
    from shared.evidence_gaps import evidence_gap_queue
    from shared.failure_lab_fault import fault_scenarios
    from watcher.incident_correlation import FAMILY_CODES

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    fault_codes = {fault_id: set(scenario.expected_health_codes) for fault_id, scenario in fault_scenarios().items()}
    with db.SessionLocal() as session:
        queue = evidence_gap_queue(session, family_codes=FAMILY_CODES, fault_codes=fault_codes, days=args.days)
    if args.json:
        print(json.dumps([gap.as_dict() for gap in queue], ensure_ascii=False, indent=2))
        return 0
    for gap in queue:
        print(f"{gap.family:22} {gap.total:4}  (log {gap.insufficient_log_findings}, case {gap.low_confidence_cases})"
              f"  → {gap.next_step}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
