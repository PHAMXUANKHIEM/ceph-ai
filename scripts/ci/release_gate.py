#!/usr/bin/env python3
"""Build a secret-free, machine-readable release-gate evidence report.

This gate deliberately separates CI evidence from production approval.  CI can
prove that the checked-out SHA is testable, has one migration head and has
passed the required dependency/image checks; operator approval, staging
rehearsal and live DR remain explicit release decisions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def command_output(*command: str) -> tuple[int, str]:
    completed = subprocess.run(
        list(command),
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def git_value(*args: str) -> str:
    rc, output = command_output("git", *args)
    return output if rc == 0 else ""


def tool_version(binary: str, *args: str) -> str:
    executable = shutil.which(binary)
    if not executable:
        return ""
    rc, output = command_output(executable, *(args or ("--version",)))
    return output.splitlines()[0] if rc == 0 and output else ""


def dependency_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    candidates = [Path("pyproject.toml"), Path("poetry.lock"), Path("requirements.txt"),
                  Path("ceph-health-dashboard/package-lock.json")]
    for relative in candidates:
        path = ROOT / relative
        if path.is_file():
            hashes[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def parse_junit(path: Path) -> dict[str, Any]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    duration = 0.0
    for suite in suites:
        for key in totals:
            value = suite.attrib.get(key)
            if value:
                totals[key] += int(float(value))
        duration += float(suite.attrib.get("time", "0") or 0)
    totals["duration_seconds"] = round(duration, 3)
    totals["path"] = str(path.relative_to(ROOT))
    return totals


def collect_junit(artifacts: Path) -> list[dict[str, Any]]:
    reports = []
    for path in sorted(artifacts.rglob("*.xml")):
        if "pytest" not in path.name.lower():
            continue
        try:
            reports.append(parse_junit(path))
        except (ET.ParseError, ValueError) as exc:
            reports.append({"path": str(path.relative_to(ROOT)), "parse_error": str(exc)})
    return reports


def quality_status(artifacts: Path) -> dict[str, Any]:
    reports = sorted(artifacts.rglob("quality-summary.txt"))
    if not reports:
        return {"status": "missing", "path": None}
    text = reports[0].read_text(encoding="utf-8", errors="replace")
    passed = text.startswith("QUALITY GATE PASSED")
    return {
        "status": "passed" if passed else "failed",
        "path": str(reports[0].relative_to(ROOT)),
        "summary": text.splitlines()[0] if text.splitlines() else "",
    }


def pip_audit_status(artifacts: Path) -> dict[str, Any]:
    reports = sorted(artifacts.rglob("pip-audit.json"))
    if not reports:
        return {"status": "missing", "path": None}
    try:
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        vulnerabilities = payload if isinstance(payload, list) else payload.get("vulnerabilities", [])
        count = len(vulnerabilities or [])
        return {
            "status": "passed" if count == 0 else "failed",
            "path": str(reports[0].relative_to(ROOT)),
            "vulnerabilities": count,
        }
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        return {"status": "invalid", "path": str(reports[0].relative_to(ROOT)), "error": str(exc)}


def image_scan_status(artifacts: Path) -> dict[str, Any]:
    reports = sorted(artifacts.rglob("trivy-image.json"))
    if not reports:
        return {"status": "missing", "path": None}
    try:
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        vulnerabilities = []
        for result in payload.get("Results", []):
            vulnerabilities.extend(result.get("Vulnerabilities") or [])
        blocking = [item for item in vulnerabilities if item.get("Severity") in {"HIGH", "CRITICAL"}]
        return {
            "status": "passed" if not blocking else "failed",
            "path": str(reports[0].relative_to(ROOT)),
            "vulnerabilities": len(vulnerabilities),
            "blocking_vulnerabilities": len(blocking),
        }
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        return {"status": "invalid", "path": str(reports[0].relative_to(ROOT)), "error": str(exc)}


def sbom_status(artifacts: Path) -> dict[str, Any]:
    reports = sorted(artifacts.rglob("sbom.cyclonedx.json"))
    if not reports:
        return {"status": "missing", "path": None}
    try:
        payload = json.loads(reports[0].read_text(encoding="utf-8"))
        components = payload.get("components", [])
        return {
            "status": "passed" if isinstance(components, list) else "invalid",
            "path": str(reports[0].relative_to(ROOT)),
            "components": len(components) if isinstance(components, list) else 0,
        }
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        return {"status": "invalid", "path": str(reports[0].relative_to(ROOT)), "error": str(exc)}


def migration_status() -> dict[str, Any]:
    rc, output = command_output(sys.executable, "-m", "alembic", "heads")
    heads = [line.split()[0] for line in output.splitlines() if line.strip() and "(head)" in line]
    return {
        "status": "passed" if rc == 0 and len(heads) == 1 else "failed",
        "heads": heads,
        "head_count": len(heads),
        "output": output[-2000:],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "release")
    parser.add_argument("--require-ci-artifacts", action="store_true")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    output.mkdir(parents=True, exist_ok=True)
    artifacts = ROOT / "artifacts"

    status = {
        # CI downloads JUnit/security artifacts into the checkout before this
        # script runs. Ignore untracked evidence files while still detecting
        # tracked-file modifications in the clean checkout.
        "git_clean": not bool(git_value("status", "--porcelain", "--untracked-files=no")),
        "quality": quality_status(artifacts),
        "pip_audit": pip_audit_status(artifacts),
        "image_scan": image_scan_status(artifacts),
        "sbom": sbom_status(artifacts),
        "migration": migration_status(),
    }
    test_reports = collect_junit(artifacts)
    test_status = {
        "status": "passed" if test_reports and all(
            report.get("failures", 0) == 0 and report.get("errors", 0) == 0 and "parse_error" not in report
            for report in test_reports
        ) else ("missing" if not test_reports else "failed"),
        "reports": test_reports,
    }
    status["tests"] = test_status

    image_digests = sorted(artifacts.rglob("image-digest.txt"))
    image_digest = image_digests[0].read_text(encoding="utf-8").strip() if image_digests else ""
    evidence = {
        "schema": "ceph-ai.release-evidence.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": git_value("rev-parse", "HEAD"),
        "branch": git_value("branch", "--show-current"),
        "python": sys.version.split()[0],
        "node": tool_version("node"),
        "dependency_sha256": dependency_hashes(),
        "image_digest": image_digest,
        "rollback_sha": os.environ.get("ROLLBACK_SHA", "").strip() or None,
        "warning_count": os.environ.get("PYTEST_WARNING_COUNT", "unknown"),
        "artifact_files": sorted(
            str(path.relative_to(ROOT)) for path in artifacts.rglob("*") if path.is_file()
        ),
        "status": status,
        "production_decision": {
            "status": "pending",
            "operator_signoff": "pending",
            "staging_migration_rehearsal": "pending",
            "postgres_backup_restore_witness": "pending",
            "browser_authenticated_smoke": "pending",
            "isolated_live_dr_drill": "pending",
            "rollback_sha_and_image_digest": "pending" if not image_digest else "sha-and-image-artifact-recorded",
        },
    }
    report_path = output / "release-evidence.json"
    report_path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary_path = output / "release-evidence.txt"
    summary_path.write_text(
        "Release evidence\n"
        f"commit_sha={evidence['commit_sha']}\n"
        f"git_clean={status['git_clean']}\n"
        f"migration={status['migration']['status']} heads={status['migration']['heads']}\n"
        f"tests={test_status['status']} reports={len(test_reports)}\n"
        f"quality={status['quality']['status']} pip_audit={status['pip_audit']['status']}\n"
        f"image_scan={status['image_scan']['status']} sbom={status['sbom']['status']}\n"
        f"rollback_sha={evidence['rollback_sha'] or 'pending'}\n"
        "production_decision=pending (staging/DR/operator approval are separate gates)\n",
        encoding="utf-8",
    )

    required = ["quality", "pip_audit", "image_scan", "sbom", "migration"]
    if args.require_ci_artifacts:
        required.append("tests")
    failures = [name for name in required if status[name].get("status") != "passed"]
    if not status["git_clean"]:
        failures.append("git_clean")
    if failures:
        print(f"RELEASE GATE FAILED: {', '.join(failures)}")
        print(report_path)
        return 1
    print("RELEASE GATE PASSED: CI evidence is complete; production approval remains pending")
    print(report_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
