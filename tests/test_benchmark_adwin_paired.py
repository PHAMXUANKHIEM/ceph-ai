import ast
from pathlib import Path


def test_adwin_paired_benchmark_is_evidence_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "benchmark_adwin_paired.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls
    assert "same_sample_unit" in source
    assert "operator approval required" in source
