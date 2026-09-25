import importlib.util
import json
from datetime import date
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "documentation_freshness.py"
spec = importlib.util.spec_from_file_location("documentation_freshness", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_documentation_freshness_requires_existing_documents(tmp_path, monkeypatch):
    root = tmp_path
    module.ROOT = root
    (root / "README.md").write_text("readme", encoding="utf-8")
    metadata = root / "docs.json"
    metadata.write_text(json.dumps({
        "reviewed_at": "2026-09-01",
        "expires_at": "2026-10-01",
        "required_documents": ["README.md", "missing.md"],
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_git_sha", lambda: "a" * 40)

    report = module.check_freshness(
        metadata_path=metadata, as_of=date(2026, 9, 25),
    )

    assert report["status"] == "failed"
    assert any("missing.md" in error for error in report["errors"])


def test_documentation_freshness_passes_valid_metadata(tmp_path, monkeypatch):
    root = tmp_path
    module.ROOT = root
    (root / "README.md").write_text("readme", encoding="utf-8")
    metadata = root / "docs.json"
    metadata.write_text(json.dumps({
        "reviewed_at": "2026-09-01",
        "expires_at": "2026-10-01",
        "required_documents": ["README.md"],
    }), encoding="utf-8")
    monkeypatch.setattr(module, "_git_sha", lambda: "b" * 40)

    report = module.check_freshness(
        metadata_path=metadata, as_of=date(2026, 9, 25),
    )

    assert report["status"] == "passed"
    assert report["documents"] == [{"path": "README.md", "exists": True}]
