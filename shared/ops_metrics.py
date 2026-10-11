"""Operating experience, measured: per fault family on production clusters (croit-parity C6).

croit sells years of operating experience; Ceph AI has to show its own in
numbers. For every fault family (watcher.incident_correlation.FAMILY_CODES)
over a window, on clusters marked ``production``:

- incidents, still open, closed, and the share closed by Ceph AI (AUTO_FIXED);
- MTTR: median minutes from detection to closing. Incidents have no closing
  timestamp, so this uses ``updated_at`` of RESOLVED/AUTO_FIXED rows; the
  report says it is an estimate;
- diagnosis accuracy from operator verdicts (CORRECT vs FALSE_POSITIVE,
  UNSAFE, INEFFECTIVE), with how many were judged;
- fix success: remediation cases VERIFIED_SUCCESS vs VERIFIED_FAILED /
  EXECUTION_FAILED / REGRESSED.

Operator requests (pool creation, OSD replacement, deploys…) carry codes that
belong to no fault family and are left out. Read-only.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from shared.evidence_gaps import family_of_code
from shared.models import Cluster, Incident, RemediationCase
from shared.remediation_feedback import NEGATIVE_VERDICTS, POSITIVE_VERDICTS
from shared.time import utc_now

CLOSED = {"RESOLVED", "AUTO_FIXED"}
NOT_COUNTED = {"REJECTED"}
FIX_SUCCESS = {"VERIFIED_SUCCESS"}
FIX_FAILURE = {"VERIFIED_FAILED", "EXECUTION_FAILED", "REGRESSED"}


@dataclass
class FamilyOps:
    family: str
    incidents: int = 0
    open: int = 0
    closed: int = 0
    auto_fixed: int = 0
    repair_minutes: list[float] = field(default_factory=list)
    verdict_correct: int = 0
    verdict_wrong: int = 0
    fix_ok: int = 0
    fix_failed: int = 0

    @property
    def mttr_minutes(self) -> float | None:
        return round(statistics.median(self.repair_minutes), 1) if self.repair_minutes else None

    @staticmethod
    def _rate(good: int, bad: int) -> float | None:
        return round(good / (good + bad), 3) if good + bad else None

    def as_dict(self) -> dict:
        return {
            "family": self.family, "incidents": self.incidents, "open": self.open, "closed": self.closed,
            "auto_fixed": self.auto_fixed,
            "auto_fixed_share": round(self.auto_fixed / self.closed, 3) if self.closed else None,
            "mttr_minutes_estimate": self.mttr_minutes,
            "diagnosis_accuracy": self._rate(self.verdict_correct, self.verdict_wrong),
            "diagnoses_judged": self.verdict_correct + self.verdict_wrong,
            "fix_success": self._rate(self.fix_ok, self.fix_failed), "fixes_verified": self.fix_ok + self.fix_failed,
        }


def _production_cluster_ids(session) -> set[str | None]:
    ids: set[str | None] = {cluster.id for cluster in session.query(Cluster)
                            .filter(Cluster.autonomy_environment == "production").all()}
    default = session.query(Cluster).filter(Cluster.is_default.is_(True)).one_or_none()
    if default is None or default.autonomy_environment == "production":
        ids.add(None)  # incidents of the default cluster may carry no cluster id
    return ids


def ops_report(session, *, family_codes: dict[str, tuple[str, ...]], days: int = 30,
               now: datetime | None = None) -> list[FamilyOps]:
    """Fault families with incidents in the last ``days`` on production clusters, busiest first."""
    since = (now or utc_now()) - timedelta(days=days)
    production = _production_cluster_ids(session)
    families: dict[str, FamilyOps] = {}
    family_of_incident: dict[str, str] = {}
    for incident in session.query(Incident).filter(Incident.detected_at >= since,
                                                   Incident.status.notin_(NOT_COUNTED)):
        if incident.cluster_id not in production:
            continue
        family = family_of_code(incident.ceph_code or "", family_codes)
        if family is None:
            continue
        row = families.setdefault(family, FamilyOps(family))
        family_of_incident[incident.id] = family
        row.incidents += 1
        if incident.status in CLOSED:
            row.closed += 1
            row.auto_fixed += incident.status == "AUTO_FIXED"
            minutes = (incident.updated_at - incident.detected_at).total_seconds() / 60
            if minutes >= 0:
                row.repair_minutes.append(minutes)
        else:
            row.open += 1
    if family_of_incident:
        for case in session.query(RemediationCase).filter(RemediationCase.incident_id.in_(list(family_of_incident))):
            row = families[family_of_incident[case.incident_id]]
            row.verdict_correct += case.operator_verdict in POSITIVE_VERDICTS
            row.verdict_wrong += case.operator_verdict in NEGATIVE_VERDICTS
            row.fix_ok += case.outcome in FIX_SUCCESS
            row.fix_failed += case.outcome in FIX_FAILURE
    return sorted(families.values(), key=lambda row: (-row.incidents, row.family))
