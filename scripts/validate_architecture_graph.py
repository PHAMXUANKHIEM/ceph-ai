#!/usr/bin/env python3
"""Validate the architecture impact graph and audit its repository references."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]


DEFAULT_MANIFEST = Path("tests/architecture/graph.yaml")
DEFAULT_DOC = Path("docs/architecture/overview.md")
_NODE_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")


def _as_path_list(value: Any, where: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{where}: expected a list of strings")
        return []
    return value


_CRITICALITIES = {"critical", "high", "medium", "low"}
_UNFLOWED_STATUSES = {"not_implemented_as_shared_event", "not_served", "planned"}


def _validate_nodes(nodes: dict, errors: list[str]) -> None:
    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            errors.append(f"nodes.{node_id}: invalid stable node id")
        if not isinstance(node, dict):
            errors.append(f"nodes.{node_id}: expected a mapping")
            continue
        if not isinstance(node.get("kind"), str) or not node["kind"].strip():
            errors.append(f"nodes.{node_id}.kind: required non-empty string")
        if node.get("criticality") not in _CRITICALITIES:
            errors.append(f"nodes.{node_id}.criticality: expected critical/high/medium/low")
        _as_path_list(node.get("source"), f"nodes.{node_id}.source", errors)
        _as_path_list(node.get("tests"), f"nodes.{node_id}.tests", errors)


def _edge_kinds(graph: dict, errors: list[str]) -> list:
    edge_kinds = graph.get("edge_kinds")
    if not isinstance(edge_kinds, list) or any(not isinstance(kind, str) for kind in edge_kinds):
        errors.append("edge_kinds: expected a list of strings")
        return []
    if len(edge_kinds) != len(set(edge_kinds)):
        errors.append("edge_kinds: duplicate entries are not allowed")
    return edge_kinds


def _validate_edges(graph: dict, nodes: dict, errors: list[str]) -> None:
    edge_kinds = _edge_kinds(graph, errors)
    edges = graph.get("edges")
    if not isinstance(edges, list):
        errors.append("edges: expected a list")
        edges = []
    seen_edges: set[tuple[str, str, str]] = set()
    for index, edge in enumerate(edges):
        if not isinstance(edge, list) or len(edge) != 3:
            errors.append(f"edges[{index}]: expected [source, target, kind]")
            continue
        source, target, kind = edge
        if any(not isinstance(value, str) for value in edge):
            errors.append(f"edges[{index}]: source, target and kind must be strings")
            continue
        edge_key = (source, target, kind)
        if edge_key in seen_edges:
            errors.append(f"edges[{index}]: duplicate edge {edge_key!r}")
        seen_edges.add(edge_key)
        for label, value, known in (("source node", source, nodes), ("target node", target, nodes),
                                    ("edge kind", kind, edge_kinds)):
            if value not in known:
                errors.append(f"edges[{index}]: unknown {label} {value!r}")


def _unknown_refs(values: list[str], known: Any, where: str, noun: str, errors: list[str]) -> None:
    for value in values:
        if value not in known:
            errors.append(f"{where}: unknown {noun} {value!r}")


def _validate_flows(graph: dict, nodes: dict, errors: list[str]) -> dict:
    flows = graph.get("flows")
    if not isinstance(flows, dict):
        errors.append("flows: expected a mapping")
        return {}
    for flow_id, flow in flows.items():
        if not isinstance(flow_id, str) or not _NODE_ID.fullmatch(flow_id):
            errors.append(f"flows.{flow_id}: invalid stable flow id")
        if not isinstance(flow, dict):
            errors.append(f"flows.{flow_id}: expected a mapping")
            continue
        if flow.get("criticality") not in _CRITICALITIES:
            errors.append(f"flows.{flow_id}.criticality: expected critical/high/medium/low")
        if not isinstance(flow.get("diagram"), str) or not flow["diagram"].strip():
            errors.append(f"flows.{flow_id}.diagram: required Mermaid diagram id")
        _unknown_refs(_as_path_list(flow.get("nodes"), f"flows.{flow_id}.nodes", errors), nodes,
                      f"flows.{flow_id}.nodes", "node", errors)
        _as_path_list(flow.get("tests"), f"flows.{flow_id}.tests", errors)
    return flows


def _validate_flow_coverage(nodes: dict, flows: dict, errors: list[str]) -> None:
    covered_nodes = {
        node_id
        for flow in flows.values()
        if isinstance(flow, dict) and isinstance(flow.get("nodes"), list)
        for node_id in flow["nodes"]
        if isinstance(node_id, str)
    }
    for node_id, node in nodes.items():
        if node_id in covered_nodes or not isinstance(node, dict):
            continue
        if node.get("status") not in _UNFLOWED_STATUSES:
            errors.append(f"nodes.{node_id}: active architecture node is not assigned to a flow")


def _section_list(graph: dict, key: str, errors: list[str]) -> list:
    value = graph.get(key, [])
    if not isinstance(value, list):
        errors.append(f"{key}: expected a list")
        return []
    return value


def _stable_id(value: Any, where: str, seen: set[str], errors: list[str]) -> None:
    if not isinstance(value, str) or not _NODE_ID.fullmatch(value):
        errors.append(f"{where}.id: expected a stable identifier")
    elif value in seen:
        errors.append(f"{where}.id: duplicate identifier {value!r}")
    else:
        seen.add(value)


def _validate_variants(graph: dict, nodes: dict, errors: list[str]) -> None:
    variant_ids: set[str] = set()
    for index, variant in enumerate(_section_list(graph, "deployment_variants", errors)):
        where = f"deployment_variants[{index}]"
        if not isinstance(variant, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        _stable_id(variant.get("id"), where, variant_ids, errors)
        _as_path_list(variant.get("source"), f"{where}.source", errors)
        _unknown_refs(_as_path_list(variant.get("changes_nodes"), f"{where}.changes_nodes", errors), nodes,
                      f"{where}.changes_nodes", "node", errors)


def _validate_rules(graph: dict, nodes: dict, flows: dict, errors: list[str]) -> None:
    rule_ids: set[str] = set()
    for index, rule in enumerate(_section_list(graph, "coverage_rules", errors)):
        where = f"coverage_rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        _stable_id(rule.get("id"), where, rule_ids, errors)
        _unknown_refs(_as_path_list(rule.get("when_nodes"), f"{where}.when_nodes", errors), nodes,
                      f"{where}.when_nodes", "node", errors)
        _unknown_refs(_as_path_list(rule.get("include_flows"), f"{where}.include_flows", errors), flows,
                      f"{where}.include_flows", "flow", errors)
        _as_path_list(rule.get("require_tests"), f"{where}.require_tests", errors)
        checks = rule.get("require_checks", [])
        if not isinstance(checks, list) or any(not isinstance(check, str) or not check.strip() for check in checks):
            errors.append(f"{where}.require_checks: expected a list of non-empty strings")


def validate_graph(graph: Any) -> list[str]:
    """Return structural/schema errors without touching the filesystem."""
    errors: list[str] = []
    if not isinstance(graph, dict):
        return ["manifest: top level must be a mapping"]
    if graph.get("schema_version") != 1:
        errors.append("schema_version: expected 1")

    nodes = graph.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return errors + ["nodes: expected a non-empty mapping"]

    _validate_nodes(nodes, errors)
    _validate_edges(graph, nodes, errors)
    flows = _validate_flows(graph, nodes, errors)
    _validate_flow_coverage(nodes, flows, errors)
    _validate_variants(graph, nodes, errors)
    _validate_rules(graph, nodes, flows, errors)
    return errors


def _missing_files(paths: list[str], root: Path, where: str) -> list[str]:
    return [f"{where}: missing {path}" for path in paths if not (root / path).is_file()]


def _unmatched_globs(patterns: list[str], root: Path, where: str) -> list[str]:
    return [f"{where}: no match for {pattern}" for pattern in patterns if not any(root.glob(pattern))]


def _audit_repository_paths(graph: dict[str, Any], root: Path) -> list[str]:
    issues: list[str] = []
    for node_id, node in graph["nodes"].items():
        issues += _unmatched_globs(node.get("source", []), root, f"nodes.{node_id}.source")
        issues += _missing_files(node.get("tests", []), root, f"nodes.{node_id}.tests")
    for flow_id, flow in graph.get("flows", {}).items():
        issues += _missing_files(flow.get("tests", []), root, f"flows.{flow_id}.tests")
    for index, variant in enumerate(graph.get("deployment_variants", [])):
        issues += _unmatched_globs(variant.get("source", []), root, f"deployment_variants[{index}].source")
    for index, rule in enumerate(graph.get("coverage_rules", [])):
        issues += _missing_files(rule.get("require_tests", []), root, f"coverage_rules[{index}].require_tests")
    return issues


def _audit_diagram_markers(graph: dict[str, Any], root: Path) -> list[str]:
    doc_path = root / DEFAULT_DOC
    if not doc_path.is_file():
        return [f"docs: missing architecture overview {DEFAULT_DOC}"]
    doc = doc_path.read_text(encoding="utf-8")
    diagram_ids = set(re.findall(r"^\s*%%\s*graph-flow:\s*([a-z0-9_.-]+)\s*$", doc, re.MULTILINE))
    issues: list[str] = []
    for flow_id, flow in graph.get("flows", {}).items():
        diagram_id = flow.get("diagram") if isinstance(flow, dict) else None
        if diagram_id and diagram_id not in diagram_ids:
            issues.append(f"flows.{flow_id}.diagram: no Mermaid graph-flow marker for {diagram_id}")
    expected = {flow.get("diagram") for flow in graph.get("flows", {}).values() if isinstance(flow, dict)}
    for stale_id in sorted(diagram_ids - expected):
        issues.append(f"docs Mermaid graph-flow marker has no manifest flow: {stale_id}")
    return issues


def audit_paths(graph: dict[str, Any], root: Path) -> list[str]:
    """Report source globs and test files that no longer resolve in this checkout."""
    return _audit_repository_paths(graph, root) + _audit_diagram_markers(graph, root)


def _load_manifest(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root")
    parser.add_argument("--strict-paths", action="store_true", help="Return failure for stale source/test references")
    args = parser.parse_args(argv)

    try:
        graph = _load_manifest(args.root / args.manifest if not args.manifest.is_absolute() else args.manifest)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    errors = validate_graph(graph)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1

    print(f"Structure OK: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges, {len(graph.get('flows', {}))} flows")
    path_issues = audit_paths(graph, args.root)
    if path_issues:
        level = "ERROR" if args.strict_paths else "DRIFT"
        for issue in path_issues:
            print(f"{level}: {issue}")
        print(f"Path audit: {len(path_issues)} stale reference(s)")
        return 1 if args.strict_paths else 0
    print("Path audit OK: all source patterns and test files resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
