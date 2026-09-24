import ast
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace

from scripts import river_linear_v2_runtime_replay as replay


def test_runtime_replay_is_read_only_and_shadow_only():
    source = (Path(__file__).resolve().parents[1] / "scripts" / "river_linear_v2_runtime_replay.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
    assert "commit" not in calls
    assert "add" not in calls
    assert "SHADOW_ONLY" in source
    assert "VERIFIED_OUTCOMES" in source


def test_replay_does_not_train_before_outcome_is_verified(monkeypatch):
    origin = datetime(2026, 9, 1)
    monkeypatch.setattr(replay, "build_features", lambda *_args, **_kwargs: SimpleNamespace(
        quality_status="OK", feature_schema="test-v1", features={"current": 40.0},
    ))
    runs = [SimpleNamespace(
        id=f"run-{index}", cluster_name="CS-LAB", host="node-1", metric="cpu",
        horizon_hours=1, predicted_at=origin + timedelta(hours=index),
        target_at=origin + timedelta(hours=index + 1),
        current_percent=40.0, actual_percent=42.0,
    ) for index in range(3)]
    label = SimpleNamespace(
        source_actor="forecast-evaluator", evidence_fingerprint="a" * 64,
        outcome="VERIFIED_SUCCESS", label_value=42.0,
        verified_at=origin + timedelta(hours=4),
    )
    report = replay._replay_scope(runs, {runs[0].id: label})
    assert report["verified_outcomes"] == 1
    assert report["scored_outcomes"] == 0
    assert report["evidence_status"] == "INSUFFICIENT_SAMPLE"


def test_replay_reports_zero_outcomes_distinctly(monkeypatch):
    monkeypatch.setattr(replay, "build_features", lambda *_args, **_kwargs: SimpleNamespace(
        quality_status="OK", feature_schema="test-v1", features={"current": 40.0},
    ))
    row = SimpleNamespace(
        id="run-1", cluster_name="CS-LAB", host="node-1", metric="cpu",
        horizon_hours=1, predicted_at=datetime(2026, 9, 1),
        target_at=datetime(2026, 9, 1, 1), current_percent=40.0, actual_percent=42.0,
    )
    assert replay._replay_scope([row], {})["evidence_status"] == "NO_VERIFIED_OUTCOME"
