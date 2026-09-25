import json
from pathlib import Path

import importlib.util


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "release_gate.py"
spec = importlib.util.spec_from_file_location("release_gate", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_dependency_license_status_requires_valid_artifact(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    module.ROOT = tmp_path
    assert module.dependency_license_status(artifacts)["status"] == "missing"
    (artifacts / "dependency-licenses.json").write_text(
        json.dumps({"status": "passed", "dependencies": [{"name": "river"}]}),
        encoding="utf-8",
    )
    result = module.dependency_license_status(artifacts)
    assert result["status"] == "passed"
    assert result["dependencies"] == 1


def test_pip_audit_status_counts_nested_vulnerabilities(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    module.ROOT = tmp_path
    (artifacts / "pip-audit.json").write_text(
        json.dumps({
            "dependencies": [{
                "name": "river",
                "version": "0.22.0",
                "vulns": [{"id": "CVE-TEST-1", "fix_versions": []}],
            }],
        }),
        encoding="utf-8",
    )
    result = module.pip_audit_status(artifacts)
    assert result["status"] == "failed"
    assert result["vulnerabilities"] == 1
    assert result["report_sha256"]


def test_pip_audit_status_rejects_empty_or_unknown_schema(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    module.ROOT = tmp_path
    for payload in ({}, {"vulnerabilities": []}, {"dependencies": [{}]}):
        (artifacts / "pip-audit.json").write_text(json.dumps(payload), encoding="utf-8")
        assert module.pip_audit_status(artifacts)["status"] == "invalid"


def test_coverage_status_requires_every_matrix_report_to_pass(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    module.ROOT = tmp_path
    assert module.coverage_status(artifacts)["status"] == "missing"
    for version, status in (("3.11", "passed"), ("3.12", "failed")):
        (artifacts / f"coverage-gate-{version}.json").write_text(
            json.dumps({"status": status, "total": {"line_percent": 70.0, "branch_percent": 60.0}}),
            encoding="utf-8",
        )
    result = module.coverage_status(artifacts)
    assert result["status"] == "failed"
    assert [item["status"] for item in result["reports"]] == ["passed", "failed"]
    (artifacts / "coverage-gate-3.12.json").write_text("{}", encoding="utf-8")
    assert module.coverage_status(artifacts)["reports"][1]["status"] == "invalid"


def test_static_analysis_status_carries_before_and_after_counts(tmp_path):
    artifacts = tmp_path / "artifacts"
    (artifacts / "static-analysis").mkdir(parents=True)
    module.ROOT = tmp_path
    assert module.static_analysis_status(artifacts)["status"] == "missing"
    (artifacts / "static-analysis" / "static-analysis-inventory.json").write_text(
        json.dumps({
            "status": "passed",
            "before": {"ruff": {"total": 3, "critical": 1}},
            "after": {"ruff": {"total": 2, "critical": 0}},
        }),
        encoding="utf-8",
    )
    result = module.static_analysis_status(artifacts)
    assert result["status"] == "passed"
    assert result["before"]["ruff"]["total"] == 3
    assert result["after"]["ruff"]["total"] == 2
