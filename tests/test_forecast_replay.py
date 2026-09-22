from datetime import datetime, timedelta, timezone

from config.settings import settings
from watcher.forecast_replay import replay_resource_forecasts
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
