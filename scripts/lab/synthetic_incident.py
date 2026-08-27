#!/usr/bin/env python3
"""Inject a shadow-only Incident into the normal AI pipeline.

This command never changes Ceph. It requires the selected cluster to be
explicitly marked ``autonomy_environment=lab`` and only publishes a marked
envelope when ``--publish`` is supplied. Generated Actions are blocked from
all real executors, including manual approval.

Examples::

    python -m scripts.lab.synthetic_incident --list
    python -m scripts.lab.synthetic_incident --cluster-id UUID --scenario osd_down --publish
    python -m scripts.lab.synthetic_incident --cluster-id UUID --cleanup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from shared import db
from shared.models import Cluster
from shared.synthetic_incidents import SCENARIOS, SyntheticInjectionError, cleanup, create
from watcher import publisher


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list available scenarios")
    parser.add_argument("--cluster-id", help="active lab cluster UUID")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), help="scenario to inject")
    parser.add_argument("--publish", action="store_true", help="publish to the AI RabbitMQ queue")
    parser.add_argument("--cleanup", action="store_true", help="close synthetic incidents only")
    parser.add_argument("--run-id", help="cleanup one injection run only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.list:
        for scenario in SCENARIOS.values():
            print(f"{scenario.id}\t{scenario.ceph_code}\t{scenario.message}")
        return 0
    if not args.cluster_id:
        print("--cluster-id is required", file=sys.stderr)
        return 2
    if args.cleanup and args.scenario:
        print("--cleanup cannot be combined with --scenario", file=sys.stderr)
        return 2
    if not args.cleanup and not args.scenario:
        print("--scenario is required unless --cleanup is used", file=sys.stderr)
        return 2

    with db.SessionLocal() as session:
        cluster = session.get(Cluster, args.cluster_id)
        if cluster is None:
            print("active cluster not found", file=sys.stderr)
            return 2
        if args.cleanup:
            changed = cleanup(session, cluster_id=cluster.id, run_id=args.run_id)
            session.commit()
            print(json.dumps({"cluster_id": cluster.id, "closed": changed}))
            return 0
        try:
            incident, envelope = create(
                session, cluster=cluster, scenario_id=args.scenario, actor="synthetic-cli",
            )
            session.commit()
            incident_id = incident.id
        except SyntheticInjectionError as exc:
            session.rollback()
            print(str(exc), file=sys.stderr)
            return 2

    published = False
    if args.publish:
        try:
            asyncio.run(publisher.publish_incident(envelope))
            published = True
        except Exception as exc:
            print(f"publish failed: {exc}", file=sys.stderr)
            return 1
    print(json.dumps({
        "incident_id": incident_id,
        "run_id": envelope["synthetic_run_id"],
        "scenario": envelope["synthetic_scenario"],
        "published": published,
        "mode": envelope["synthetic_mode"],
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
