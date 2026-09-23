import ast
from pathlib import Path


def test_walk_forward_report_is_paired_and_read_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "forecast_walk_forward_report.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls
    assert "paired_same_target" in source
    assert "same_scope" in source
    assert "same_horizon" in source
