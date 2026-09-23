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
