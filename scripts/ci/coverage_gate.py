#!/usr/bin/env python3
"""Coverage gate: no regression against the recorded release baseline.

The gate reads a Cobertura XML report produced by ``pytest --cov --cov-branch``
and compares it with ``scripts/ci/coverage_baseline.json``:

* total line and branch coverage may not fall below the baseline;
* every critical-path group (auth/session, cluster scope, action gateway,
  migration wrapper, outbox, deploy tooling) may not fall below its own
  baseline;
* the 90% critical-path target is reported per group and becomes blocking
  with ``--enforce-critical-target`` once the backlog has been closed.

A critical file that is absent from the report counts as 0% covered; a skipped
test never contributes coverage, so it cannot turn a critical path green.  The
JSON report is written before the exit status is decided so CI can upload it
even when the gate fails.
"""

from __future__ import annotations

import argparse
import json
import sys
# Reports are produced by this repository's own CI run, not untrusted input.
import xml.etree.ElementTree as ET  # nosec B405
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASELINE = ROOT / "scripts" / "ci" / "coverage_baseline.json"
# Coverage percentages are rounded to two decimals in the baseline; allow the
# rounding error but nothing that could hide a removed test.
TOLERANCE_PERCENT = 0.05


def _normalise(filename: str, sources: list[str]) -> str:
    path = Path(filename)
    if path.is_absolute():
        try:
            return path.relative_to(ROOT).as_posix()
        except ValueError:
            return path.as_posix()
    if (ROOT / path).is_file():
        return path.as_posix()
    # Without ``relative_files`` coverage.py names files relative to one of
    # several <source> roots; pick the root that actually contains the file.
    for source in sources:
        source_path = Path(source)
        if not source_path.is_absolute():
            source_path = ROOT / source_path
        if (source_path / path).is_file():
            try:
                return (source_path / path).relative_to(ROOT).as_posix()
            except ValueError:
                return (source_path / path).as_posix()
    return path.as_posix()


def parse_cobertura(path: Path) -> dict[str, dict[str, int]]:
    """Return per-file line/branch counters from a Cobertura report."""
    root = ET.parse(path).getroot()  # nosec B314
    sources = [(node.text or "").strip() for node in root.iter("source")]
    files: dict[str, dict[str, int]] = {}
    for node in root.iter("class"):
        filename = _normalise(node.attrib.get("filename", ""), sources)
        counters = files.setdefault(
            filename, {"lines_valid": 0, "lines_covered": 0, "branches_valid": 0, "branches_covered": 0}
        )
        for line in node.iter("line"):
            counters["lines_valid"] += 1
            if int(line.attrib.get("hits", "0")) > 0:
                counters["lines_covered"] += 1
            if line.attrib.get("branch") == "true":
                condition = line.attrib.get("condition-coverage", "")
                # Format: "50% (1/2)".
                if "(" in condition and "/" in condition:
                    covered, valid = condition.split("(", 1)[1].rstrip(")").split("/", 1)
                    counters["branches_valid"] += int(valid)
                    counters["branches_covered"] += int(covered)
    return files


def _percent(covered: int, valid: int) -> float:
    return round(100.0 * covered / valid, 2) if valid else 100.0


def summarise(files: dict[str, dict[str, int]], selected: list[str] | None = None) -> dict[str, Any]:
    names = list(files) if selected is None else selected
    totals = {"lines_valid": 0, "lines_covered": 0, "branches_valid": 0, "branches_covered": 0}
    missing = []
    for name in names:
        counters = files.get(name)
        if counters is None:
            missing.append(name)
            continue
        for key in totals:
            totals[key] += counters[key]
    summary: dict[str, Any] = {
        **totals,
        "line_percent": _percent(totals["lines_covered"], totals["lines_valid"]),
        "branch_percent": _percent(totals["branches_covered"], totals["branches_valid"]),
    }
    if selected is not None:
        summary["files"] = len(names)
        summary["missing_files"] = missing
        if missing:
            # An unmeasured critical file must not look healthy.
            summary["line_percent"] = 0.0 if not totals["lines_valid"] else summary["line_percent"]
            summary["branch_percent"] = 0.0 if not totals["branches_valid"] else summary["branch_percent"]
    return summary


