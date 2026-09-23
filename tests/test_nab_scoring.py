import ast
from pathlib import Path


def test_nab_adapter_is_versioned_and_read_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "nab_scoring.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls
    assert "ceph-ai-nab-adapted-v1" in source
    assert "false_alerts_per_day" in source
    assert "incident_window_seconds" in source
