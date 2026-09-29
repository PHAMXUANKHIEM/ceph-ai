from pathlib import Path

import yaml  # type: ignore[import-untyped]
import pytest

from scripts.validate_architecture_graph import (
    _load_manifest,
    audit_paths,
    coverage_gaps,
    edge_review_gaps,
    validate_graph,
)


def _minimal_graph():
    return {
        "schema_version": 3,
        "edge_kinds": ["calls", "covered_by"],
        "edge_impact": {"calls": "both", "covered_by": "both"},
        "edge_reviews": [],
        "node_reviews": {},
        "nodes": {
            "api.read": {
                "kind": "route",
                "source": ["dashboard/routes/read.py"],
                "criticality": "high",
                "tests": ["tests/test_read.py"],
            },
            "ui.read": {
                "kind": "page",
                "source": ["dashboard/templates/read.html"],
                "criticality": "medium",
                "tests": [],
            },
        },
        "flows": {
            "flow.read": {
                "diagram": "dashboard-read",
                "criticality": "high",
                "nodes": ["api.read", "ui.read"],
                "tests": ["tests/test_read.py"],
            }
        },
        "edges": [["api.read", "ui.read", "calls"]],
        "deployment_variants": [],
        "coverage_rules": [],
    }


def test_repository_architecture_graph_has_valid_structure():
    manifest = Path("tests/architecture/graph.yaml")
    graph = yaml.safe_load(manifest.read_text(encoding="utf-8"))

    assert validate_graph(graph) == []


def test_graph_validator_rejects_dangling_edge_and_flow_nodes():
    graph = _minimal_graph()
    graph["edges"].append(["api.read", "api.missing", "calls"])
    graph["flows"]["flow.read"]["nodes"].append("component.missing")

    errors = validate_graph(graph)

    assert any("unknown target node 'api.missing'" in error for error in errors)
    assert any("unknown node 'component.missing'" in error for error in errors)


def test_graph_validator_rejects_unknown_edge_kind_and_bad_criticality():
    graph = _minimal_graph()
    graph["edges"][0][2] = "teleports"
    graph["nodes"]["api.read"]["criticality"] = "urgent"

    errors = validate_graph(graph)

    assert any("unknown edge kind 'teleports'" in error for error in errors)
    assert any("criticality: expected" in error for error in errors)


def test_graph_validator_requires_explicit_impact_policy_for_every_edge_kind():
    graph = _minimal_graph()
    graph["edge_impact"].pop("calls")
    graph["edge_impact"]["teleports"] = "both"
    graph["edge_impact"]["covered_by"] = []

    errors = validate_graph(graph)

    assert any("missing policy for edge kind 'calls'" in error for error in errors)
    assert any("unknown edge kind 'teleports'" in error for error in errors)
    assert any("edge_impact.covered_by: expected one of" in error for error in errors)


def test_coverage_gap_report_exposes_nodes_without_tests_or_validation():
    graph = _minimal_graph()

    assert coverage_gaps(graph) == [("ui.read", "medium")]

    graph["nodes"]["ui.read"]["validation"] = ["npm run build"]
    assert coverage_gaps(graph) == []


def test_node_validation_must_be_well_formed_and_referenced_by_a_coverage_rule():
    graph = _minimal_graph()
    graph["nodes"]["ui.read"]["validation"] = ["npm run build"]

    errors = validate_graph(graph)

    assert any("validation: check is not required by any coverage rule" in error for error in errors)

    graph["coverage_rules"] = [{
        "id": "rule.frontend",
        "when_nodes": ["ui.read"],
        "require_tests": [],
        "require_checks": ["npm run build"],
    }]
    assert validate_graph(graph) == []


def test_node_validation_rejects_multiline_or_duplicate_commands():
    graph = _minimal_graph()
    graph["nodes"]["ui.read"]["validation"] = ["npm run build", "npm run build"]
    errors = validate_graph(graph)
    assert any("nodes.ui.read.validation: duplicate entries" in error for error in errors)

    graph["nodes"]["ui.read"]["validation"] = ["npm run build\npytest"]
    errors = validate_graph(graph)
    assert any("single-line non-empty strings" in error for error in errors)


