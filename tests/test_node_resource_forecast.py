from datetime import datetime, timedelta, timezone

from watcher import node_resource_forecast as forecast


def _points(count=30, slope=1.0, start=20.0):
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    return [(origin + timedelta(hours=index), start + slope * index)
            for index in range(count)]


def test_linear_forecast_predicts_threshold_crossing(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 24)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_horizon_hours", 168)
    result = forecast._linear_forecast(_points(), "ram")
    assert result is not None
    assert result.slope_percent_per_hour == 1.0
    assert result.hours_to_90 == 41.0
    assert result.confidence == 1.0


def test_linear_forecast_requires_enough_history(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 24)
    assert forecast._linear_forecast(_points(count=23), "cpu") is None


def test_flat_usage_has_no_threshold_crossing(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 24)
    result = forecast._linear_forecast(_points(slope=0), "cpu")
    assert result is not None
    assert result.hours_to_90 is None
    assert forecast.risky_forecasts({"cpu": result}) == []


def test_risky_forecast_rejects_low_confidence(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_confidence", 0.8)
    value = forecast.ResourceForecast("cpu", 80, 1, 95, 10, 0.7, 30, 29)
    assert forecast.risky_forecasts({"cpu": value}) == []


def test_forecast_reads_loki_samples(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 24)
    samples = [(ts, value, value / 2) for ts, value in _points()]
    monkeypatch.setattr(forecast, "fetch_samples", lambda cluster, host, now=None: samples)
    result = forecast.forecast("CS-LAB", "node-1")
    assert set(result) == {"cpu", "ram"}
    assert result["cpu"].samples == 30
