#!/usr/bin/env python3
"""Read-only operational summary of verified River v2 label provenance."""

from __future__ import annotations

import json

from sqlalchemy import text

from config.settings import settings
from shared import db


def main() -> int:
    with db.SessionLocal() as session:
        total = session.scalar(text("""
            SELECT COUNT(*) FROM online_learner_labels
            WHERE outcome IN ('VERIFIED_SUCCESS', 'VERIFIED_FAILED')
              AND evidence_fingerprint IS NOT NULL
        """)) or 0
        recent_window_labels = session.scalar(text("""
            WITH ranked AS (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY cluster_name, host, metric, horizon_hours
                    ORDER BY predicted_at DESC
                ) AS position
                FROM node_resource_forecast_runs
                WHERE status = 'EVALUATED' AND algorithm = 'linear'
            )
            SELECT COUNT(*)
            FROM online_learner_labels AS l
            JOIN ranked AS r ON r.id = l.source_run_id
            WHERE r.position <= 512
              AND l.outcome IN ('VERIFIED_SUCCESS', 'VERIFIED_FAILED')
              AND l.evidence_fingerprint IS NOT NULL
        """)) or 0
        rows = session.execute(text("""
            SELECT l.cluster_key, l.host, l.metric, COUNT(*) AS labels,
                   MAX(l.outcome_observed_at) AS latest_observed
            FROM online_learner_labels AS l
            WHERE l.outcome IN ('VERIFIED_SUCCESS', 'VERIFIED_FAILED')
              AND l.evidence_fingerprint IS NOT NULL
            GROUP BY l.cluster_key, l.host, l.metric
            ORDER BY labels DESC, l.cluster_key, l.host, l.metric
            LIMIT 100
        """)).all()
        blocked = session.execute(text("""
            SELECT LEFT(reason, 120), COUNT(*)
            FROM online_learner_label_events
            WHERE action = 'BLOCKED'
              AND created_at >= (now() AT TIME ZONE 'UTC') - INTERVAL '1 hour'
            GROUP BY LEFT(reason, 120)
            ORDER BY COUNT(*) DESC
            LIMIT 10
        """)).all()
        payload = {
            "read_only": True,
            "algorithm": "river_linear_v2",
            "execution_mode": settings.online_learning_mode,
            "verified_labels_total": total,
            "verified_labels_in_replay_window": recent_window_labels,
            "scopes_truncated": len(rows) == 100,
            "scopes": [
                {
                    "cluster_key": row.cluster_key,
                    "host": row.host,
                    "metric": row.metric,
                    "verified_labels": row.labels,
                    "latest_observed_at": row.latest_observed.isoformat() if row.latest_observed else None,
                }
                for row in rows
            ],
            "recent_blocked_reasons": [
                {"reason": reason, "count": count} for reason, count in blocked
            ],
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
