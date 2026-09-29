#!/usr/bin/env python3
"""Validate the architecture impact graph and audit its repository references."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Generator

import yaml  # type: ignore[import-untyped]


DEFAULT_MANIFEST = Path("tests/architecture/graph.yaml")
DEFAULT_DOC = Path("docs/architecture/overview.md")
_NODE_ID = re.compile(r"^[a-z][a-z0-9_.-]*$")
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:")


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> Generator[dict[Any, Any], None, dict[Any, Any]]:
    mapping: dict[Any, Any] = {}
    yield mapping
    explicit_key_nodes = {
        id(key_node)
        for key_node, _ in node.value
        if key_node.tag != "tag:yaml.org,2002:merge"
    }
    # Expand YAML `<<` merges before checking duplicates. Merge-derived keys may
    # be overridden by explicit keys, as defined by YAML; only repeated explicit
    # keys in this mapping are rejected.
    loader.flatten_mapping(node)
    explicit_keys: set[Any] = set()
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in explicit_keys
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                "found an unhashable key", key_node.start_mark,
            ) from exc
        is_explicit = id(key_node) in explicit_key_nodes
        if is_explicit and duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping", node.start_mark,
                f"found duplicate key {key!r}", key_node.start_mark,
            )
        value = loader.construct_object(value_node, deep=deep)
        mapping[key] = value
        if is_explicit:
            explicit_keys.add(key)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def _is_safe_repo_path(value: str) -> bool:
    """Accept only non-empty POSIX paths/globs confined lexically to the repo."""
    if not value or "\\" in value or _WINDOWS_DRIVE.match(value):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts


def _repo_path_list(value: Any, where: str, errors: list[str]) -> list[str]:
    paths = _as_path_list(value, where, errors)
    for path in paths:
        if not _is_safe_repo_path(path):
            errors.append(f"{where}: path must be a repo-relative path without '..': {path!r}")
    return paths


def _as_path_list(value: Any, where: str, errors: list[str]) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        errors.append(f"{where}: expected a list of strings")
        return []
    if len(value) != len(set(value)):
        errors.append(f"{where}: duplicate entries are not allowed")
    return value


_CRITICALITIES = {"critical", "high", "medium", "low"}
_REVIEW_CONFIDENCES = {"high", "medium", "low"}
_UNFLOWED_STATUSES = {"not_implemented_as_shared_event", "not_served", "planned"}
_EDGE_IMPACT_POLICIES = {"from_to", "to_from", "both", "full_suite"}


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
        _repo_path_list(node.get("source"), f"nodes.{node_id}.source", errors)
        _repo_path_list(node.get("tests"), f"nodes.{node_id}.tests", errors)
        validations = node.get("validation", [])
        if not isinstance(validations, list) or any(
            not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item
            for item in validations
        ):
            errors.append(f"nodes.{node_id}.validation: expected a list of single-line non-empty strings")
        elif len(validations) != len(set(validations)):
            errors.append(f"nodes.{node_id}.validation: duplicate entries are not allowed")


def _validate_node_reviews(graph: dict, nodes: dict, errors: list[str]) -> None:
    reviews = graph.get("node_reviews")
    if not isinstance(reviews, dict):
        errors.append("node_reviews: expected a mapping; unreviewed nodes may be omitted")
        return
    for node_id, review in reviews.items():
        where = f"node_reviews.{node_id}"
        if node_id not in nodes:
            errors.append(f"{where}: unknown node")
        if not isinstance(review, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        owner_area = review.get("owner_area")
        if not isinstance(owner_area, str) or not _NODE_ID.fullmatch(owner_area):
            errors.append(f"{where}.owner_area: expected a stable code-area identifier")
        if review.get("confidence") not in _REVIEW_CONFIDENCES:
            errors.append(f"{where}.confidence: expected high/medium/low")
        evidence = review.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{where}.evidence: reviewed node requires at least one evidence item")
            continue
        for index, item in enumerate(evidence):
            _validate_review_evidence(item, f"{where}.evidence[{index}]", errors)


def _validate_review_evidence(item: Any, item_where: str, errors: list[str]) -> None:
    if not isinstance(item, dict):
        errors.append(f"{item_where}: expected a mapping")
        return
    path = item.get("path")
    if not isinstance(path, str) or not _is_safe_repo_path(path):
        errors.append(f"{item_where}.path: expected a safe repo-relative path")
    for field in ("symbol", "claim"):
        value = item.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{item_where}.{field}: required non-empty string")


def _edge_kinds(graph: dict, errors: list[str]) -> list:
    edge_kinds = graph.get("edge_kinds")
    if not isinstance(edge_kinds, list) or any(not isinstance(kind, str) for kind in edge_kinds):
        errors.append("edge_kinds: expected a list of strings")
        return []
    if len(edge_kinds) != len(set(edge_kinds)):
        errors.append("edge_kinds: duplicate entries are not allowed")
    return edge_kinds


def _validate_edge_impact(graph: dict, edge_kinds: list[str], errors: list[str]) -> None:
    policies = graph.get("edge_impact")
    if not isinstance(policies, dict):
        errors.append("edge_impact: expected a mapping covering every edge kind")
        return
    declared = set(policies)
    expected = set(edge_kinds)
    for missing in sorted(expected - declared):
        errors.append(f"edge_impact: missing policy for edge kind {missing!r}")
    for unknown in sorted(declared - expected, key=str):
        errors.append(f"edge_impact: unknown edge kind {unknown!r}")
    for kind, policy in policies.items():
        if kind in expected and (
            not isinstance(policy, str) or policy not in _EDGE_IMPACT_POLICIES
        ):
            errors.append(
                f"edge_impact.{kind}: expected one of {', '.join(sorted(_EDGE_IMPACT_POLICIES))}"
            )


def _validate_edges(graph: dict, nodes: dict, errors: list[str]) -> None:
    edge_kinds = _edge_kinds(graph, errors)
    _validate_edge_impact(graph, edge_kinds, errors)
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


def _validate_edge_reviews(graph: dict, nodes: dict, errors: list[str]) -> None:
    reviews = graph.get("edge_reviews", [])
    if not isinstance(reviews, list):
        errors.append("edge_reviews: expected a list")
        return
    declared_edges = {
        tuple(edge)
        for edge in graph.get("edges", [])
        if isinstance(edge, list) and len(edge) == 3 and all(isinstance(value, str) for value in edge)
    }
    seen_edges: set[tuple[str, str, str]] = set()
    for index, review in enumerate(reviews):
        where = f"edge_reviews[{index}]"
        if not isinstance(review, dict):
            errors.append(f"{where}: expected a mapping")
            continue
        edge = tuple(review.get(field) for field in ("source", "target", "kind"))
        if any(not isinstance(value, str) for value in edge):
            errors.append(f"{where}: source, target and kind must be strings")
            continue
        if edge in seen_edges:
            errors.append(f"{where}: duplicate edge review {edge!r}")
        seen_edges.add(edge)
        if edge not in declared_edges:
            errors.append(f"{where}: edge is not declared in graph.edges: {edge!r}")
        owner_area = review.get("owner_area")
        if not isinstance(owner_area, str) or not _NODE_ID.fullmatch(owner_area):
            errors.append(f"{where}.owner_area: expected a stable code-area identifier")
        if review.get("confidence") not in _REVIEW_CONFIDENCES:
            errors.append(f"{where}.confidence: expected high/medium/low")
        evidence = review.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            errors.append(f"{where}.evidence: reviewed edge requires at least one evidence item")
            continue
        for evidence_index, item in enumerate(evidence):
            _validate_review_evidence(item, f"{where}.evidence[{evidence_index}]", errors)


def edge_review_gaps(graph: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Return declared edges that do not yet have code evidence review."""
    reviewed = {
        (item.get("source"), item.get("target"), item.get("kind"))
        for item in graph.get("edge_reviews", [])
        if isinstance(item, dict)
    }
    return sorted(
        tuple(edge)
        for edge in graph.get("edges", [])
        if isinstance(edge, list) and len(edge) == 3 and tuple(edge) not in reviewed
    )


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
        _repo_path_list(flow.get("tests"), f"flows.{flow_id}.tests", errors)
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
        _repo_path_list(variant.get("source"), f"{where}.source", errors)
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
        _repo_path_list(rule.get("require_tests"), f"{where}.require_tests", errors)
        checks = rule.get("require_checks", [])
        if not isinstance(checks, list) or any(not isinstance(check, str) or not check.strip() for check in checks):
            errors.append(f"{where}.require_checks: expected a list of non-empty strings")


