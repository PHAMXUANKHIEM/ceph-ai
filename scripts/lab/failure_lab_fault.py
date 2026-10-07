"""Run one reversible real fault on the lab cluster (Failure Lab FL2).

Every gate in shared/failure_lab_fault.py must hold (FAILURE_LAB_FAULT_ENABLED,
autonomy_environment=lab, pinned fsid, no HALT file, ...). The fault is always
undone, also on Ctrl-C or SIGTERM. Run it inside the Worker container, which
holds the mutation identity::

    podman exec -it ceph-ai_worker_1 python -m scripts.lab.failure_lab_fault \\
        --cluster-id <LAB_UUID> --scenario osd_down_fault

``--scheduled`` (for the timer, FL4) also requires FAILURE_LAB_WINDOW.
The report is written under /var/lib/ceph-ai/failure-lab/.
"""

from __future__ import annotations

import argparse
import signal
import sys
from datetime import datetime, timezone

from shared import db
from shared.failure_lab_fault import STATE_DIR, FaultRefused, fault_scenarios, report_json, run_fault


def _interrupt(_signum, _frame):
    raise KeyboardInterrupt  # unwinds through run_fault's finally, which undoes the fault


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cluster-id", required=True)
    parser.add_argument("--scenario", required=True, choices=sorted(fault_scenarios()))
    parser.add_argument("--scheduled", action="store_true", help="refuse outside FAILURE_LAB_WINDOW")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, _interrupt)
    try:
        result = run_fault(db.SessionLocal, cluster_id=args.cluster_id, scenario_id=args.scenario,
                           scheduled=args.scheduled)
    except FaultRefused as exc:
        print(f"failure lab refused: {exc}", file=sys.stderr)
        return 2
    path = STATE_DIR / f"{result['run_id']}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(report_json(result) + "\n", encoding="utf-8")
    failed = [stage for stage, ok in result["stages"].items() if not ok]
    print(f"{args.scenario} on {result['target']}: {'PASS' if result['passed'] else 'FAIL ' + ', '.join(failed)} -> {path}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
