"""Validated, read-only RCA report assembly for natural-language queries.

This module assembles deterministic findings and bounded evidence into a
provider-neutral response contract. It does not call an LLM, retrieve data,
execute commands, or invent recommendations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable, Mapping

from .query_executor import QueryExecutionResult
from .retrieval import KnowledgeCitation, RetrievalResult
from .schema import NaturalLanguageIntent
from .incident_bridge import IncidentCandidateSignal, build_incident_candidate_signals


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


@dataclass(frozen=True)
class RcaReport:
    """Safe structured RCA output; observed and inferred facts stay separate."""

    schema_version: str
    status: str
    intent: str
    language: str
    cluster_id: str | None
    conclusion: str
    observed: tuple[dict[str, Any], ...] = ()
    evidence_refs: tuple[dict[str, Any], ...] = ()
    inferences: tuple[str, ...] = ()
    next_checks: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    incident_candidates: tuple[dict[str, Any], ...] = ()
    citations: tuple[dict[str, Any], ...] = ()
    freshness: dict[str, Any] = field(default_factory=dict)
    validation_errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "observed", "evidence_refs", "inferences", "next_checks",
            "recommendations", "citations", "validation_errors",
            "incident_candidates",
        ):
            value[key] = list(value[key])
        return value


def _citation_dict(citation: KnowledgeCitation | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(citation, KnowledgeCitation):
        return citation.to_dict()
    if isinstance(citation, Mapping):
        return dict(citation)
    raise TypeError("citations must be KnowledgeCitation or mapping values")


def validate_citation_ids(
    citation_ids: Iterable[str],
    retrieval: RetrievalResult | None,
) -> tuple[dict[str, Any], ...]:
    """Return only citations present in retrieval output; reject unknown IDs."""

    requested = _dedupe(citation_ids)
    if not requested:
        return ()
    if retrieval is None:
        raise ValueError("citation IDs require retrieval evidence")
    available = {_citation_dict(hit)["source_id"]: _citation_dict(hit) for hit in retrieval.hits}
    missing = tuple(source_id for source_id in requested if source_id not in available)
    if missing:
        raise ValueError(f"citation not present in retrieval evidence: {', '.join(missing)}")
    return tuple(available[source_id] for source_id in requested)


def validate_read_only_rca(
    report: RcaReport,
    *,
    allowed_tool_names: Iterable[str] = (),
    allowed_citation_ids: Iterable[str] = (),
) -> RcaReport:
    """Validate report references before a provider or UI can consume them."""

    errors: list[str] = list(report.validation_errors)
    allowed_tools = {str(item) for item in allowed_tool_names}
    allowed_citations = {str(item) for item in allowed_citation_ids}
    if report.schema_version != "rca-v1":
        errors.append("unsupported_schema_version")
    for evidence_ref in report.evidence_refs:
        tool_name = str(evidence_ref.get("tool_name") or "")
        if tool_name not in allowed_tools:
            errors.append(f"evidence_tool_not_collected:{tool_name or 'missing'}")
    for citation in report.citations:
        source_id = str(citation.get("source_id") or "")
        if source_id not in allowed_citations:
            errors.append(f"citation_not_collected:{source_id or 'missing'}")
    for finding in report.observed:
        if str(finding.get("status") or "") != "OBSERVED":
            errors.append("observed_finding_has_invalid_status")
    if not errors:
        return report
    return replace(
        report,
        status="validation_failed",
        conclusion="Không thể xác thực report RCA; không dùng report này để kết luận.",
        validation_errors=_dedupe(errors),
    )


def build_read_only_rca(
    intent: NaturalLanguageIntent,
    execution: QueryExecutionResult | None,
    *,
    retrieval: RetrievalResult | None = None,
    citation_ids: Iterable[str] = (),
) -> RcaReport:
    """Assemble a report while failing closed on scope, clarification, or citations."""

    if intent.needs_clarification:
        return RcaReport(
            schema_version="rca-v1", status="clarification_required",
            intent=intent.intent, language=intent.language, cluster_id=intent.cluster_id,
            conclusion=intent.clarification_question or "Cần làm rõ phạm vi truy vấn.",
        )
    if execution is None:
        return RcaReport(
            schema_version="rca-v1", status="evidence_unavailable",
            intent=intent.intent, language=intent.language, cluster_id=intent.cluster_id,
            conclusion="Chưa có evidence để phân tích; không kết luận trạng thái Ceph.",
        )
    if intent.cluster_id != execution.cluster_id:
        return RcaReport(
            schema_version="rca-v1", status="scope_mismatch",
            intent=intent.intent, language=intent.language, cluster_id=intent.cluster_id,
            conclusion="Evidence không cùng cluster scope với yêu cầu; đã chặn tổng hợp.",
            validation_errors=("cluster_scope_mismatch",),
        )

    citations = validate_citation_ids(citation_ids, retrieval)
    observed = tuple(
        dict(finding) for finding in execution.findings
        if str(finding.get("status") or "") == "OBSERVED"
    )
    next_checks = _dedupe(
        check
        for finding in execution.findings
        for check in finding.get("next_checks", ())
    )
    recommendations = _dedupe(
        str(finding["recommended_action"])
        for finding in execution.findings
        if finding.get("recommended_action")
    )
    incident_candidates = tuple(
        signal.to_dict()
        for signal in build_incident_candidate_signals(
            observed, cluster_id=execution.cluster_id,
        )
    )
    evidence_refs = tuple(
        {
            "tool_name": item.tool_name,
            "status": item.status,
            "metadata": dict(item.metadata or {}),
        }
        for item in execution.evidence
        if item.status == "ok"
    )
    if observed:
        conclusion = observed[0].get("summary") or "Deterministic analyzer đã ghi nhận finding."
    elif execution.status in {"failed", "unsupported"}:
        conclusion = "Chưa đủ evidence để kết luận; cần kiểm tra trạng thái thu thập dữ liệu."
    else:
        conclusion = "Chưa phát hiện finding deterministic từ evidence hiện có."
    report = RcaReport(
        schema_version="rca-v1", status=execution.status,
        intent=intent.intent, language=intent.language, cluster_id=execution.cluster_id,
        conclusion=str(conclusion), observed=observed, evidence_refs=evidence_refs,
        next_checks=next_checks, recommendations=recommendations, citations=citations,
        incident_candidates=incident_candidates,
        freshness={
            "stale": execution.stale,
            "refreshing": execution.refreshing,
            "partial": execution.partial,
        },
    )
    return validate_read_only_rca(
        report,
        allowed_tool_names=(item.tool_name for item in execution.evidence),
        allowed_citation_ids=(item["source_id"] for item in citations),
    )


def render_read_only_rca(report: RcaReport) -> str:
    """Render a bounded provider-free answer from a validated RCA report.

    This is intentionally a presentation helper, not a second inference
    engine.  It only emits fields already validated by ``build_read_only_rca``
    and keeps observed facts, next checks, recommendations and citations
    visibly separate for operators.
    """

    if report.status == "clarification_required":
        return report.conclusion
    if report.status in {"scope_mismatch", "validation_failed"}:
        return report.conclusion

    vietnamese = report.language == "vi"
    labels = {
        "conclusion": "Kết luận" if vietnamese else "Conclusion",
        "observed": "Đã quan sát" if vietnamese else "Observed",
        "next_checks": "Cần kiểm tra tiếp" if vietnamese else "Next checks",
        "recommendations": "Khuyến nghị" if vietnamese else "Recommendations",
        "evidence": "Evidence" if vietnamese else "Evidence",
        "citations": "Tài liệu tham chiếu" if vietnamese else "References",
        "incident_candidates": "Tín hiệu Incident" if vietnamese else "Incident signals",
        "freshness": "Freshness" if vietnamese else "Freshness",
    }
    lines = [f"{labels['conclusion']}: {report.conclusion}"]
    if report.observed:
        lines.append(f"\n{labels['observed']}:")
        for finding in report.observed:
            severity = str(finding.get("severity") or "info").upper()
            code = str(finding.get("code") or "finding")
            summary = str(finding.get("summary") or "")
            lines.append(f"- [{severity}] {code}: {summary}".rstrip())
    if report.next_checks:
        lines.append(f"\n{labels['next_checks']}:")
        lines.extend(f"- {item}" for item in report.next_checks)
    if report.recommendations:
        lines.append(f"\n{labels['recommendations']}:")
        lines.extend(f"- {item}" for item in report.recommendations)
    if report.evidence_refs:
        tools = _dedupe(str(item.get("tool_name") or "") for item in report.evidence_refs)
        lines.append(f"\n{labels['evidence']}: {', '.join(tools)}")
    freshness = report.freshness
    if freshness:
        state = "stale" if freshness.get("stale") else "fresh"
        if freshness.get("partial"):
            state += ", partial"
        if freshness.get("refreshing"):
            state += ", refreshing"
        lines.append(f"{labels['freshness']}: {state}")
    if report.citations:
        lines.append(f"\n{labels['citations']}:")
        lines.extend(
            f"- [{item.get('source_id', 'unknown')}] {item.get('title', item.get('source', ''))}"
            for item in report.citations
        )
    if report.incident_candidates:
        lines.append(f"\n{labels['incident_candidates']}:")
        lines.extend(
            f"- [{item.get('severity', 'warning').upper()}] {item.get('ceph_code')}: "
            f"{item.get('summary', '')}" for item in report.incident_candidates
        )
    return "\n".join(lines).strip()
