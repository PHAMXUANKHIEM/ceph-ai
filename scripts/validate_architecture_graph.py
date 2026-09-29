#!/usr/bin/env python3
"""Validate the architecture impact graph and audit its repository references."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

import yaml


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

    for node_id, node in nodes.items():
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            errors.append(f"nodes.{node_id}: invalid stable node id")
        if not isinstance(node, dict):
            errors.append(f"nodes.{node_id}: expected a mapping")
            continue
        if not isinstance(node.get("kind"), str) or not node["kind"].strip():
            errors.append(f"nodes.{node_id}.kind: required non-empty string")
        if node.get("criticality") not in {"critical", "high", "medium", "low"}:
            errors.append(f"nodes.{node_id}.criticality: expected critical/high/medium/low")
        _as_path_list(node.get("source"), f"nodes.{node_id}.source", errors)
        _as_path_list(node.get("tests"), f"nodes.{node_id}.tests", errors)

    edge_kinds = graph.get("edge_kinds")
    if not isinstance(edge_kinds, list) or any(not isinstance(kind, str) for kind in edge_kinds):
        errors.append("edge_kinds: expected a list of strings")
        edge_kinds = []
    edges = graph.get("edges")
    if not isinstance(edges, list):
        errors.append("edges: expected a list")
        edges = []
    for index, edge in enumerate(edges):
        if not isinstance(edge, list) or len(edge) != 3:
            errors.append(f"edges[{index}]: expected [source, target, kind]")
            continue
        source, target, kind = edge
        if source not in nodes:
            errors.append(f"edges[{index}]: unknown source node {source!r}")
        if target not in nodes:
            errors.append(f"edges[{index}]: unknown target node {target!r}")
        if kind not in edge_kinds:
            errors.append(f"edges[{index}]: unknown edge kind {kind!r}")

    flows = graph.get("flows")
    if not isinstance(flows, dict):
        errors.append("flows: expected a mapping")
        flows = {}
    for flow_id, flow in flows.items():
        if not isinstance(flow_id, str) or not _NODE_ID.fullmatch(flow_id):
            errors.append(f"flows.{flow_id}: invalid stable flow id")
        if not isinstance(flow, dict):
            errors.append(f"flows.{flow_id}: expected a mapping")
            continue
        if flow.get("criticality") not in {"critical", "high", "medium", "low"}:
            errors.append(f"flows.{flow_id}.criticality: expected critical/high/medium/low")
        if not isinstance(flow.get("diagram"), str) or not flow["diagram"].strip():
            errors.append(f"flows.{flow_id}.diagram: required Mermaid diagram id")
        for node_id in _as_path_list(flow.get("nodes"), f"flows.{flow_id}.nodes", errors):
            if node_id not in nodes:
                errors.append(f"flows.{flow_id}.nodes: unknown node {node_id!r}")
        _as_path_list(flow.get("tests"), f"flows.{flow_id}.tests", errors)

    if isinstance(nodes, dict) and isinstance(flows, dict):
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
            status = node.get("status")
            if status not in {"not_implemented_as_shared_event", "not_served", "planned"}:
                errors.append(f"nodes.{node_id}: active architecture node is not assigned to a flow")

    variants = graph.get("deployment_variants", [])
    if not isinstance(variants, list):
        errors.append("deployment_variants: expected a list")
        variants = []
    for index, variant in enumerate(variants):
        if not isinstance(variant, dict):
            errors.append(f"deployment_variants[{index}]: expected a mapping")
            continue
        for node_id in _as_path_list(variant.get("changes_nodes"), f"deployment_variants[{index}].changes_nodes", errors):
            if node_id not in nodes:
                errors.append(f"deployment_variants[{index}].changes_nodes: unknown node {node_id!r}")

    rules = graph.get("coverage_rules", [])
    if not isinstance(rules, list):
        errors.append("coverage_rules: expected a list")
        rules = []
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            errors.append(f"coverage_rules[{index}]: expected a mapping")
            continue
        for field in ("when_nodes",):
            for node_id in _as_path_list(rule.get(field), f"coverage_rules[{index}].{field}", errors):
                if node_id not in nodes:
                    errors.append(f"coverage_rules[{index}].{field}: unknown node {node_id!r}")
        for flow_id in _as_path_list(rule.get("include_flows"), f"coverage_rules[{index}].include_flows", errors):
            if flow_id not in flows:
                errors.append(f"coverage_rules[{index}].include_flows: unknown flow {flow_id!r}")
        for field in ("require_tests",):
            _as_path_list(rule.get(field), f"coverage_rules[{index}].{field}", errors)
    return errors


def audit_paths(graph: dict[str, Any], root: Path) -> list[str]:
    """Report source globs and test files that no longer resolve in this checkout."""
    issues: list[str] = []
    for node_id, node in graph["nodes"].items():
        for pattern in node.get("source", []):
            if not any(root.glob(pattern)):
                issues.append(f"nodes.{node_id}.source: no match for {pattern}")
        for test_path in node.get("tests", []):
            if not (root / test_path).is_file():
                issues.append(f"nodes.{node_id}.tests: missing {test_path}")
    for flow_id, flow in graph.get("flows", {}).items():
        for test_path in flow.get("tests", []):
            if not (root / test_path).is_file():
                issues.append(f"flows.{flow_id}.tests: missing {test_path}")
    for index, variant in enumerate(graph.get("deployment_variants", [])):
        for pattern in variant.get("source", []):
            if not any(root.glob(pattern)):
                issues.append(f"deployment_variants[{index}].source: no match for {pattern}")
    doc_path = root / DEFAULT_DOC
    if doc_path.is_file():
        doc = doc_path.read_text(encoding="utf-8")
        diagram_ids = set(re.findall(r"^\s*%%\s*graph-flow:\s*([a-z0-9_.-]+)\s*$", doc, re.MULTILINE))
        for flow_id, flow in graph.get("flows", {}).items():
            diagram_id = flow.get("diagram") if isinstance(flow, dict) else None
            if diagram_id and diagram_id not in diagram_ids:
                issues.append(f"flows.{flow_id}.diagram: no Mermaid graph-flow marker for {diagram_id}")
        expected = {flow.get("diagram") for flow in graph.get("flows", {}).values() if isinstance(flow, dict)}
        for stale_id in sorted(diagram_ids - expected):
            issues.append(f"docs Mermaid graph-flow marker has no manifest flow: {stale_id}")
    else:
        issues.append(f"docs: missing architecture overview {DEFAULT_DOC}")
    return issues


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