def _validate_validation_coverage(nodes: dict, graph: dict, errors: list[str]) -> None:
    """Require every non-test validation command to be selected by a coverage rule."""
    rules = graph.get("coverage_rules", [])
    if not isinstance(rules, list):
        return
    declared_checks = {
        check
        for rule in rules
        if isinstance(rule, dict) and isinstance(rule.get("require_checks", []), list)
        for check in rule.get("require_checks", [])
        if isinstance(check, str)
    }
    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            continue
        validations = node.get("validation", [])
        if not isinstance(validations, list):
            continue
        for check in validations:
            if isinstance(check, str) and check not in declared_checks:
                errors.append(
                    f"nodes.{node_id}.validation: check is not required by any coverage rule: {check!r}"
                )


def coverage_gaps(graph: dict[str, Any]) -> list[tuple[str, str]]:
    """Return active nodes with neither test files nor a declared validation check."""
    gaps = []
    for node_id, node in graph.get("nodes", {}).items():
        if not isinstance(node, dict) or node.get("status") in _UNFLOWED_STATUSES:
            continue
        if node.get("tests") or node.get("validation"):
            continue
        gaps.append((str(node_id), str(node.get("criticality", "unknown"))))
    return sorted(gaps)


def validate_graph(graph: Any) -> list[str]:
    """Return structural/schema errors without touching the filesystem."""
    errors: list[str] = []
    if not isinstance(graph, dict):
        return ["manifest: top level must be a mapping"]
    if graph.get("schema_version") != 3:
        errors.append("schema_version: expected 3")

    nodes = graph.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return errors + ["nodes: expected a non-empty mapping"]

    _validate_nodes(nodes, errors)
    _validate_node_reviews(graph, nodes, errors)
    _validate_edges(graph, nodes, errors)
    _validate_edge_reviews(graph, nodes, errors)
    flows = _validate_flows(graph, nodes, errors)
    _validate_flow_coverage(nodes, flows, errors)
    _validate_variants(graph, nodes, errors)
    _validate_rules(graph, nodes, flows, errors)
    _validate_validation_coverage(nodes, graph, errors)
    return errors


