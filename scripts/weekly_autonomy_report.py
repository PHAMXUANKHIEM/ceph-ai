#!/usr/bin/env python3
"""Print the weekly autonomy report for every active cluster (autonomy plan WP8); read-only."""

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
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--output", type=Path, help="also write the JSON here")
    parser.add_argument("--text", action="store_true", help="print the Telegram lines instead of JSON")
    args = parser.parse_args(argv)

    from shared import db, weekly_autonomy_report
    from shared.clusters import list_active_clusters

    with db.SessionLocal() as session:
        clusters = list_active_clusters(session)
        reports = [weekly_autonomy_report.build(session, cluster, period_days=args.days) for cluster in clusters]
        session.rollback()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(reports, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    if args.text:
        for report in reports:
            print(f"== {report['cluster']}")
            print("\n".join(weekly_autonomy_report.format_lines(report)))
    else:
        print(json.dumps(reports, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
