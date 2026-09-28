#!/usr/bin/env python3
"""Off-policy evaluation report over the decision log (autonomy plan WP6.2); read-only."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def build(logged, unknown: int) -> dict:
    from shared import off_policy_evaluation as ope
    from shared.decision_log import ESCALATE

    deterministic = bool(logged) and all(item.propensity == 1.0 for item in logged)
    policies = {"logged": ope.logged_policy(logged), "always_escalate": ope.always(ESCALATE)}
    estimates = {}
    for name, policy in policies.items():
        result = ope.estimate(logged, policy, method="snips", resamples=500)
        estimates[name] = {**result.__dict__}
    return {
        "schema": "ceph-ai.ope-report.v1",
        "decisions_with_reward": len(logged),
        "decisions_without_reward": unknown,
        "by_source": dict(Counter(item.chosen_by for item in logged)),
        "by_family": dict(Counter(item.fault_family for item in logged).most_common(15)),
        "reward_mean": round(sum(item.reward for item in logged) / len(logged), 4) if logged else None,
        "estimates_snips": estimates,
        "deterministic_logging": deterministic,
        "note": ("All logged propensities are 1.0: only policies that repeat the logged choice have support, "
                 "so a different policy cannot be evaluated until exploration (WP6.3) is logged.")
        if deterministic else "",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    from shared import db, decision_log

    with db.SessionLocal() as session:
        logged, unknown = decision_log.load(session, days=args.days)
        session.rollback()
    report = {**build(logged, unknown), "window_days": args.days}
    text = json.dumps(report, indent=2, sort_keys=True, default=str)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
