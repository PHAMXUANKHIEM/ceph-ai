import ast
from pathlib import Path


def test_baseline_report_is_read_only_and_immutable():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "forecast_baseline_report.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls
    assert 'open("x"' in source
    assert "artifact_sha256" in source


def test_baseline_report_has_per_scope_quality_and_resource_fields():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "forecast_baseline_report.py").read_text(encoding="utf-8")
    for field in ("mae", "rmse", "smape", "bias", "false_positive_rate", "alert_volume", "data_quality_failure_rate", "sample_interval_seconds", "history_length", "state_bytes"):
        assert field in source
