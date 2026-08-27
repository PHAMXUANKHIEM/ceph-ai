#!/usr/bin/env python3
"""Print aggregate AI health metrics without exposing request content."""
import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timedelta

from shared import db
from shared.models import AIInvocation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    cutoff = datetime.utcnow() - timedelta(hours=max(1, args.hours))
    with db.SessionLocal() as session:
        rows = session.query(AIInvocation).filter(AIInvocation.created_at >= cutoff).all()
    groups = defaultdict(list)
    for row in rows:
        groups[(row.feature, row.provider, row.model_id)].append(row)
    output = []
    for (feature, provider, model_id), items in sorted(groups.items()):
        latencies = sorted(item.latency_ms for item in items)
        errors = sum(item.status == "ERROR" for item in items)
        output.append({
            "feature": feature, "provider": provider, "model_id": model_id,
            "calls": len(items), "errors": errors,
            "error_rate": round(errors / len(items), 4),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1),
            "p95_latency_ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)],
            "input_chars": sum(item.input_chars for item in items),
            "output_chars": sum(item.output_chars for item in items),
        })
    print(json.dumps({"hours": max(1, args.hours), "groups": output}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
