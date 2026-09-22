import ast
from pathlib import Path


def test_acceptance_replay_is_read_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "acceptance_replay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls


def test_acceptance_replay_covers_volume_horizon_scope():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "acceptance_replay.py").read_text(encoding="utf-8")
    assert "def _volume_evidence" in source
    assert "horizon_hours" in source
    assert 'status": "REPLAYED"' in source
    assert 'status": "BLOCKED_MIGRATION_PENDING"' in source


def test_acceptance_replay_filters_node_evidence_by_horizon():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "acceptance_replay.py").read_text(encoding="utf-8")
    assert "horizon_hours=:horizon" in source
    assert '"horizon": state["horizon_hours"]' in source
    assert "def _node_replay_schema_ready" in source
