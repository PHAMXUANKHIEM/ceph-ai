import ast
from pathlib import Path
from datetime import datetime, timedelta, timezone
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
        current_percent=40.0, predicted_percent=41.0, actual_percent=42.0,
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
    assert report["baseline_scored_outcomes"] == 1
    assert report["naive_scored_outcomes"] == 1


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


def test_replay_reports_data_quality_blockers_separately(monkeypatch):
    monkeypatch.setattr(replay, "build_features", lambda *_args, **_kwargs: SimpleNamespace(
        quality_status="GAP_DETECTED", feature_schema="test-v1", features={},
    ))
    row = SimpleNamespace(
        id="run-gap", cluster_name="CS-LAB", host="node-1", metric="cpu",
        horizon_hours=1, predicted_at=datetime(2026, 9, 1),
        target_at=datetime(2026, 9, 1, 1), current_percent=40.0, actual_percent=42.0,
    )
    report = replay._replay_scope([row], {})
    assert report["evidence_status"] == "DATA_QUALITY_BLOCKED"
    assert report["quality_blockers"] == {"GAP_DETECTED": 1}


def test_replay_uses_runtime_telemetry_instead_of_forecast_rows(monkeypatch):
    origin = datetime(2026, 9, 1, tzinfo=timezone.utc)
    captured = []

    def fake_build_features(points, **kwargs):
        captured.append(list(points))
        return SimpleNamespace(
            quality_status="OK", feature_schema="test-v1", features={"current": points[-1].value},
        )

    monkeypatch.setattr(replay, "build_features", fake_build_features)
    runs = [SimpleNamespace(
        id=f"run-{index}", cluster_name="CS-LAB", host="node-1", metric="cpu",
        horizon_hours=1, predicted_at=(origin + timedelta(hours=index)).replace(tzinfo=None),
        target_at=(origin + timedelta(hours=index + 1)).replace(tzinfo=None),
        current_percent=99.0, actual_percent=42.0,
    ) for index in range(3)]
    observations = [
        replay.MetricPoint(origin + timedelta(minutes=15 * index), float(index))
        for index in range(13)
    ]

    report = replay._replay_scope(runs, {}, observations)

    assert captured
    assert captured[-1][-1].value == 8.0
    assert captured[-1][-1].value != runs[-1].current_percent
    assert report["telemetry_source"] == "loki"
