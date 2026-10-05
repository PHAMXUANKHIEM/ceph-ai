#!/usr/bin/env python3
"""Read-only River v2 promotion evidence by scope, with a shadow verdict.

Plan 7.2: before any promotion is even requested, report per
cluster/host/metric scope how many independent verified outcomes exist, how
many were scored (consumed by the learner), rejected (revoked), inconclusive
or stale, how old the samples are, the data-quality ratio of the learner's
audit stream, and whether any label traces back to the candidate model
itself.  The verdict is ``KEEP_SHADOW`` unless every threshold is met, in
which case it is ``ELIGIBLE_FOR_REVIEW`` — an operator decision, never an
automatic promotion.  The script only reads.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The logic lives in shared/ so the weekly digest can reuse it.
from shared.river_v2_evidence import (  # noqa: E402,F401
    CANDIDATE_PREFIX,
    READY_TO_LEARN,
    SCHEMA,
    VERIFIED,
    build_report,
    collect,
    verdict,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, help="write the JSON report here as well as stdout")
    parser.add_argument("--min-verified", type=int, default=100)
    parser.add_argument("--min-scopes", type=int, default=3)
    parser.add_argument("--min-clusters", type=int, default=2)
    parser.add_argument("--min-per-scope", type=int, default=20)
    parser.add_argument("--min-quality", type=float, default=0.8, help="minimum READY_TO_LEARN ratio")
    parser.add_argument("--stale-days", type=float, default=7.0)
    parser.add_argument("--audit-window-days", type=float, default=30.0)
    args = parser.parse_args(argv)

    from shared import db

    now = datetime.now(timezone.utc)
    with db.SessionLocal() as session:
        report = build_report(
            session, now=now, stale_days=args.stale_days, audit_window_days=args.audit_window_days,
            thresholds={
                "min_verified": args.min_verified, "min_scopes": args.min_scopes,
                "min_clusters": args.min_clusters, "min_per_scope": args.min_per_scope,
                "min_quality": args.min_quality,
            },
        )
        session.rollback()
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
