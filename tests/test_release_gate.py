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
