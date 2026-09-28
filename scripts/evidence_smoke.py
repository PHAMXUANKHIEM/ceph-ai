#!/usr/bin/env python3
"""Run investigation runbooks read-only against a cluster and record an
artifact (autonomy plan WP3): per-collector status and duration plus the
deterministic triage conclusion.  Command output is NOT stored in the
artifact; it can contain cluster details that do not belong in the repo.
Nothing is written to the database."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_CODES = ("NODE_UNREACHABLE:{mon_last}", "OSD_LATENCY_HIGH:1", "MON_CLOCK_SKEW", "PG_DEGRADED", "OSD_DOWN")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cluster", help="cluster id (default: first active cluster)")
    parser.add_argument("--ssh-key", help="read-only SSH key path on this host (overrides the stored path)")
    parser.add_argument("--code", action="append", help="incident code to investigate (repeatable)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--git-sha", default=os.environ.get("GIT_SHA", "unknown"),
                        help="commit recorded in the artifact (default: $GIT_SHA)")
    args = parser.parse_args(argv)

    from shared import db, deterministic_triage, evidence_collectors as ec, investigation_runbooks as ir
    from shared.clusters import list_active_clusters

    with db.SessionLocal() as session:
        clusters = list_active_clusters(session)
        cluster = next((c for c in clusters if c.id == args.cluster), None) if args.cluster else clusters[0]
        if cluster is None:
            print(f"cluster {args.cluster} not found", file=sys.stderr)
            return 2
        session.expunge(cluster)
    transport = ec.SshTransport(cluster)
    if args.ssh_key:
        transport.ssh_key_path = args.ssh_key
    codes = [code.format(mon_last=transport.mon_nodes[-1]) for code in (args.code or DEFAULT_CODES)]
    runs = []
    for code in codes:
        plan = ir.plan(code, ir.context_for(code), mon_host=transport.mon_nodes[0])
        started = time.monotonic()
        results = ec.EvidenceRunner(transport).run(plan.requests)
        rows = [{"collector_id": r.collector_id, "status": r.status, "output_redacted": r.output} for r in results]
        triage = deterministic_triage.triage(code, rows)
        runs.append({
            # Host addresses stay out of a public artifact.
            "code": re.sub(r":[^:]+$", ":<host>", code) if code.startswith("NODE_UNREACHABLE:") else code,
            "runbook": plan.runbook,
            "wall_seconds": round(time.monotonic() - started, 1),
            "collectors": [{"id": r.collector_id, "target_kind": "mon" if r.target == "mon" else "host",
                            "status": r.status, "duration_ms": r.duration_ms, "truncated": r.truncated}
                           for r in results],
            "skipped": plan.skipped,
            "triage": {"conclusion": triage.conclusion, "confidence": triage.confidence,
                       "action_id": triage.action_id, "summary": triage.summary},
        })
    report = {
        "schema": "ceph-ai.evidence-smoke.v1",
        "git_sha": args.git_sha,
        "cluster_name": cluster.name,
        "read_only": True,
        "output_stored": False,
        "runs": runs,
        "summary": {
            "collectors": sum(len(run["collectors"]) for run in runs),
            "ok": sum(1 for run in runs for c in run["collectors"] if c["status"] == ec.OK),
            "max_wall_seconds": max((run["wall_seconds"] for run in runs), default=0),
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
