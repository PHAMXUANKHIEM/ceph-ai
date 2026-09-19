import pytest

from shared.natural_language import (
    NaturalLanguageAnswerError,
    parse_provider_answer,
    render_provider_answer,
    validate_provider_answer,
)


def _payload(**overrides):
    value = {
        "schema_version": "answer-v1",
        "language": "vi",
        "cluster_id": "prod",
        "conclusion": "Cluster có HEALTH_WARN.",
        "observed": [{"code": "HEALTH_WARN", "status": "OBSERVED", "summary": "Đã quan sát."}],
        "evidence_refs": [{"tool_name": "get_health_detail"}],
        "inferences": [{"text": "Có thể liên quan health check.", "confidence": 0.7}],
        "next_checks": ["Mở health detail."],
        "recommendations": ["Kiểm tra dependency."],
        "citations": [{"source_id": "knowledge:runbook:0"}],
        "freshness": {"stale": False, "partial": False},
    }
    value.update(overrides)
    return value


def _report():
    return {
        "schema_version": "rca-v1",
        "observed": [{"code": "HEALTH_WARN", "status": "OBSERVED"}],
        "evidence_refs": [{"tool_name": "get_health_detail"}],
        "citations": [{"source_id": "knowledge:runbook:0"}],
    }


def test_provider_answer_schema_and_fact_validation_are_bounded():
    answer = parse_provider_answer(_payload())
    validated = validate_provider_answer(answer, _report(), cluster_id="prod")

    assert validated.validation_errors == ()
    assert "Kết luận: Cluster có HEALTH_WARN." in render_provider_answer(validated)


def test_provider_answer_rejects_unknown_fact_evidence_and_citation():
    answer = parse_provider_answer(_payload(
        observed=[{"code": "INVENTED", "status": "OBSERVED"}],
        evidence_refs=[{"tool_name": "run_ceph_command"}],
        citations=[{"source_id": "knowledge:not-collected"}],
    ))
    validated = validate_provider_answer(answer, _report(), cluster_id="prod")

    assert "observed_fact_not_collected:INVENTED" in validated.validation_errors
    assert "evidence_tool_not_collected:run_ceph_command" in validated.validation_errors
    assert "citation_not_collected:knowledge:not-collected" in validated.validation_errors
    assert "provider" in render_provider_answer(validated)


def test_provider_answer_rejects_invalid_schema_and_confidence():
    with pytest.raises(NaturalLanguageAnswerError, match="schema"):
        parse_provider_answer(_payload(schema_version="answer-v0"))
    with pytest.raises(NaturalLanguageAnswerError, match="confidence"):
        parse_provider_answer(_payload(inferences=[{"text": "x", "confidence": 2}]))