def junit_skips(paths: list[Path]) -> list[str]:
    skipped: list[str] = []
    for path in paths:
        try:
            root = ET.parse(path).getroot()  # nosec B314
        except (OSError, ET.ParseError):
            continue
        for case in root.iter("testcase"):
            if case.find("skipped") is not None:
                skipped.append(f"{case.attrib.get('classname', '')}::{case.attrib.get('name', '')}")
    return sorted(skipped)


def evaluate(
    files: dict[str, dict[str, int]],
    baseline: dict[str, Any],
    *,
    enforce_target: bool,
) -> tuple[dict[str, Any], list[str]]:
    failures: list[str] = []
    total = summarise(files)
    base_total = baseline.get("total", {})
    for metric in ("line_percent", "branch_percent"):
        floor = float(base_total.get(metric, 0.0))
        if total[metric] + TOLERANCE_PERCENT < floor:
            failures.append(f"total {metric} {total[metric]:.2f} < baseline {floor:.2f}")

    target = float(baseline.get("critical_target_percent", 90.0))
    groups: dict[str, Any] = {}
    for name, config in sorted(baseline.get("critical_paths", {}).items()):
        summary = summarise(files, list(config.get("files", [])))
        base_group = config.get("baseline", {})
        for metric in ("line_percent", "branch_percent"):
            floor = float(base_group.get(metric, 0.0))
            if summary[metric] + TOLERANCE_PERCENT < floor:
                failures.append(f"critical path {name} {metric} {summary[metric]:.2f} < baseline {floor:.2f}")
        if summary["missing_files"]:
            failures.append(f"critical path {name} has unmeasured files: {', '.join(summary['missing_files'])}")
        summary["target_percent"] = target
        summary["target_met"] = summary["line_percent"] >= target and summary["branch_percent"] >= target
        if enforce_target and not summary["target_met"]:
            failures.append(
                f"critical path {name} below {target:.0f}% target "
                f"(line {summary['line_percent']:.2f}, branch {summary['branch_percent']:.2f})"
            )
        groups[name] = summary
    return {"total": total, "critical_paths": groups}, failures


def _floor(summary: dict[str, Any], margin: float) -> dict[str, float]:
    return {
        "line_percent": max(0.0, round(summary.get("line_percent", 0.0) - margin, 2)),
        "branch_percent": max(0.0, round(summary.get("branch_percent", 0.0) - margin, 2)),
        "measured_line_percent": summary.get("line_percent", 0.0),
        "measured_branch_percent": summary.get("branch_percent", 0.0),
    }


def write_baseline(path: Path, current: dict[str, Any], template: dict[str, Any], *, margin: float) -> None:
    """Record floors ``margin`` points below the measured values.

    The margin absorbs interpreter and environment differences between the
    machine that recorded the baseline and the CI matrix; ratchet it to 0 once
    a CI coverage artifact has been recorded.
    """
    updated = dict(template)
    updated["recorded_at"] = datetime.now(timezone.utc).date().isoformat()
    updated["margin_percent"] = margin
    updated["total"] = _floor(current["total"], margin)
    for name, config in updated.get("critical_paths", {}).items():
        config["baseline"] = _floor(current["critical_paths"].get(name, {}), margin)
    path.write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coverage-xml", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--junit", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--enforce-critical-target", action="store_true")
    parser.add_argument("--margin", type=float, default=0.0, help="baseline floor margin in points")
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="record the current report as the new baseline (operator action, never in CI)",
    )
    args = parser.parse_args(argv)

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    files = parse_cobertura(args.coverage_xml)
    current, failures = evaluate(files, baseline, enforce_target=args.enforce_critical_target)
    skipped = junit_skips(args.junit)

    if args.write_baseline:
        write_baseline(args.baseline, current, baseline, margin=args.margin)
        failures = []

    report = {
        "schema": "ceph-ai.coverage-gate.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage_xml": str(args.coverage_xml),
        "baseline_recorded_at": baseline.get("recorded_at"),
        "status": "failed" if failures else "passed",
        "failures": failures,
        "skipped_tests": len(skipped),
        "skipped_test_ids": skipped[:200],
        **current,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    total = current["total"]
    print(f"coverage total line={total['line_percent']:.2f}% branch={total['branch_percent']:.2f}%")
    for name, group in current["critical_paths"].items():
        marker = "ok" if group["target_met"] else "below-target"
        print(
            f"  {name}: line={group['line_percent']:.2f}% branch={group['branch_percent']:.2f}% [{marker}]"
        )
    if failures:
        print("COVERAGE GATE FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("COVERAGE GATE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
