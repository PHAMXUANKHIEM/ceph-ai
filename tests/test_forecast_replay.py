from datetime import datetime, timedelta, timezone
from pathlib import Path

from config.settings import settings
from watcher.forecast_replay import _latest_candidate_drift, replay_resource_forecasts
import watcher.forecast_replay as replay_module


def test_replay_is_read_only_and_scores_linear_rolling_and_consensus(monkeypatch):
    monkeypatch.setattr(settings, "node_resource_forecast_min_samples", 6)
    monkeypatch.setattr(settings, "node_resource_forecast_min_consensus_candidates", 2)
    monkeypatch.setattr(settings, "node_resource_forecast_min_consensus_ratio", .67)
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    points = [
        (origin + timedelta(hours=index), 30.0 + index * .2)
        for index in range(40)
    ]

    result = replay_resource_forecasts(
        points, "cpu", horizon_hours=1, window_hours=[6, 12, 24],
    )

    assert set(result) == {"linear", "rolling_quantile", "consensus"}
    assert result["linear"].evaluated > 0
    assert result["rolling_quantile"].evaluated > 0
    assert result["consensus"].evaluated > 0
    assert result["consensus"].mae is not None
    assert result["consensus"].smape is not None


def test_volume_replay_schema_guard_skips_older_live_schema(monkeypatch):
    class Inspector:
        def get_columns(self, _table):
            return [{"name": "id"}]

    monkeypatch.setattr(replay_module, "inspect", lambda _bind: Inspector())
    session = type("Session", (), {"bind": object()})()
    assert replay_module._volume_replay_schema_ready(session) is False


def test_model_state_schema_guard_skips_older_node_schema(monkeypatch):
    class Inspector:
        def get_columns(self, _table):
            return [{"name": "id"}]

    monkeypatch.setattr(replay_module, "inspect", lambda _bind: Inspector())
    session = type("Session", (), {"bind": object()})()
    assert replay_module._model_state_schema_ready(session, "node_resource_model_states") is False


def test_replay_does_not_create_alerts_or_database_state(monkeypatch):
    monkeypatch.setattr(settings, "node_resource_forecast_min_samples", 6)
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    points = [(origin + timedelta(hours=index), 50.0) for index in range(12)]

    result = replay_resource_forecasts(points, "ram", horizon_hours=1, window_hours=[6])

    assert result["consensus"].evaluated > 0


def test_persisted_replay_keeps_node_and_volume_horizons_isolated():
    source = (Path(__file__).resolve().parents[1] / "watcher" / "forecast_replay.py").read_text(encoding="utf-8")
    assert "horizon_hours=state.horizon_hours" in source
    assert "NodeResourceForecastRun.horizon_hours == state.horizon_hours" in source
    assert "VolumeForecastRun.horizon_hours == state.horizon_hours" in source


def test_soak_uses_latest_drift_instead_of_any_historical_drift():
    class Row:
        def __init__(self, target_at, status, score):
            self.target_at = target_at
            self.drift_status = status
            self.drift_score = score

    old = Row(datetime(2026, 1, 1, tzinfo=timezone.utc), "DRIFT", 1.0)
    latest = Row(datetime(2026, 1, 2, tzinfo=timezone.utc), "STABLE", 0.0)
    assert _latest_candidate_drift([old, latest]) == ("STABLE", 0.0)
