#!/usr/bin/python3.11
"""Run tonight's Failure Lab campaign if it is due (ceph-ai-failure-lab.timer).

A no-op unless Settings > Cụm Staging has fault injection on and the time is
inside its window. Manually:
    podman exec ceph-ai_worker_1 python -m scripts.lab.failure_lab_campaign
"""

from __future__ import annotations

import json
import logging


def main() -> int:
    from shared import db
    from shared.failure_lab_campaign import run_campaign

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    result = run_campaign(db.SessionLocal)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
