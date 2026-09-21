"""Read-only bridge from deterministic NL findings to Incident intelligence.

Natural-language analysis must not create an Incident as a side effect of a
read-only chat request.  This module therefore emits bounded, deduplicated
candidate signals that the existing watcher/Incident pipeline can correlate
or persist explicitly.  It never creates ORM rows, commands, or approvals.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


_INCIDENT_SEVERITIES = frozenset({"critical", "warning"})


def _stable_dedupe_key(code: str, entities: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"code": code, "entities": dict(entities)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:64]


@dataclass(frozen=True)
class IncidentCandidateSignal:
    """A safe candidate signal consumable by existing incident intelligence."""

    schema_version: str
    source: str
    finding_id: str
    dedupe_key: str
    cluster_id: str | None
    ceph_code: str
    severity: str
    status: str
    summary: str
    entities: dict[str, Any] = field(default_factory=dict)
    evidence_gaps: tuple[str, ...] = ()
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence_gaps"] = list(self.evidence_gaps)
        return value


def build_incident_candidate_signals(
    findings: Iterable[Mapping[str, Any]],
    *,
    cluster_id: str | None,
) -> tuple[IncidentCandidateSignal, ...]:
    """Convert only actionable observed findings into stable candidate signals.

    ``INSUFFICIENT_EVIDENCE``, informational findings and stale findings are
    intentionally excluded from incident candidates.  A stale finding remains
    visible in the RCA itself, but must not open or reinforce an Incident.
    """

    result: list[IncidentCandidateSignal] = []
    seen: set[str] = set()
    for finding in findings:
        status = str(finding.get("status") or "").upper()
        severity = str(finding.get("severity") or "").lower()
        if status != "OBSERVED" or severity not in _INCIDENT_SEVERITIES:
            continue
        if bool(finding.get("stale")):
            continue
        code = str(finding.get("code") or "").strip()
        finding_id = str(finding.get("finding_id") or "").strip()
        summary = str(finding.get("summary") or "").strip()
        if not code or not finding_id or not summary:
            continue
        entities = finding.get("entities")
        safe_entities = dict(entities) if isinstance(entities, Mapping) else {}
        dedupe_key = _stable_dedupe_key(code, safe_entities)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        gaps = finding.get("evidence_gaps")
        evidence_gaps = tuple(
            str(item).strip() for item in (gaps or ()) if str(item).strip()
        ) if isinstance(gaps, (list, tuple)) else ()
        result.append(IncidentCandidateSignal(
            schema_version="incident-candidate-v1",
            source="natural_language_deterministic_analyzer",
            finding_id=finding_id,
            dedupe_key=dedupe_key,
            cluster_id=cluster_id,
            ceph_code=code,
            severity=severity,
            status="CANDIDATE",
            summary=summary,
            entities=safe_entities,
            evidence_gaps=evidence_gaps,
            stale=False,
        ))
    return tuple(result)

