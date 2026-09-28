"""Weekly autonomy report (autonomy plan WP8).

Per cluster and read-only: the noise KPIs (WP0), new operator verdicts and
progress towards the 200 labels WP6 needs (WP2), evidence and rule triage
coverage (WP3), decision sources (WP6.1) and the playbooks closest to the
Trust Engine threshold. ``format_lines`` turns it into the short Telegram
section appended to the weekly AI Ops digest; the dict itself is the JSON
artifact.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import func, or_

from shared import autonomy_kpi
from shared.incident_evidence import EVENT_TRIAGED, SKIPPED_FLAPPING
from shared.models import (
    AutonomyDecision,
    Cluster,
    Incident,
    IncidentEvidence,
    IncidentTimelineEvent,
    PlaybookStat,
    RemediationCase,
)
from shared.time import utc_now
from shared.trust_engine import SHADOW_MIN_TRUST_SCORE, SHADOW_MIN_VERIFIED_SAMPLES

logger = logging.getLogger(__name__)

LABEL_TARGET = 200          # WP6 starts with at least this many operator labels
SCHEMA = "ceph-ai.weekly-autonomy-report.v1"


def _scope(column, cluster: Cluster):
    return or_(column == cluster.id, column.is_(None)) if cluster.is_default else column == cluster.id


def _section(session, name: str, errors: dict[str, str], build: Callable[[], Any]) -> Any:
    """One failing section must not suppress the whole weekly report; the
    rollback keeps a failed PostgreSQL transaction from breaking the next."""
    try:
        return build()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        logger.exception("weekly autonomy report: section %s failed", name)
        errors[name] = type(exc).__name__
        return None


def _noise(session, cluster: Cluster, days: int, now: datetime) -> dict:
    kpi = autonomy_kpi.collect(session, days=days, cluster_id=cluster.id,
                               include_unscoped=cluster.is_default, now=now)
    incidents, actions = kpi["incidents"], kpi["actions"]
    return {
        "incidents": incidents["total"],
        "top_families": list(incidents["by_family"].items())[:3],
        "reopen_rate": incidents["reopen_rate"],
        "investigate_manually_rate": actions["investigate_manually_rate"],
    }


def _verdicts(session, cluster: Cluster, start: datetime, now: datetime) -> dict:
    rows = session.query(RemediationCase.operator_verdict).filter(
        _scope(RemediationCase.cluster_id, cluster),
        RemediationCase.operator_verdict_at >= start, RemediationCase.operator_verdict_at <= now,
    ).all()
    total = session.query(func.count(RemediationCase.id)).filter(
        _scope(RemediationCase.cluster_id, cluster), RemediationCase.operator_verdict.isnot(None),
    ).scalar() or 0
    return {"new": dict(Counter(verdict for (verdict,) in rows)), "new_total": len(rows),
            "labelled_total": int(total), "label_target": LABEL_TARGET}


def _evidence(session, cluster: Cluster, start: datetime) -> dict:
    investigated = session.query(func.count(func.distinct(IncidentEvidence.incident_id))).join(
        Incident, Incident.id == IncidentEvidence.incident_id,
    ).filter(
        _scope(Incident.cluster_id, cluster), IncidentEvidence.created_at >= start,
        IncidentEvidence.status != SKIPPED_FLAPPING,
    ).scalar() or 0
    conclusions: Counter = Counter()
    for (raw,) in session.query(IncidentTimelineEvent.evidence_json).join(
        Incident, Incident.id == IncidentTimelineEvent.incident_id,
    ).filter(
        _scope(Incident.cluster_id, cluster), IncidentTimelineEvent.event_type == EVENT_TRIAGED,
        IncidentTimelineEvent.created_at >= start,
    ):
        try:
            conclusions[str(json.loads(raw or "{}").get("conclusion") or "UNKNOWN")] += 1
        except ValueError:
            conclusions["UNKNOWN"] += 1
    triaged = sum(conclusions.values())
    known = triaged - conclusions.get("UNKNOWN", 0)
    return {"incidents_investigated": int(investigated), "triaged": triaged, "rule_concluded": known,
            "rule_coverage": round(known / triaged, 3) if triaged else None,
            "conclusions": dict(conclusions.most_common(5))}


def _decisions(session, cluster: Cluster, start: datetime) -> dict:
    rows = session.query(AutonomyDecision.chosen_by).filter(
        _scope(AutonomyDecision.cluster_id, cluster), AutonomyDecision.created_at >= start,
    ).all()
    return dict(Counter(source for (source,) in rows))


def _playbooks(session, cluster: Cluster) -> dict:
    prefixes = [f"cluster={cluster.id}|%"] + (["cluster=legacy|%"] if cluster.is_default else [])
    rows = session.query(PlaybookStat).filter(
        or_(*(PlaybookStat.scope_key.like(prefix) for prefix in prefixes)),
        PlaybookStat.auto_disabled_reason.is_(None), PlaybookStat.verified_count > 0,
    ).all()
    ready = [row for row in rows if row.verified_count >= SHADOW_MIN_VERIFIED_SAMPLES
             and (row.trust_score or 0) >= SHADOW_MIN_TRUST_SCORE]
    closest = sorted((row for row in rows if row not in ready),
                     key=lambda row: (-row.verified_count, -(row.trust_score or 0)))[:3]

    def item(row: PlaybookStat) -> dict:
        return {"playbook": row.playbook_id, "verified": row.verified_count,
                "trust_score": round(row.trust_score or 0, 3), "maturity": row.maturity_level}

    return {"ready": [item(row) for row in ready], "closest": [item(row) for row in closest],
            "threshold": {"verified": SHADOW_MIN_VERIFIED_SAMPLES, "trust_score": SHADOW_MIN_TRUST_SCORE}}


def build(session, cluster: Cluster, *, now: datetime | None = None, period_days: int = 7) -> dict:
    now = now or utc_now()
    start = now - timedelta(days=period_days)
    errors: dict[str, str] = {}
    return {
        "schema": SCHEMA,
        "cluster": cluster.name,
        "period_start": start.isoformat(),
        "period_end": now.isoformat(),
        "noise": _section(session, "noise", errors, lambda: _noise(session, cluster, period_days, now)),
        "verdicts": _section(session, "verdicts", errors, lambda: _verdicts(session, cluster, start, now)),
        "evidence": _section(session, "evidence", errors, lambda: _evidence(session, cluster, start)),
        "decisions": _section(session, "decisions", errors, lambda: _decisions(session, cluster, start)),
        "playbooks": _section(session, "playbooks", errors, lambda: _playbooks(session, cluster)),
        "errors": errors,
    }


def _percent(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def format_lines(report: dict) -> list[str]:
    lines = ["🤖 Tự vận hành (autonomy):"]
    noise = report.get("noise")
    if noise:
        top = ", ".join(f"{family} {count}" for family, count in noise["top_families"]) or "—"
        lines.append(f"• Nhiễu: {noise['incidents']} incident (top: {top}); mở lại ≤30 phút "
                     f"{_percent(noise['reopen_rate'])}; investigate_manually {_percent(noise['investigate_manually_rate'])}")
    verdicts = report.get("verdicts")
    if verdicts:
        detail = ", ".join(f"{key} {value}" for key, value in sorted(verdicts["new"].items())) or "chưa có"
        lines.append(f"• Verdict mới: {verdicts['new_total']} ({detail}); tổng {verdicts['labelled_total']}/"
                     f"{verdicts['label_target']} nhãn cần cho WP6")
    evidence = report.get("evidence")
    if evidence:
        lines.append(f"• Bằng chứng: {evidence['incidents_investigated']} incident; luật kết luận "
                     f"{evidence['rule_concluded']}/{evidence['triaged']} ({_percent(evidence['rule_coverage'])})")
    decisions = report.get("decisions")
    if decisions:
        lines.append("• Nguồn quyết định: " + ", ".join(f"{key} {value}" for key, value in sorted(decisions.items())))
    playbooks = report.get("playbooks")
    if playbooks:
        if playbooks["ready"]:
            lines.append("• Đủ ngưỡng Trust Engine (chờ operator duyệt nâng quyền): "
                         + ", ".join(p["playbook"] for p in playbooks["ready"]))
        if playbooks["closest"]:
            threshold = playbooks["threshold"]
            lines.append("• Gần ngưỡng: " + ", ".join(
                f"{p['playbook']} {p['verified']}/{threshold['verified']} mẫu, trust {p['trust_score']:.2f}"
                for p in playbooks["closest"]))
    if report.get("errors"):
        lines.append("• Thiếu mục: " + ", ".join(sorted(report["errors"])))
    return lines
