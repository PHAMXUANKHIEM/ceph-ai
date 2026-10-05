"""Read model for the "Tiến độ tự vận hành" card on /ai-learning (plan WP8).

One read-only snapshot per cluster of the self-learning loop, built from the
modules that already compute each part:

* evidence (WP3) and decisions (WP6) from shared.weekly_autonomy_report,
* the shadow policy's false-release rate (WP6) from shared.shadow_policy,
* the off-policy evaluation summary (WP6) from scripts/ope_report.py,
* online learning (WP7) from shared.river_v2_evidence.

Each section fails on its own (``None`` plus an entry in ``errors``), so a
missing table or a bad row never hides the rest of the card.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from shared import decision_log, river_v2_evidence, shadow_policy, weekly_autonomy_report
from shared.models import Cluster
from shared.time import utc_now

logger = logging.getLogger(__name__)

OPE_WINDOW_DAYS = 90


def _section(session, name: str, errors: dict[str, str], build: Callable[[], Any]) -> Any:
    try:
        with session.begin_nested():
            return build()
    except Exception as exc:
        logger.warning("autonomy status: section %s unavailable", name, exc_info=True)
        errors[name] = type(exc).__name__
        return None


def _online_learning(session, cluster: Cluster, now: datetime) -> dict:
    report = river_v2_evidence.build_report(session, now=now.replace(tzinfo=timezone.utc))
    keys = {cluster.id, "__default__"} if cluster.is_default else {cluster.id}
    scopes = [scope for scope in report["scopes"] if scope.get("cluster_key") in keys]
    verdict = report.get("verdict") or {}
    return {
        "mode": report.get("execution_mode"),
        "enabled": report.get("online_learning_enabled"),
        "scopes": len(scopes),
        "verified": sum(int(scope.get("verified") or 0) for scope in scopes),
        "scored": sum(int(scope.get("scored") or 0) for scope in scopes),
        "decision": verdict.get("decision"),
        "reasons": list(verdict.get("reasons") or [])[:3],
    }


def _ope(session, now: datetime) -> dict:
    from scripts.ope_report import build as build_ope

    logged, unknown = decision_log.load(session, days=OPE_WINDOW_DAYS, now=now)
    report = build_ope(logged, unknown)
    return {
        "window_days": OPE_WINDOW_DAYS,
        "decisions_with_reward": report["decisions_with_reward"],
        "decisions_without_reward": report["decisions_without_reward"],
        "reward_mean": report["reward_mean"],
        "deterministic_logging": report["deterministic_logging"],
    }


def build(session, cluster: Cluster, *, now: datetime | None = None, period_days: int = 7) -> dict:
    now = now or utc_now()
    errors: dict[str, str] = {}
    weekly = _section(session, "weekly", errors, lambda: weekly_autonomy_report.build(
        session, cluster, now=now, period_days=period_days,
    ))
    if weekly:
        errors.update(weekly.get("errors") or {})
    return {
        "cluster": cluster.name,
        "period_days": period_days,
        "generated_at": now.isoformat(),
        "evidence": (weekly or {}).get("evidence"),
        "decisions": (weekly or {}).get("decisions"),
        "verdicts": (weekly or {}).get("verdicts"),
        "false_release_rate": _section(
            session, "false_release_rate", errors,
            lambda: shadow_policy.false_release_rate(session, cluster.id, now),
        ),
        "ope": _section(session, "ope", errors, lambda: _ope(session, now)),
        "online_learning": _section(session, "online_learning", errors, lambda: _online_learning(session, cluster, now)),
        "errors": errors,
    }
