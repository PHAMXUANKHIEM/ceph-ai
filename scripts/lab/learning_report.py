#!/usr/bin/python3.11
"""Print the FL6.5 learning report: per fault family, how the AI learns from lab runs.

Run where the database is reachable, e.g.:
    podman exec ceph-ai_worker_1 python -m scripts.lab.learning_report
"""

from __future__ import annotations

import argparse
import json


def main() -> int:
    from shared import db
    from shared.failure_lab_report import family_summary_text, learning_report
    from watcher.incident_correlation import FAMILY_CODES

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    with db.SessionLocal() as session:
        report = learning_report(session, family_codes=FAMILY_CODES)
    if args.json:
        print(json.dumps([item.as_dict() for item in report], ensure_ascii=False, indent=2))
        return 0
    if not report:
        print("Chưa có lượt Failure Lab nào được gắn nhãn.")
    for item in report:
        print(family_summary_text(item), end="\n\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
