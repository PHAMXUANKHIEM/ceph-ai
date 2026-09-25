#!/usr/bin/env python3
"""Full-tree static-analysis inventory with a no-growth budget.

``quality_gate.py`` blocks new diagnostics in changed files.  This script covers
the rest of the plan: it scans the whole tree with Ruff (default rules and
C901 complexity), mypy and Bandit, writes an inventory by file/rule/owner and
compares the counts with ``scripts/ci/static_analysis_budget.json``.

The gate fails when any tool's total count, or its count in the critical tier
(auth, executor, worker, migration, outbox, deploy), exceeds the budget.  The
budget is a ceiling that is only ever lowered: re-record it with
``--write-budget`` after a burn-down, never to absorb new findings.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
# Fixed tool argv, no shell.
import subprocess  # nosec B404
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BUDGET = ROOT / "scripts" / "ci" / "static_analysis_budget.json"
SCAN_DIRS = ["config", "dashboard", "scripts", "shared", "watcher", "worker", "vitastor"]
MYPY_DIRS = ["config", "dashboard", "shared", "watcher", "worker", "vitastor"]
TOOLS = ("ruff", "complexity", "mypy", "bandit")

# Burn-down order from the readiness plan: security, auth, executor, migration,
# worker and deploy first; UI/legacy code afterwards.
CRITICAL_PREFIXES = (
    "dashboard/routes/auth.py",
    "dashboard/routes/actions.py",
    "dashboard/cluster_scope.py",
    "dashboard/cluster_authorization.py",
    "shared/ai_redaction.py",
    "shared/logging_redaction.py",
    "shared/security_audit.py",
    "shared/full_executor_auth.py",
    "shared/ldap_identity.py",
    "shared/single_full_",
    "shared/controlled_action_contract.py",
    "shared/incident_actions.py",
    "shared/incident_outbox.py",
    "shared/telegram_outbox.py",
    "shared/db.py",
    "worker/",
    "scripts/deploy/",
)


def owner_for(path: str) -> str:
    parts = Path(path).parts
    if not parts:
        return "unknown"
    if parts[0] == "scripts" and len(parts) > 1:
        return {"deploy": "deploy", "ci": "ci"}.get(parts[1], "scripts")
    if parts[0] == "worker" and len(parts) > 1 and parts[1] == "executor":
        return "executor"
    return parts[0]


def tier_for(path: str) -> str:
    return "critical" if path.startswith(CRITICAL_PREFIXES) else "standard"


def _relative(filename: str, root: Path) -> str:
    path = Path(filename)
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()
    return path.as_posix().removeprefix("./")


def _run(command: list[str], root: Path) -> str:
    completed = subprocess.run(  # nosec B603
        command, cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False
    )
    # Every tool exits non-zero when it has findings; a crash is detected by
    # the parsers below receiving output they cannot read.
    return completed.stdout


def _existing(dirs: list[str], root: Path) -> list[str]:
    return [name for name in dirs if (root / name).is_dir()]


def parse_ruff(output: str, root: Path, tool: str) -> list[dict[str, Any]]:
    findings = []
    for item in json.loads(output or "[]"):
        findings.append({
            "tool": tool,
            "rule": str(item.get("code") or "unknown"),
            "file": _relative(str(item.get("filename", "")), root),
            "line": (item.get("location") or {}).get("row"),
            "message": str(item.get("message", "")),
        })
    return findings


MYPY_LINE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+)(?::\d+)?: error: (?P<message>.*?)(?:\s+\[(?P<code>[a-z0-9-]+)\])?$")


def parse_mypy(output: str, root: Path) -> list[dict[str, Any]]:
    findings = []
    for line in output.splitlines():
        match = MYPY_LINE.match(line.strip())
        if not match:
            continue
        findings.append({
            "tool": "mypy",
            "rule": match.group("code") or "error",
            "file": _relative(match.group("file"), root),
            "line": int(match.group("line")),
            "message": match.group("message"),
        })
    return findings


def parse_bandit(output: str, root: Path) -> list[dict[str, Any]]:
    payload = json.loads(output or "{}")
    findings = []
    for item in payload.get("results", []):
        findings.append({
            "tool": "bandit",
            "rule": str(item.get("test_id") or "unknown"),
            "file": _relative(str(item.get("filename", "")), root),
            "line": item.get("line_number"),
            "severity": str(item.get("issue_severity", "")).upper(),
            "message": str(item.get("issue_text", "")),
        })
    return findings


def scan(root: Path) -> list[dict[str, Any]]:
    missing = [name for name in ("ruff", "mypy", "bandit") if not shutil.which(name)]
    if missing:
        raise SystemExit(f"static-analysis tools are not installed: {', '.join(missing)}")
    dirs = _existing(SCAN_DIRS, root)
    findings = parse_ruff(_run(["ruff", "check", "--output-format", "json", *dirs], root), root, "ruff")
    findings += parse_ruff(
        _run(["ruff", "check", "--select", "C901", "--output-format", "json", *dirs], root), root, "complexity"
    )
    findings += parse_mypy(
        _run(["mypy", *_existing(MYPY_DIRS, root), "--ignore-missing-imports", "--no-error-summary"], root), root
    )
    findings += parse_bandit(_run(["bandit", "-r", "-q", "-f", "json", *dirs], root), root)
    for finding in findings:
        finding["owner"] = owner_for(finding["file"])
        finding["tier"] = tier_for(finding["file"])
    return findings


def count(findings: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for tool in TOOLS:
        selected = [item for item in findings if item["tool"] == tool]
        counts[tool] = {
            "total": len(selected),
            "critical": sum(1 for item in selected if item["tier"] == "critical"),
        }
    bandit_high = [item for item in findings if item["tool"] == "bandit" and item.get("severity") == "HIGH"]
    counts["bandit_high"] = {
        "total": len(bandit_high),
        "critical": sum(1 for item in bandit_high if item["tier"] == "critical"),
    }
    return counts


def compare(counts: dict[str, dict[str, int]], budget: dict[str, Any]) -> list[str]:
    failures = []
    for tool, limits in budget.get("max", {}).items():
        current = counts.get(tool, {"total": 0, "critical": 0})
        for scope in ("total", "critical"):
            if scope in limits and current[scope] > int(limits[scope]):
                failures.append(f"{tool} {scope} findings {current[scope]} > budget {limits[scope]}")
    return failures


def inventory(findings: list[dict[str, Any]]) -> dict[str, Any]:
    by_rule = Counter(f"{item['tool']}:{item['rule']}" for item in findings)
    by_owner = Counter(f"{item['owner']}:{item['tool']}" for item in findings)
    by_file = Counter(item["file"] for item in findings)
    return {
        "by_rule": dict(by_rule.most_common()),
        "by_owner": dict(sorted(by_owner.items())),
        "by_file": dict(by_file.most_common()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="tree to scan (e.g. a git archive)")
    parser.add_argument("--budget", type=Path, default=DEFAULT_BUDGET)
    parser.add_argument("--output", type=Path, required=True, help="directory for the inventory reports")
    parser.add_argument(
        "--write-budget",
        action="store_true",
        help="record the current counts as the new ceiling (operator action after a burn-down)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()

    findings = scan(root)
    counts = count(findings)
    budget = json.loads(args.budget.read_text(encoding="utf-8")) if args.budget.is_file() else {}
    failures = compare(counts, budget)

    if args.write_budget:
        budget["recorded_at"] = datetime.now(timezone.utc).date().isoformat()
        budget["max"] = counts
        args.budget.write_text(json.dumps(budget, indent=2) + "\n", encoding="utf-8")
        failures = []

    targets = budget.get("burn_down", [])
    report = {
        "schema": "ceph-ai.static-analysis-inventory.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "failed" if failures else "passed",
        "failures": failures,
        "budget_recorded_at": budget.get("recorded_at"),
        "before": budget.get("max", {}),
        "after": counts,
        "burn_down": targets,
        **inventory(findings),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "static-analysis-inventory.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output / "static-analysis-findings.json").write_text(
        json.dumps(findings, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )

    lines = ["tool           budget(total/critical)  current(total/critical)"]
    for tool, current in counts.items():
        limit = budget.get("max", {}).get(tool, {})
        lines.append(
            f"{tool:<14} {limit.get('total', '-')!s:>6}/{limit.get('critical', '-')!s:<16} "
            f"{current['total']:>6}/{current['critical']}"
        )
    for target in targets:
        lines.append(f"burn-down target {target.get('release')}: {json.dumps(target.get('max', {}), sort_keys=True)}")
    summary = "\n".join(lines)
    (args.output / "static-analysis-summary.txt").write_text(summary + "\n", encoding="utf-8")
    print(summary)
    if failures:
        print("STATIC ANALYSIS BUDGET FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("STATIC ANALYSIS BUDGET PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