def _missing_files(paths: list[str], root: Path, where: str) -> list[str]:
    issues: list[str] = []
    root_resolved = root.resolve()
    for path in paths:
        if not _is_safe_repo_path(path):
            issues.append(f"{where}: path escapes repository: {path}")
            continue
        resolved = (root / path).resolve()
        if not resolved.is_relative_to(root_resolved):
            issues.append(f"{where}: path resolves outside repository: {path}")
        elif not resolved.is_file():
            issues.append(f"{where}: missing {path}")
    return issues


def _unmatched_globs(patterns: list[str], root: Path, where: str) -> list[str]:
    issues: list[str] = []
    root_resolved = root.resolve()
    for pattern in patterns:
        if not _is_safe_repo_path(pattern):
            issues.append(f"{where}: path escapes repository: {pattern}")
            continue
        matches = list(root.glob(pattern))
        safe_matches = [match for match in matches if match.resolve().is_relative_to(root_resolved)]
        if len(safe_matches) != len(matches):
            issues.append(f"{where}: path resolves outside repository: {pattern}")
        elif not safe_matches:
            issues.append(f"{where}: no match for {pattern}")
    return issues


def _audit_repository_paths(graph: dict[str, Any], root: Path) -> list[str]:
    issues: list[str] = []
    for node_id, node in graph["nodes"].items():
        issues += _unmatched_globs(node.get("source", []), root, f"nodes.{node_id}.source")
        issues += _missing_files(node.get("tests", []), root, f"nodes.{node_id}.tests")
    for node_id, review in graph.get("node_reviews", {}).items():
        node = graph["nodes"].get(node_id, {})
        for index, item in enumerate(review.get("evidence", [])):
            where = f"node_reviews.{node_id}.evidence[{index}].path"
            path = item.get("path", "")
            path_issues = _missing_files([path], root, where)
            issues += path_issues
            if path_issues:
                continue
            if not any(
                _is_safe_repo_path(pattern) and PurePosixPath(path).match(pattern)
                for pattern in node.get("source", [])
            ):
                issues.append(f"{where}: evidence path is not covered by nodes.{node_id}.source")
    for index, review in enumerate(graph.get("edge_reviews", [])):
        if not isinstance(review, dict):
            continue
        source = graph["nodes"].get(review.get("source"), {})
        target = graph["nodes"].get(review.get("target"), {})
        endpoint_sources = [
            pattern
            for endpoint in (source, target)
            if isinstance(endpoint, dict)
            for pattern in endpoint.get("source", [])
        ]
        for evidence_index, item in enumerate(review.get("evidence", [])):
            if not isinstance(item, dict):
                continue
            path = item.get("path", "")
            where = f"edge_reviews[{index}].evidence[{evidence_index}].path"
            path_issues = _missing_files([path], root, where)
            issues += path_issues
            if path_issues:
                continue
            if not any(
                _is_safe_repo_path(pattern) and PurePosixPath(path).match(pattern)
                for pattern in endpoint_sources
            ):
                issues.append(f"{where}: evidence path is not covered by either endpoint source mapping")
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
        # _UniqueKeyLoader subclasses yaml.SafeLoader: no arbitrary objects.
        return yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)  # nosec B506
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"cannot read manifest {path}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository root")
    parser.add_argument("--strict-paths", action="store_true", help="Return failure for stale source/test references")
    parser.add_argument(
        "--strict-coverage", action="store_true",
        help="Return failure when a critical/high active node has no test or validation",
    )
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
    gaps = coverage_gaps(graph)
    if gaps:
        print("Coverage gaps: " + ", ".join(f"{node_id} [{criticality}]" for node_id, criticality in gaps))
        if args.strict_coverage and any(level in {"critical", "high"} for _, level in gaps):
            return 1
    else:
        print("Coverage gaps: none")
    edge_gaps = edge_review_gaps(graph)
    print(f"Edge review coverage: {len(graph.get('edges', [])) - len(edge_gaps)}/{len(graph.get('edges', []))}; {len(edge_gaps)} unreviewed")
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
