#!/usr/bin/env python3
"""Close the rollout checklist only after the persisted 24-hour gate passes."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from scripts.report_natural_language_rollout import _monitoring_window
from shared.time import utc_now


CHECKBOX = "- [ ] Theo dõi latency, cost, rejection và approval trong 24–72 giờ."
DONE_CHECKBOX = (
    "- [x] Theo dõi latency, cost, rejection và approval trong 24–72 giờ; "
    "monitoring gate đạt tối thiểu 24 giờ và report đã được lưu trong rollout log."
)


def close_plan_if_ready(
    *, plan_path: str | Path,
    state_path: str | Path,
    now: datetime | None = None,
) -> dict:
    now = now or utc_now()
    window = _monitoring_window(now=now, state_path=state_path)
    if not window["ready_for_close"]:
        return {"status": "waiting", "monitoring_window": window}

    path = Path(plan_path)
    text = path.read_text(encoding="utf-8")
    if CHECKBOX not in text:
        if DONE_CHECKBOX in text:
            return {"status": "already_closed", "monitoring_window": window}
        raise RuntimeError("natural-language monitoring checkbox was not found")
    path.write_text(text.replace(CHECKBOX, DONE_CHECKBOX, 1), encoding="utf-8")
    return {"status": "closed", "monitoring_window": window}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--plan",
        default="/root/ceph-ai/Plan/in-progress/natural-language-ceph-ai-plan.md",
    )
    parser.add_argument(
        "--state-file",
        default="/var/lib/ceph-ai/nl-rollout-monitoring-start.json",
    )
    args = parser.parse_args()
    print(json.dumps(close_plan_if_ready(
        plan_path=args.plan,
        state_path=args.state_file,
    ), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
