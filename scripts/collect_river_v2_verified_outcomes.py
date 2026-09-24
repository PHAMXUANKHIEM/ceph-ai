#!/usr/bin/env python3
"""Select independent telemetry matches and enqueue River v2 labels.

Dry-run by default.  PostgreSQL-only selection; every write goes through the
existing online-learning label policy, never through an ad-hoc INSERT.
"""

from __future__ import annotations

import argparse
import json

from sqlalchemy import text

from config.settings import settings
from shared import db
from shared.online_learning_labels import enqueue_verified_outcomes


_CANDIDATES = text("""
SELECT r.id
FROM node_resource_forecast_runs AS r
JOIN clusters AS c ON c.name = r.cluster_name
JOIN LATERAL (
    SELECT a.value, a.label, a.update_applied
    FROM online_learner_audit AS a
    WHERE a.cluster_key = c.id
      AND a.host = r.host
      AND a.metric = CASE WHEN r.metric IN ('memory', 'mem') THEN 'ram' ELSE r.metric END
      AND a.observed_at BETWEEN
          r.target_at - (:tolerance_seconds * INTERVAL '1 second') AND
          r.target_at + (:tolerance_seconds * INTERVAL '1 second')
    ORDER BY ABS(EXTRACT(EPOCH FROM (a.observed_at - r.target_at))), a.observed_at
    LIMIT 1
) AS a ON TRUE
LEFT JOIN online_learner_labels AS l ON l.source_run_id = r.id
WHERE r.cluster_name = :cluster_name
  AND r.status = 'EVALUATED'
  AND r.actual_percent BETWEEN 0 AND 100
  AND r.evaluated_at >= (now() AT TIME ZONE 'UTC') - INTERVAL '30 days'
  AND r.evaluated_at <= (now() AT TIME ZONE 'UTC') + INTERVAL '5 minutes'
  AND r.evaluated_at >= r.target_at
  AND r.target_at >= r.predicted_at
  AND a.label IS NULL
  AND a.update_applied = FALSE
  AND ABS(a.value - r.actual_percent) <= 0.01
  AND l.id IS NULL
ORDER BY r.evaluated_at DESC
LIMIT :limit
OFFSET :offset
""")


def collect(*, cluster_name: str, limit: int = 50, offset: int = 0, apply: bool = False) -> dict:
    if not cluster_name.strip() or not 1 <= limit <= 100 or not 0 <= offset <= 10000:
        raise ValueError("cluster_name, limit 1..100 and offset 0..10000 are required")
    with db.SessionLocal() as session:
        if session.bind.dialect.name != "postgresql":
            raise RuntimeError("candidate selection requires PostgreSQL")
        session.execute(text("SET LOCAL statement_timeout = '30s'"))
        run_ids = list(session.scalars(_CANDIDATES, {
            "cluster_name": cluster_name,
            "tolerance_seconds": max(
                30.0, float(settings.node_resource_learning_max_outcome_gap_hours) * 3600,
            ),
            "limit": limit,
            "offset": offset,
        }))
        created = 0
        if apply and run_ids:
            created = enqueue_verified_outcomes(
                session, max_rows=limit, source_run_ids=run_ids,
            )
            session.commit()
        return {
            "cluster_name": cluster_name,
            "offset": offset,
            "candidate_runs": len(run_ids),
            "labels_created": created,
            "applied": apply,
            "execution_mode": "SHADOW_ONLY",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", default=settings.cluster_name)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(collect(
        cluster_name=args.cluster_name, limit=args.limit,
        offset=args.offset, apply=args.apply,
    )))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
