"""FL6.1: which fault families keep ending in "not enough evidence"?

Plan/in-progress/failure-lab-plan-2026-10-06.md, FL6. On CS-LAB in the 30
days to 09/10/2026, log intelligence returned INSUFFICIENT_EVIDENCE 351
times against 89 findings (bluestore_slow_ops 40, pg_peering 9,
rgw_request 8, network_heartbeat 4, ...). Those families are where a lab
reproduction with a known cause would teach the most.

The queue ranks families by how often they lacked evidence, and says for
each whether an approved Failure Lab fault already reproduces it (a fault
scenario whose expected health codes belong to the family) and whether a
lab label exists yet. Read-only; nothing here injects anything.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from shared.models import Incident, IncidentTimelineEvent, LogFinding, RemediationCase
from shared.time import utc_now

LOW_CONFIDENCE = 0.5
LAB_LABEL_EVENT = "failure_lab_label"
_INSUFFICIENT_TEXT = ("chưa đủ bằng chứng", "insufficient evidence", "not enough evidence")


@dataclass
class EvidenceGap:
    family: str
    insufficient_log_findings: int = 0
    low_confidence_cases: int = 0
    last_seen: datetime | None = None
    examples: list[str] = field(default_factory=list)
    reproducible_by: list[str] = field(default_factory=list)
    lab_labels: int = 0

    @property
    def total(self) -> int:
        return self.insufficient_log_findings + self.low_confidence_cases

    @property
    def next_step(self) -> str:
        if self.lab_labels:
            return "đã có tái hiện trên lab"
        if self.reproducible_by:
            return "tái hiện được bằng kiểu lỗi hiện có: " + ", ".join(self.reproducible_by)
        return "cần thêm kiểu lỗi mới (đề xuất qua FL6.2, review code)"

    def as_dict(self) -> dict:
        return {"family": self.family, "total": self.total,
                "insufficient_log_findings": self.insufficient_log_findings,
                "low_confidence_cases": self.low_confidence_cases,
                "last_seen": self.last_seen.isoformat() if self.last_seen else None,
                "examples": self.examples, "reproducible_by": self.reproducible_by,
                "lab_labels": self.lab_labels, "next_step": self.next_step}


def family_of_code(code: str, family_codes: dict[str, tuple[str, ...]]) -> str | None:
    """The first log family whose health-code prefixes match ``code``."""
    for family, prefixes in family_codes.items():
        if any(code.startswith(prefix) for prefix in prefixes):
            return family
    return None


def low_confidence(case: RemediationCase) -> bool:
    text = (case.diagnosis or "").casefold()
    return (case.diagnosis_confidence is not None and case.diagnosis_confidence < LOW_CONFIDENCE) or any(
        marker in text for marker in _INSUFFICIENT_TEXT)


def _touch(gap: EvidenceGap, seen: datetime | None, example: str | None) -> None:
    if seen and (gap.last_seen is None or seen > gap.last_seen):
        gap.last_seen = seen
    if example and len(gap.examples) < 2 and example not in gap.examples:
        gap.examples.append(example[:160])


def evidence_gap_queue(session, *, family_codes: dict[str, tuple[str, ...]],
                       fault_codes: dict[str, set[str]], days: int = 30,
                       now: datetime | None = None) -> list[EvidenceGap]:
    """Families ranked by how often they lacked evidence in the last ``days``.

    ``family_codes``: log family -> health-code prefixes (watcher.incident_correlation).
    ``fault_codes``: Failure Lab fault id -> expected health codes (approved catalog).
    """
    since = (now or utc_now()) - timedelta(days=days)
    gaps: dict[str, EvidenceGap] = {}
    for finding in session.query(LogFinding).filter(LogFinding.verdict == "INSUFFICIENT_EVIDENCE",
                                                    LogFinding.created_at >= since):
        if not finding.fault_family:
            continue  # unclassified findings cannot be reproduced on purpose
        gap = gaps.setdefault(finding.fault_family, EvidenceGap(finding.fault_family))
        gap.insufficient_log_findings += 1
        _touch(gap, finding.created_at, finding.root_cause_hypothesis or finding.title)
    for case in session.query(RemediationCase).filter(RemediationCase.created_at >= since):
        family = family_of_code(case.fault_family or "", family_codes)
        if family is None or not low_confidence(case):
            continue
        gap = gaps.setdefault(family, EvidenceGap(family))
        gap.low_confidence_cases += 1
        _touch(gap, case.created_at, case.diagnosis)
    labelled = Counter(
        family_of_code(code or "", family_codes)
        for (code,) in session.query(Incident.ceph_code).join(
            IncidentTimelineEvent, IncidentTimelineEvent.incident_id == Incident.id,
        ).filter(IncidentTimelineEvent.event_type == LAB_LABEL_EVENT)
    )
    for gap in gaps.values():
        gap.lab_labels = labelled.get(gap.family, 0)
        gap.reproducible_by = sorted(
            fault_id for fault_id, codes in fault_codes.items()
            if any(family_of_code(code, family_codes) == gap.family for code in codes)
        )
    return sorted(gaps.values(), key=lambda gap: (-gap.total, gap.family))
