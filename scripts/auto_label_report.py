#!/usr/bin/env python3
"""Print weak-supervision auto-label quality (autonomy plan WP2.3); read-only."""

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
    parser.add_argument("--output", type=Path, help="also write the JSON report here")
    args = parser.parse_args(argv)

    from shared import db
    from shared.auto_labels import collect_facts, quality_report

    with db.SessionLocal() as session:
        report = quality_report(collect_facts(session, days=args.days))
        session.rollback()
    report["window_days"] = args.days
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
