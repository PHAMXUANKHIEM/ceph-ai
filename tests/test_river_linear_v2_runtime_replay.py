import ast
from pathlib import Path


def test_runtime_replay_is_read_only_and_shadow_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "river_linear_v2_runtime_replay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls
    assert "SHADOW_ONLY" in source
    assert "VERIFIED_OUTCOMES" in source
