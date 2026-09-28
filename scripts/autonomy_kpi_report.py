#!/usr/bin/env python3
"""Print the autonomy KPIs (autonomy plan WP0); read-only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--cluster", help="cluster id (default: every cluster)")
    parser.add_argument("--output", type=Path, help="also write the JSON report here")
    args = parser.parse_args(argv)

    from shared import db
    from shared.autonomy_kpi import collect

    with db.SessionLocal() as session:
        report = collect(session, days=args.days, cluster_id=args.cluster)
        session.rollback()
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