def test_edge_review_coverage_tracks_reviewed_and_unreviewed_relations():
    graph = _minimal_graph()
    assert edge_review_gaps(graph) == [("api.read", "ui.read", "calls")]

    graph["edge_reviews"] = [{
        "source": "api.read",
        "target": "ui.read",
        "kind": "calls",
        "owner_area": "api-ui-contract",
        "confidence": "high",
        "evidence": [{"path": "dashboard/routes/read.py", "symbol": "read", "claim": "API endpoint is called by this page flow."}],
    }]
    assert validate_graph(graph) == []
    assert edge_review_gaps(graph) == []


def test_edge_review_rejects_unknown_duplicate_and_incomplete_records():
    graph = _minimal_graph()
    review = {
        "source": "api.read",
        "target": "ui.read",
        "kind": "calls",
        "owner_area": "review-area",
        "confidence": "high",
        "evidence": [{"path": "dashboard/routes/read.py", "symbol": "read", "claim": "verified"}],
    }
    unknown = dict(review)
    unknown["target"] = "api.missing"
    graph["edge_reviews"] = [review, dict(review), unknown]
    errors = validate_graph(graph)

    assert any("duplicate edge review" in error for error in errors)
    assert any("edge is not declared in graph.edges" in error for error in errors)

    graph["edge_reviews"] = [{"source": "api.read", "target": "ui.read", "kind": "calls"}]
    errors = validate_graph(graph)
    assert any("reviewed edge requires at least one evidence item" in error for error in errors)


def test_edge_evidence_path_must_match_one_endpoint_source_mapping(tmp_path):
    graph = _minimal_graph()
    graph["edge_reviews"] = [{
        "source": "api.read",
        "target": "ui.read",
        "kind": "calls",
        "owner_area": "api-ui-contract",
        "confidence": "high",
        "evidence": [{"path": "tests/test_read.py", "symbol": "test_read", "claim": "test only; not endpoint evidence"}],
    }]
    (tmp_path / "dashboard/routes").mkdir(parents=True)
    (tmp_path / "dashboard/routes/read.py").write_text("def read(): pass\n", encoding="utf-8")
    (tmp_path / "dashboard/templates").mkdir(parents=True)
    (tmp_path / "dashboard/templates/read.html").write_text("<!-- page -->\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_read.py").write_text("def test_read(): pass\n", encoding="utf-8")
    (tmp_path / "docs/architecture").mkdir(parents=True)
    (tmp_path / "docs/architecture/overview.md").write_text(
        "```mermaid\n%% graph-flow: dashboard-read\n```\n", encoding="utf-8"
    )

    issues = audit_paths(graph, tmp_path)
    assert any("not covered by either endpoint source mapping" in issue for issue in issues)


def test_graph_validator_rejects_legacy_graph_schema_version():
    graph = _minimal_graph()
    graph["schema_version"] = 2

    errors = validate_graph(graph)

    assert "schema_version: expected 3" in errors


def test_graph_validator_checks_reviewed_node_owner_confidence_and_evidence():
    graph = _minimal_graph()
    graph["node_reviews"] = {
        "api.missing": {"owner_area": "bad owner", "confidence": "certain", "evidence": []},
        "ui.read": {"owner_area": "frontend", "confidence": "high", "evidence": []},
    }

    errors = validate_graph(graph)

    assert any("node_reviews.api.missing: unknown node" in error for error in errors)
    assert any("owner_area: expected a stable code-area identifier" in error for error in errors)
    assert any("confidence: expected high/medium/low" in error for error in errors)
    assert any("evidence: reviewed node requires" in error for error in errors)


def test_path_audit_requires_node_evidence_to_exist_and_match_declared_source(tmp_path):
    graph = _minimal_graph()
    graph["node_reviews"] = {
        "api.read": {
            "owner_area": "api",
            "confidence": "high",
            "evidence": [
                {"path": "dashboard/templates/read.html", "symbol": "read", "claim": "not in source map"}
            ],
        }
    }
    (tmp_path / "dashboard/routes").mkdir(parents=True)
    (tmp_path / "dashboard/routes/read.py").write_text("# route\n", encoding="utf-8")
    (tmp_path / "dashboard/templates").mkdir(parents=True)
    (tmp_path / "dashboard/templates/read.html").write_text("<!-- page -->\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_read.py").write_text("def test_read(): pass\n", encoding="utf-8")
    (tmp_path / "docs/architecture").mkdir(parents=True)
    (tmp_path / "docs/architecture/overview.md").write_text(
        "```mermaid\n%% graph-flow: dashboard-read\n```\n", encoding="utf-8"
    )

    issues = audit_paths(graph, tmp_path)

    assert any(
        "node_reviews.api.read.evidence[0].path: evidence path is not covered by nodes.api.read.source" in issue
        for issue in issues
    )


