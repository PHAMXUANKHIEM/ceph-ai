"""FL6.5: is the AI learning faster from the Failure Lab? Metrics per fault family.

Every real-fault run leaves an FL3 label (``failure_lab_label``) on the
incident it raised, with the six stage results and, since FL6.5, how many
seconds the diagnosis took. Per fault family this reports:

- runs, fully passed runs, and the pass rate of each stage;
- the run number of the first correct diagnosis, and the median seconds to a
  correct diagnosis;
- lab diagnosis accuracy on the first run of a family (no lab reference yet)
  against later runs (lab references from earlier runs available);
- on production clusters, the share of cases that ended "not enough
  evidence" or below 0.5 confidence before the family's first lab label and
  after it. That is the number the lab exists to lower; it is a correlation,
  not proof, so the report shows the counts behind each rate.

Read-only. ``family_summary_text`` is the Telegram line sent to the lab chat
after each run.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from shared.evidence_gaps import low_confidence, family_of_code
from shared.models import Cluster, Incident, IncidentTimelineEvent, RemediationCase

LABEL_EVENT = "failure_lab_label"
STAGES = ("detection", "diagnosis", "proposal", "recovery", "side_effects", "cleanup")


@dataclass
class LabRun:
    at: datetime
    passed: bool
    stages: dict[str, bool]
    diagnosis_seconds: int | None
    proposal_id: str | None


@dataclass
class FamilyLearning:
    family: str
    runs: list[LabRun] = field(default_factory=list)
    production_before: tuple[int, int] = (0, 0)  # (gaps, cases) before the first lab label
    production_after: tuple[int, int] = (0, 0)

    @property
    def passed(self) -> int:
        return sum(run.passed for run in self.runs)

    def stage_rate(self, stage: str) -> float | None:
        return _rate(sum(bool(run.stages.get(stage)) for run in self.runs), len(self.runs))

    @property
    def first_correct_run(self) -> int | None:
        """1-based number of the first run whose diagnosis was right."""
        return next((index for index, run in enumerate(self.runs, 1) if run.stages.get("diagnosis")), None)

    @property
    def median_correct_diagnosis_seconds(self) -> float | None:
        values = [run.diagnosis_seconds for run in self.runs
                  if run.stages.get("diagnosis") and run.diagnosis_seconds is not None]
        return statistics.median(values) if values else None

    @property
    def lab_accuracy(self) -> tuple[tuple[int, int], tuple[int, int]]:
        """((correct, runs) without a lab reference yet, (correct, runs) with one)."""
        first, later = self.runs[:1], self.runs[1:]
        return ((sum(bool(run.stages.get("diagnosis")) for run in first), len(first)),
                (sum(bool(run.stages.get("diagnosis")) for run in later), len(later)))

    def as_dict(self) -> dict:
        (before_ok, before_n), (after_ok, after_n) = self.lab_accuracy
        return {
            "family": self.family, "runs": len(self.runs), "passed": self.passed,
            "stage_rates": {stage: self.stage_rate(stage) for stage in STAGES},
            "first_correct_run": self.first_correct_run,
            "median_correct_diagnosis_seconds": self.median_correct_diagnosis_seconds,
            "lab_diagnosis_without_reference": {"correct": before_ok, "runs": before_n},
            "lab_diagnosis_with_reference": {"correct": after_ok, "runs": after_n},
            "production_gaps_before_lab": {"gaps": self.production_before[0], "cases": self.production_before[1]},
            "production_gaps_after_lab": {"gaps": self.production_after[0], "cases": self.production_after[1]},
        }


def _rate(hits: int, total: int) -> float | None:
    return round(hits / total, 3) if total else None


def _evidence(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _int_or_none(value: object) -> int | None:
    try:
        return int(value) if value is not None else None  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None


def _lab_runs(session, family_codes: dict[str, tuple[str, ...]]) -> dict[str, list[LabRun]]:
    runs: dict[str, list[LabRun]] = {}
    rows = session.query(IncidentTimelineEvent, Incident.ceph_code).join(
        Incident, Incident.id == IncidentTimelineEvent.incident_id,
    ).filter(IncidentTimelineEvent.event_type == LABEL_EVENT).order_by(IncidentTimelineEvent.created_at)
    for event, code in rows:
        family = family_of_code(code or "", family_codes)
        if family is None:
            continue
        evidence = _evidence(event.evidence_json)
        raw_stages = evidence.get("stages")
        stages: dict = raw_stages if isinstance(raw_stages, dict) else {}
        runs.setdefault(family, []).append(LabRun(
            at=event.created_at, passed=bool(evidence.get("passed")),
            stages={stage: bool(stages.get(stage)) for stage in STAGES},
            diagnosis_seconds=_int_or_none(evidence.get("diagnosis_seconds")),
            proposal_id=str(evidence["proposal_id"]) if evidence.get("proposal_id") else None,
        ))
    return runs


def _production_gaps(session, family_codes: dict[str, tuple[str, ...]],
                     first_label: dict[str, datetime]) -> dict[str, list[list[int]]]:
    """family -> [[gaps, cases] before, [gaps, cases] after] its first lab label; lab clusters excluded."""
    counts: dict[str, list[list[int]]] = {}
    cases = session.query(RemediationCase).outerjoin(Cluster, Cluster.id == RemediationCase.cluster_id).filter(
        (Cluster.id.is_(None)) | (Cluster.autonomy_environment != "lab"))
    for case in cases:
        family = family_of_code(case.fault_family or "", family_codes)
        if family not in first_label:
            continue
        side = counts.setdefault(family, [[0, 0], [0, 0]])[int(case.created_at >= first_label[family])]
        side[0] += int(low_confidence(case))
        side[1] += 1
    return counts


def learning_report(session, *, family_codes: dict[str, tuple[str, ...]]) -> list[FamilyLearning]:
    """Families with at least one lab run, most runs first."""
    runs = _lab_runs(session, family_codes)
    gaps = _production_gaps(session, family_codes, {family: items[0].at for family, items in runs.items()})
    report = []
    for family, items in runs.items():
        before, after = gaps.get(family, [[0, 0], [0, 0]])
        report.append(FamilyLearning(family, items, (before[0], before[1]), (after[0], after[1])))
    return sorted(report, key=lambda item: (-len(item.runs), item.family))


def _percent(hits: int, total: int) -> str:
    return f"{round(100 * hits / total)}% ({hits}/{total})" if total else "chưa có"


def family_summary_text(item: FamilyLearning) -> str:
    """The lab-chat line after a run: how this family is learning so far."""
    (before_ok, before_n), (after_ok, after_n) = item.lab_accuracy
    seconds = item.median_correct_diagnosis_seconds
    return "\n".join([
        f"📈 Học họ lỗi {item.family}: {len(item.runs)} lượt, {item.passed} đạt đủ 6 khâu.",
        "Chẩn đoán đúng lần đầu ở lượt: " + (str(item.first_correct_run) if item.first_correct_run else "chưa có"),
        "Thời gian tới chẩn đoán đúng (trung vị): " + (f"{seconds:.0f} s" if seconds is not None else "chưa có"),
        f"Chẩn đoán lab đúng: lượt đầu {_percent(before_ok, before_n)} · có tham khảo lab {_percent(after_ok, after_n)}",
        f"Production thiếu bằng chứng: trước lab {_percent(*item.production_before)} · "
        f"sau lab {_percent(*item.production_after)}",
    ])
