import pytest

from shared.natural_language import (
    KnowledgeStore,
    QueryExecutionResult,
    ToolEvidence,
    build_read_only_rca,
    render_read_only_rca,
    route_natural_language,
    validate_citation_ids,
    validate_read_only_rca,
)


def _execution(*, cluster_id="prod", findings=(), status="ok"):
    return QueryExecutionResult(
        plan_version="query-v1",
        status=status,
        cluster_id=cluster_id,
        evidence=(ToolEvidence(
            tool_name="get_cluster_status", status="ok", started_at="now",
            finished_at="now", duration_ms=1,
            result={"data": {"health": {"status": "HEALTH_WARN"}}, "meta": {"stale": False}},
            metadata={"stale": False},
        ),),
        stale=False,
        findings=tuple(findings),
    )


def test_rca_keeps_observed_findings_and_freshness_separate():
    intent = route_natural_language("Cụm đang HEALTH_WARN", cluster_id="prod")
    execution = _execution(findings=(
        {"code": "HEALTH_WARN", "status": "OBSERVED", "summary": "Cluster đang HEALTH_WARN.",
         "next_checks": ["Mở health detail."], "recommended_action": None},
    ))

    report = build_read_only_rca(intent, execution)

    assert report.status == "ok"
    assert report.conclusion == "Cluster đang HEALTH_WARN."
    assert report.observed[0]["code"] == "HEALTH_WARN"
    assert report.inferences == ()
    assert report.recommendations == ()
    assert report.freshness["stale"] is False


def test_render_read_only_rca_preserves_operator_sections_without_provider():
    intent = route_natural_language("Cụm đang HEALTH_WARN", cluster_id="prod")
    report = build_read_only_rca(
        intent,
        _execution(findings=(
            {"code": "HEALTH_WARN", "status": "OBSERVED", "severity": "warning",
             "summary": "Cluster đang HEALTH_WARN.",
             "next_checks": ["Mở health detail."],
             "recommended_action": "Kiểm tra các health check đang mở."},
        )),
    )

    rendered = render_read_only_rca(report)

    assert "Kết luận: Cluster đang HEALTH_WARN." in rendered
    assert "Đã quan sát:" in rendered
    assert "Cần kiểm tra tiếp:" in rendered
    assert "Khuyến nghị:" in rendered
    assert "get_cluster_status" in rendered


def test_rca_fails_closed_on_cluster_scope_mismatch():
    intent = route_natural_language("Cụm đang HEALTH_WARN", cluster_id="prod")

    report = build_read_only_rca(intent, _execution(cluster_id="other"))

    assert report.status == "scope_mismatch"
    assert report.observed == ()
    assert report.validation_errors == ("cluster_scope_mismatch",)


def test_clarification_does_not_consume_evidence():
    intent = route_natural_language("Kiểm tra OSD 1 và OSD 2", cluster_id="prod")

    report = build_read_only_rca(intent, _execution())

    assert report.status == "clarification_required"
    assert report.evidence_refs == ()
    assert report.observed == ()


def test_citation_validation_rejects_unknown_source():
    store = KnowledgeStore()
    store.ingest_text(
        document_id="runbook-1", source="docs/runbook.md", title="OSD runbook",
        content="# OSD\nCheck OSD_DOWN and health detail.", components=("osd",),
    )
    retrieval = store.retrieve("OSD_DOWN", component="osd")
    source_id = retrieval.hits[0].source_id

    assert validate_citation_ids([source_id], retrieval)[0]["source_id"] == source_id
    with pytest.raises(ValueError, match="citation not present"):
        validate_citation_ids(["knowledge:missing"], retrieval)


def test_rca_carries_only_retrieved_runbook_citations():
    store = KnowledgeStore()
    store.ingest_text(
        document_id="runbook-2", source="docs/runbook.md", title="Health runbook",
        content="# Health\nCheck HEALTH_WARN and health detail.", components=("health",),
    )
    retrieval = store.retrieve("HEALTH_WARN", component="health")
    intent = route_natural_language("Cụm đang HEALTH_WARN", cluster_id="prod")

    report = build_read_only_rca(
        intent,
        _execution(findings=()),
        retrieval=retrieval,
        citation_ids=[retrieval.hits[0].source_id],
    )

    assert [item["source_id"] for item in report.citations] == [retrieval.hits[0].source_id]


def test_rca_validation_blocks_uncollected_tools_and_citations():
    report = build_read_only_rca(
        route_natural_language("Cụm đang HEALTH_WARN", cluster_id="prod"),
        _execution(findings=()),
    )
    invalid = report.__class__(
        **{
            **report.to_dict(),
            "evidence_refs": ({"tool_name": "run_ceph_command", "status": "ok"},),
            "citations": ({"source_id": "knowledge:not-collected"},),
        }
    )

    validated = validate_read_only_rca(
        invalid,
        allowed_tool_names=("get_cluster_status",),
        allowed_citation_ids=(),
    )

    assert validated.status == "validation_failed"
    assert "evidence_tool_not_collected:run_ceph_command" in validated.validation_errors
    assert "citation_not_collected:knowledge:not-collected" in validated.validation_errors