def test_path_audit_matches_evidence_against_recursive_source_glob(tmp_path):
    graph = _minimal_graph()
    graph["nodes"]["api.read"]["source"] = ["worker/policy/**"]
    graph["node_reviews"] = {
        "api.read": {
            "owner_area": "worker-policy",
            "confidence": "high",
            "evidence": [{"path": "worker/policy/gate.py", "symbol": "gate", "claim": "policy evidence"}],
        }
    }
    (tmp_path / "worker/policy").mkdir(parents=True)
    (tmp_path / "worker/policy/gate.py").write_text("def gate(): pass\n", encoding="utf-8")
    (tmp_path / "dashboard/templates").mkdir(parents=True)
    (tmp_path / "dashboard/templates/read.html").write_text("<!-- page -->\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_read.py").write_text("def test_read(): pass\n", encoding="utf-8")
    (tmp_path / "docs/architecture").mkdir(parents=True)
    (tmp_path / "docs/architecture/overview.md").write_text(
        "```mermaid\n%% graph-flow: dashboard-read\n```\n", encoding="utf-8"
    )

    issues = audit_paths(graph, tmp_path)

    assert not any("node_reviews.api.read.evidence" in issue for issue in issues)


def test_graph_validator_rejects_duplicate_edges_and_unhashable_edge_values():
    graph = _minimal_graph()
    graph["edges"].append(["api.read", "ui.read", "calls"])
    graph["edges"].append([{}, "ui.read", "calls"])

    errors = validate_graph(graph)

    assert any("duplicate edge" in error for error in errors)
    assert any("must be strings" in error for error in errors)


def test_graph_validator_checks_coverage_rule_ids_and_check_commands():
    graph = _minimal_graph()
    graph["coverage_rules"] = [
        {
            "id": "rule.read",
            "when_nodes": ["api.read"],
            "require_tests": ["tests/test_read.py"],
            "require_checks": ["npm run build"],
        },
        {
            "id": "rule.read",
            "when_nodes": ["api.read"],
            "require_tests": [],
            "require_checks": ["  "],
        },
    ]

    errors = validate_graph(graph)

    assert any("duplicate identifier 'rule.read'" in error for error in errors)
    assert any("require_checks: expected a list of non-empty strings" in error for error in errors)


def test_path_audit_checks_tests_required_by_coverage_rules(tmp_path):
    graph = _minimal_graph()
    graph["coverage_rules"] = [
        {
            "id": "rule.read",
            "when_nodes": ["api.read"],
            "require_tests": ["tests/test_required_by_rule.py"],
            "require_checks": [],
        }
    ]
    (tmp_path / "dashboard/routes").mkdir(parents=True)
    (tmp_path / "dashboard/routes/read.py").write_text("# route\n", encoding="utf-8")
    (tmp_path / "dashboard/templates").mkdir(parents=True)
    (tmp_path / "dashboard/templates/read.html").write_text("<!-- page -->\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_read.py").write_text("def test_read(): pass\n", encoding="utf-8")
    (tmp_path / "docs/architecture").mkdir(parents=True)
    (tmp_path / "docs/architecture/overview.md").write_text(
        "```mermaid\n%% graph-flow: dashboard-read\n```\n", encoding="utf-8"
    )

    issues = audit_paths(graph, tmp_path)

    assert issues == ["coverage_rules[0].require_tests: missing tests/test_required_by_rule.py"]


def test_validator_rejects_absolute_and_parent_traversal_paths(tmp_path):
    graph = _minimal_graph()
    graph["nodes"]["api.read"]["tests"] = ["../outside_test.py"]
    graph["nodes"]["ui.read"]["source"] = ["/tmp/external.py"]

    errors = validate_graph(graph)

    assert any("nodes.api.read.tests: path must be a repo-relative" in error for error in errors)
    assert any("nodes.ui.read.source: path must be a repo-relative" in error for error in errors)


