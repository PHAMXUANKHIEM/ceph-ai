"""Run a Failure Lab replay campaign against a lab cluster (FL1).

Each scenario injects one synthetic Incident (never executed by the Worker),
waits for the Worker's diagnosis, scores it and closes that run's synthetic
rows. The report is written under /var/lib/ceph-ai/failure-lab/.

Usage::

    python -m scripts.lab.failure_lab_replay --cluster-id <LAB_UUID> --scenario all
    python -m scripts.lab.failure_lab_replay --cluster-id <LAB_UUID> --scenario osd_down --scenario large_omap
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

from shared import db
from shared.failure_lab import report_json, run_campaign
from shared.synthetic_incidents import SyntheticInjectionError, scenarios
from watcher import publisher

REPORT_DIR = Path("/var/lib/ceph-ai/failure-lab")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cluster-id", required=True, help="cluster with autonomy_environment=lab")
    parser.add_argument("--scenario", action="append", required=True, help="scenario id, repeatable, or 'all'")
    parser.add_argument("--wait-seconds", type=float, default=300, help="how long to wait for each diagnosis")
    parser.add_argument("--output-dir", type=Path, default=REPORT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    catalog = scenarios()
    chosen = sorted(catalog) if "all" in args.scenario else args.scenario
    unknown = [scenario_id for scenario_id in chosen if scenario_id not in catalog]
    if unknown:
        print(f"unknown scenario(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    campaign_id = "replay-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        report = run_campaign(
            db.SessionLocal, cluster_id=args.cluster_id, scenario_ids=chosen, campaign_id=campaign_id,
            publish=lambda envelope: asyncio.run(publisher.publish_incident(envelope)),
            wait_seconds=args.wait_seconds,
        )
    except SyntheticInjectionError as exc:
        print(f"failure lab refused: {exc}", file=sys.stderr)
        return 2
    args.output_dir.mkdir(parents=True, exist_ok=True)
    path = args.output_dir / f"{campaign_id}.json"
    path.write_text(report_json(report) + "\n", encoding="utf-8")
    print(f"{campaign_id}: {report['passed_count']}/{report['run_count']} passed -> {path}")
    for result in report["results"]:
        failed = [stage for stage, ok in result["stages"].items() if ok is False]
        print(f"  {result['scenario_id']}: {'PASS' if result['passed'] else 'FAIL ' + ', '.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
