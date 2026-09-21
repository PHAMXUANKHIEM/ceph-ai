"""Strict provider-output contract for natural-language RCA answers.

The model may propose prose, but when structured mode is enabled the server
accepts only this schema and only facts already present in the deterministic
RCA report. Unknown evidence/citation identifiers fail closed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping


class NaturalLanguageAnswerError(ValueError):
    """Raised when provider output cannot be trusted as an RCA answer."""


def _strings(value: object, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise NaturalLanguageAnswerError(f"{field_name} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise NaturalLanguageAnswerError(f"{field_name} contains an invalid item")
        result.append(item.strip())
    return tuple(result)


@dataclass(frozen=True)
class NaturalLanguageAnswer:
    schema_version: str
    language: str
    cluster_id: str | None
    conclusion: str
    observed: tuple[dict[str, Any], ...] = ()
    evidence_refs: tuple[dict[str, Any], ...] = ()
    inferences: tuple[dict[str, Any], ...] = ()
    next_checks: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    citations: tuple[dict[str, Any], ...] = ()
    validation_errors: tuple[str, ...] = ()
    freshness: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        for key in (
            "observed", "evidence_refs", "inferences", "next_checks",
            "recommendations", "citations", "validation_errors",
        ):
            value[key] = list(value[key])
        return value


def parse_provider_answer(payload: object) -> NaturalLanguageAnswer:
    """Parse a JSON object or fenced JSON emitted by a provider."""

    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise NaturalLanguageAnswerError("provider output is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise NaturalLanguageAnswerError("provider output must be an object")
    schema_version = str(payload.get("schema_version") or "")
    conclusion = str(payload.get("conclusion") or "").strip()
    if schema_version != "answer-v1":
        raise NaturalLanguageAnswerError("unsupported answer schema version")
    if not conclusion:
        raise NaturalLanguageAnswerError("answer conclusion is required")
    observed = payload.get("observed") or []
    evidence_refs = payload.get("evidence_refs") or []
    citations = payload.get("citations") or []
    inferences = payload.get("inferences") or []
    for name, value in (("observed", observed), ("evidence_refs", evidence_refs),
                        ("citations", citations), ("inferences", inferences)):
        if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
            raise NaturalLanguageAnswerError(f"{name} must be a list of objects")
    normalized_inferences: list[dict[str, Any]] = []
    for item in inferences:
        text = str(item.get("text") or "").strip()
        confidence = item.get("confidence")
        if not text or isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise NaturalLanguageAnswerError("inference requires text and numeric confidence")
        if not 0 <= float(confidence) <= 1:
            raise NaturalLanguageAnswerError("inference confidence must be between 0 and 1")
        normalized_inferences.append({"text": text, "confidence": float(confidence)})
    freshness = payload.get("freshness") or {}
    if not isinstance(freshness, Mapping):
        raise NaturalLanguageAnswerError("freshness must be an object")
    return NaturalLanguageAnswer(
        schema_version=schema_version,
        language=str(payload.get("language") or "unknown"),
        cluster_id=(str(payload["cluster_id"]) if payload.get("cluster_id") is not None else None),
        conclusion=conclusion,
        observed=tuple(dict(item) for item in observed),
        evidence_refs=tuple(dict(item) for item in evidence_refs),
        inferences=tuple(normalized_inferences),
        next_checks=_strings(payload.get("next_checks"), field_name="next_checks"),
        recommendations=_strings(payload.get("recommendations"), field_name="recommendations"),
        citations=tuple(dict(item) for item in citations),
        freshness=dict(freshness),
    )


def validate_provider_answer(
    answer: NaturalLanguageAnswer,
    deterministic_report: Mapping[str, Any],
    *,
    cluster_id: str | None,
) -> NaturalLanguageAnswer:
    """Allow only facts/citations collected by the deterministic report."""

    errors: list[str] = []
    if answer.cluster_id != cluster_id:
        errors.append("cluster_scope_mismatch")
    if str(deterministic_report.get("schema_version") or "") != "rca-v1":
        errors.append("deterministic_report_schema_invalid")
    observed = deterministic_report.get("observed") or []
    allowed_codes = {str(item.get("code") or "") for item in observed if isinstance(item, Mapping)}
    for item in answer.observed:
        code = str(item.get("code") or "")
        if code not in allowed_codes:
            errors.append(f"observed_fact_not_collected:{code or 'missing'}")
        if str(item.get("status") or "") != "OBSERVED":
            errors.append("provider_observed_status_invalid")
    allowed_tools = {
        str(item.get("tool_name") or "")
        for item in deterministic_report.get("evidence_refs", ())
        if isinstance(item, Mapping)
    }
    for item in answer.evidence_refs:
        tool_name = str(item.get("tool_name") or "")
        if tool_name not in allowed_tools:
            errors.append(f"evidence_tool_not_collected:{tool_name or 'missing'}")
    allowed_citations = {
        str(item.get("source_id") or "")
        for item in deterministic_report.get("citations", ())
        if isinstance(item, Mapping)
    }
    for item in answer.citations:
        source_id = str(item.get("source_id") or "")
        if source_id not in allowed_citations:
            errors.append(f"citation_not_collected:{source_id or 'missing'}")
    if errors:
        return NaturalLanguageAnswer(
            **{
                **answer.to_dict(),
                "validation_errors": tuple(dict.fromkeys(errors)),
            }
        )
    return answer


def render_provider_answer(answer: NaturalLanguageAnswer) -> str:
    """Render only validated structured fields into operator-facing prose."""
    if answer.validation_errors:
        return "Không thể xác thực câu trả lời của provider; chỉ dùng deterministic RCA."
    vi = answer.language == "vi"
    labels = (
        ("Kết luận", "Conclusion"), ("Đã quan sát", "Observed"),
        ("Suy luận", "Inferences"), ("Cần kiểm tra tiếp", "Next checks"),
        ("Khuyến nghị", "Recommendations"), ("Evidence", "Evidence"),
        ("Tài liệu tham chiếu", "References"),
    )
    label = {key: (vn if vi else en) for (vn, en), key in zip(labels, (
        "conclusion", "observed", "inferences", "next_checks",
        "recommendations", "evidence", "citations",
    ))}
    lines = [f"{label['conclusion']}: {answer.conclusion}"]
    if answer.observed:
        lines.append(f"\n{label['observed']}:")
        lines.extend(
            f"- {item.get('code', 'finding')}: {item.get('summary', '')}" for item in answer.observed
        )
    if answer.inferences:
        lines.append(f"\n{label['inferences']}:")
        lines.extend(
            f"- {item['text']} (confidence {item['confidence']:.2f})"
            for item in answer.inferences
        )
    if answer.next_checks:
        lines.append(f"\n{label['next_checks']}:")
        lines.extend(f"- {item}" for item in answer.next_checks)
    if answer.recommendations:
        lines.append(f"\n{label['recommendations']}:")
        lines.extend(f"- {item}" for item in answer.recommendations)
    if answer.evidence_refs:
        lines.append(f"\n{label['evidence']}: " + ", ".join(
            str(item.get("tool_name")) for item in answer.evidence_refs
        ))
    if answer.citations:
        lines.append(f"\n{label['citations']}: " + ", ".join(
            str(item.get("source_id")) for item in answer.citations
        ))
    return "\n".join(lines)