def test_path_audit_rejects_symlink_that_resolves_outside_repository(tmp_path):
    graph = _minimal_graph()
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.py"
    (repo / "dashboard/routes").mkdir(parents=True)
    (repo / "dashboard/templates").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "docs/architecture").mkdir(parents=True)
    (repo / "dashboard/routes/read.py").write_text("# route\n", encoding="utf-8")
    (repo / "dashboard/templates/read.html").write_text("<!-- page -->\n", encoding="utf-8")
    (repo / "tests/test_read.py").write_text("def test_read(): pass\n", encoding="utf-8")
    (repo / "docs/architecture/overview.md").write_text(
        "```mermaid\n%% graph-flow: dashboard-read\n```\n", encoding="utf-8"
    )
    outside.write_text("# outside repo\n", encoding="utf-8")
    (repo / "linked.py").symlink_to(outside)
    graph["nodes"]["api.read"]["source"] = ["linked.py"]

    issues = audit_paths(graph, repo)

    assert any("nodes.api.read.source: path resolves outside repository: linked.py" in issue for issue in issues)


def test_manifest_loader_rejects_duplicate_yaml_keys(tmp_path):
    manifest = tmp_path / "duplicate.yaml"
    manifest.write_text("nodes:\n  api.read: {}\nnodes:\n  ui.read: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate key 'nodes'"):
        _load_manifest(manifest)


def test_manifest_loader_supports_yaml_merge_and_explicit_override(tmp_path):
    manifest = tmp_path / "merge.yaml"
    manifest.write_text(
        "defaults: &defaults\n"
        "  criticality: high\n"
        "first: &first\n"
        "  precedence: first\n"
        "second: &second\n"
        "  precedence: second\n"
        "merged:\n"
        "  <<: [*first, *second]\n"
        "  criticality: low\n",
        encoding="utf-8",
    )

    loaded = _load_manifest(manifest)

    assert loaded["merged"]["criticality"] == "low"
    assert loaded["merged"]["precedence"] == "first"


def test_manifest_loader_still_rejects_duplicate_explicit_key_with_merge(tmp_path):
    manifest = tmp_path / "merge-duplicate.yaml"
    manifest.write_text(
        "defaults: &defaults\n"
        "  criticality: high\n"
        "node:\n"
        "  <<: *defaults\n"
        "  kind: route\n"
        "  kind: page\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate key 'kind'"):
        _load_manifest(manifest)


def test_path_audit_reports_missing_code_and_test_evidence(tmp_path):
    graph = _minimal_graph()
    (tmp_path / "dashboard/routes").mkdir(parents=True)
    (tmp_path / "dashboard/routes/read.py").write_text("# route\n", encoding="utf-8")
    (tmp_path / "docs/architecture").mkdir(parents=True)
    (tmp_path / "docs/architecture/overview.md").write_text("```mermaid\n%% graph-flow: dashboard-read\n```\n", encoding="utf-8")

    issues = audit_paths(graph, tmp_path)

    assert len(issues) == 3
    assert any("dashboard/templates/read.html" in issue for issue in issues)
    assert sum("tests/test_read.py" in issue for issue in issues) == 2


def test_active_node_must_belong_to_a_flow():
    graph = _minimal_graph()
    graph["flows"]["flow.read"]["nodes"].remove("ui.read")

    errors = validate_graph(graph)

    assert any("nodes.ui.read: active architecture node is not assigned to a flow" in error for error in errors)


def test_path_audit_detects_mermaid_manifest_drift(tmp_path):
    graph = _minimal_graph()
    (tmp_path / "dashboard/routes").mkdir(parents=True)
    (tmp_path / "dashboard/routes/read.py").write_text("# route\n", encoding="utf-8")
    (tmp_path / "dashboard/templates").mkdir(parents=True)
    (tmp_path / "dashboard/templates/read.html").write_text("<!-- page -->\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_read.py").write_text("def test_read(): pass\n", encoding="utf-8")
    (tmp_path / "docs/architecture").mkdir(parents=True)
    (tmp_path / "docs/architecture/overview.md").write_text("```mermaid\n%% graph-flow: stale-flow\n```\n", encoding="utf-8")

    issues = audit_paths(graph, tmp_path)

    assert any("flows.flow.read.diagram" in issue for issue in issues)
    assert any("no manifest flow: stale-flow" in issue for issue in issues)
