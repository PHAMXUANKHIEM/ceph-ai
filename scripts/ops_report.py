#!/usr/bin/python3.11
"""Print the per-fault-family operations report (croit-parity C6).

    podman exec ceph-ai_worker_1 python -m scripts.ops_report --days 30 [--json]
"""

from __future__ import annotations

import argparse
import json


def main() -> int:
    from shared import db
    from shared.ops_metrics import ops_report
    from watcher.incident_correlation import FAMILY_CODES

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    with db.SessionLocal() as session:
        rows = [row.as_dict() for row in ops_report(session, family_codes=FAMILY_CODES, days=args.days)]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    for row in rows:
        print(f"{row['family']:22} {row['incidents']:4} sự cố, mở {row['open']:3}, MTTR~{row['mttr_minutes_estimate']} phút, "
              f"AI tự đóng {row['auto_fixed']}/{row['closed']}, chẩn đoán đúng {row['diagnosis_accuracy']} "
              f"({row['diagnoses_judged']}), sửa thành công {row['fix_success']} ({row['fixes_verified']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
