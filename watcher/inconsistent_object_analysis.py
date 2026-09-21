"""Read-only inconsistent-object analysis.

This module classifies evidence already present in PG rows and optional health
payloads. It never calls pg repair, object fix, or any data-mutating command.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

MAX_FINDINGS = 200
MAX_OBJECT_SAMPLES = 10
TOKENS = {
    "critical": ("corrupt", "unfound", "missing", "lost"),
    "high": ("inconsistent", "scrub error", "digest mismatch", "repair"),
    "medium": ("scrub", "deep-scrub", "damaged"),
}


def _text(value: object) -> str:
    if isinstance(value, Mapping):
        return " ".join(_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(_text(item) for item in value)
    return str(value or "")


def _severity(text: str) -> str | None:
    lowered = text.casefold()
    for severity in ("critical", "high", "medium"):
        if any(token in lowered for token in TOKENS[severity]):
            return severity
    return None


def _masked_object_id(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value)
    if len(raw) <= 12:
        return raw
    return raw[:8] + "..." + raw[-4:]


def _row_signal_text(row: Mapping[str, object]) -> str:
    values = []
    for key in (
        "state",
        "status",
        "scrub",
        "deep_scrub",
        "last_scrub",
        "last_deep_scrub",
        "error",
        "errors",
        "inconsistent",
        "corrupt",
        "unfound",
        "missing",
        "object_errors",
    ):
        if key in row:
            values.append(_text(row.get(key)))
    values.extend(_row_objects(row))
    return " ".join(value for value in values if value)


def _row_objects(row: Mapping[str, object]) -> list[str]:
    candidates = []
    for key in ("inconsistent_objects", "objects", "object_errors", "errors"):
        value = row.get(key)
        if isinstance(value, list):
            candidates.extend(
                str(item.get("object") or item.get("object_id") or item)
                if isinstance(item, Mapping) else str(item)
                for item in value
            )
    return [sample for sample in candidates if sample][:MAX_OBJECT_SAMPLES]


def _finding(row: Mapping[str, object], severity: str, text: str) -> dict[str, Any]:
    pgid = str(row.get("pgid") or row.get("pg_id") or "unknown")
    return {
        "code": "INCONSISTENT_OBJECT_EVIDENCE",
        "severity": severity,
        "title": "Phát hiện evidence bất nhất hoặc corruption ở PG",
        "reason": text[:500],
        "target": {
            "type": "pg",
            "pgid": pgid,
            "pool": str(row.get("pool") or "unknown"),
        },
        "evidence": {
            "state": str(row.get("state") or "unknown"),
            "object_samples": [_masked_object_id(value) for value in _row_objects(row)],
        },
        "confidence": "high" if _row_objects(row) else "medium",
        "repair_classification": "DESTRUCTIVE/RISKY",
        "repair_allowed": False,
        "read_only": True,
        "action_id": None,
    }


def analyze_inconsistent_objects(
    *,
    cluster_id: str,
    cluster_name: str,
    pg_rows: list[dict[str, object]] | None,
    health_payload: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    rows = [row for row in (pg_rows or []) if isinstance(row, dict)]
    findings: list[dict[str, Any]] = []
    evidence_gaps: list[dict[str, str]] = []
    object_evidence_count = 0

    for row in rows[:MAX_FINDINGS]:
        row_text = _row_signal_text(row)
        severity = _severity(row_text)
        samples = _row_objects(row)
        object_evidence_count += len(samples)
        if severity:
            findings.append(_finding(row, severity, row_text))

    health = health_payload if isinstance(health_payload, Mapping) else {}
    health_text = _text({
        key: health.get(key)
        for key in ("status", "checks", "summary")
        if key in health
    })
    health_severity = _severity(health_text)
    if health_severity and not findings:
        findings.append(_finding({
            "pgid": "cluster-health",
            "pool": "unknown",
            "state": str(health.get("status") or "unknown"),
        }, health_severity, health_text))

    if not rows:
        evidence_gaps.append({
            "code": "PG_INCONSISTENCY_INVENTORY_UNAVAILABLE",
            "reason": "Chưa có PG evidence để phân tích inconsistency.",
        })
    if rows and object_evidence_count == 0:
        evidence_gaps.append({
            "code": "OBJECT_LEVEL_EVIDENCE_UNAVAILABLE",
            "reason": "Có PG state nhưng chưa có danh sách object/checksum mismatch cụ thể.",
        })
    if findings:
        evidence_gaps.append({
            "code": "REPAIR_REQUIRES_OPERATOR_REVIEW",
            "reason": "Repair/object fix luôn là DESTRUCTIVE/RISKY và đang bị khóa.",
        })
    if health_payload is None:
        evidence_gaps.append({
            "code": "CEPH_HEALTH_EVIDENCE_UNAVAILABLE",
            "reason": "Không có health payload để đối chiếu scrub/corruption signal.",
        })

    findings.sort(key=lambda item: (
        {"critical": 0, "high": 1, "medium": 2}.get(item["severity"], 9),
        item["target"]["pgid"],
    ))
    return {
        "cluster_id": cluster_id,
        "cluster_name": cluster_name,
        "status": "observed" if rows or health_payload else "not_available",
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
        "findings": findings[:MAX_FINDINGS],
        "summary": {
            "pgs_scanned": min(len(rows), MAX_FINDINGS),
            "finding_count": len(findings),
            "object_samples": object_evidence_count,
        },
        "evidence_gaps": evidence_gaps,
        "repair_policy": {
            "classification": "DESTRUCTIVE/RISKY",
            "automatic_repair": False,
            "operator_approval_required": True,
            "commands_included": False,
        },
        "limits": {
            "max_findings": MAX_FINDINGS,
            "max_object_samples_per_pg": MAX_OBJECT_SAMPLES,
        },
    }
