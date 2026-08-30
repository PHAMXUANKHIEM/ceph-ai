"""Entrypoint for the daily, timer-driven AI self-improvement review."""

from __future__ import annotations

import logging
from pathlib import Path


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
    )
    try:
        from config.settings import settings
        from worker.code_repair_supervisor import run_nightly_ai_improvement

        if not settings.ai_nightly_improvement_enabled:
            logging.getLogger(__name__).info("nightly AI improvement is disabled")
            return 0
        run_nightly_ai_improvement(
            Path(__file__).resolve().parents[1],
            Path(settings.ai_nightly_improvement_state_file),
        )
        return 0
    except Exception:
        logging.getLogger(__name__).exception("nightly AI improvement entrypoint failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
