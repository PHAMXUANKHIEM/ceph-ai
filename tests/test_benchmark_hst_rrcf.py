import ast
from pathlib import Path


def test_hst_rrcf_benchmark_is_optional_and_read_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "benchmark_hst_rrcf.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls
    assert "production_dependency" in source
    assert '"rrcf"' in source
    assert "alert_rate_on_scored_points" in source
    assert "abstention_rate" in source
    assert "promotion_allowed" in source
