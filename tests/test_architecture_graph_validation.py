from pathlib import Path

import yaml

from scripts.validate_architecture_graph import audit_paths, validate_graph


def _minimal_graph():
    return {
        "schema_version": 1,
        "edge_kinds": ["calls", "covered_by"],
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
