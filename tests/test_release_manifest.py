import importlib.util
import json
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "release_manifest.py"
spec = importlib.util.spec_from_file_location("release_manifest", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_manifest_binds_commit_dependencies_and_image(tmp_path, monkeypatch):
    module.ROOT = tmp_path
    (tmp_path / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "image-digest.txt").write_text("sha256:test-image\n", encoding="utf-8")
    monkeypatch.setattr(module, "_git_value", lambda *args: {
        ("rev-parse", "HEAD"): "a" * 40,
        ("branch", "--show-current"): "main",
        ("status", "--porcelain", "--untracked-files=no"): "",
    }.get(args, ""))
    monkeypatch.setattr(module, "migration_heads", lambda: {
        "status": "passed", "heads": ["m20260923incidentmetrics"], "head_count": 1, "output": ""
    })

    payload = module.build_manifest("ci", artifacts)

    assert payload["schema"] == "ceph-ai.release-manifest.v1"
    assert payload["repository"]["commit_sha"] == "a" * 40
    assert payload["migration"]["head_count"] == 1
    assert payload["image"]["digest"] == "sha256:test-image"
    assert payload["dependencies"]["sha256"]["pyproject.toml"]


def test_manifest_writer_is_atomic_and_json(tmp_path):
    output = tmp_path / "release" / "manifest.json"
    module.write_atomic(output, {"schema": "test", "secret": None})
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == "test"
    assert not list(output.parent.glob("*.tmp"))


def test_manifest_carries_release_gate_evidence_and_residual_risk(tmp_path, monkeypatch):
    module.ROOT = tmp_path
    artifacts = tmp_path / "artifacts" / "release"
    artifacts.mkdir(parents=True)
    (artifacts / "release-evidence.json").write_text(json.dumps({
        "status": {
            "tests": {"status": "passed"},
            "quality": {"status": "passed"},
            "pip_audit": {"status": "passed"},
            "image_scan": {"status": "passed"},
            "sbom": {"status": "passed"},
        },
        "production_decision": {"status": "pending"},
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_git_value", lambda *args: "a" * 40)
    monkeypatch.setattr(module, "migration_heads", lambda: {
        "status": "passed", "heads": ["head"], "head_count": 1, "output": ""
    })
    monkeypatch.setenv("CEPH_AI_CONFIG_FINGERPRINT", "sha256:config")
    monkeypatch.setenv("CEPH_AI_ROLLBACK_ARTIFACT", "rollback.json")

    payload = module.build_manifest("ci", tmp_path / "artifacts")

    assert payload["evidence"]["tests"]["status"] == "passed"
    assert payload["evidence"]["image_scan"]["status"] == "passed"
    assert payload["evidence"]["config_fingerprint"] == "sha256:config"
    assert payload["evidence"]["rollback_artifact"] == "rollback.json"
    assert payload["production_approval"]["residual_risk"]
