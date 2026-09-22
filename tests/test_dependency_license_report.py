import importlib.util
from pathlib import Path


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "ci" / "dependency_license_report.py"
spec = importlib.util.spec_from_file_location("dependency_license_report", MODULE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_direct_dependency_report_requires_pin_and_license(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["pip==1.2.3", "setuptools==4.5.6"]\n',
        encoding="utf-8",
    )
    report = module.build_report(pyproject)
    assert report["status"] == "passed"
    assert report["unpinned"] == []
    assert report["missing"] == []


def test_direct_dependency_report_detects_unpinned_and_missing(tmp_path):
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        '[project]\ndependencies = ["pip", "not-installed-package==1.0"]\n',
        encoding="utf-8",
    )
    report = module.build_report(pyproject)
    assert report["status"] == "failed"
    assert "pip" in report["unpinned"]
    assert "not-installed-package" in report["missing"]
