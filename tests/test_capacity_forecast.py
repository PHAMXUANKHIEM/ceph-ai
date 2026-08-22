from datetime import datetime, timedelta
from types import SimpleNamespace

from watcher import capacity_forecast as subject


def _rows(count=31, slope=1.0):
    start = datetime(2026, 1, 1)
    return [SimpleNamespace(
        entity_type="cluster", entity_name="cluster", used_percent=50 + slope * i,
        used_bytes=500 + i * 10, total_bytes=1000, captured_at=start + timedelta(days=i),
    ) for i in range(count)]


def test_forecast_requires_minimum_history(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_samples", 30)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_history_days", 30)
    assert subject._forecast(_rows(30), datetime(2026, 2, 1)) is None


def test_forecast_returns_cited_threshold_dates_and_capacity(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_samples", 30)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_history_days", 30)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_confidence", .5)
    result = subject._forecast(_rows(), datetime(2026, 1, 31))
    assert result is not None
    assert result.current_percent == 80
    assert result.growth_percent_per_day == 1
    assert result.confidence == 1
    assert result.thresholds == {"80": "2026-01-31", "90": "2026-02-10", "95": "2026-02-15"}
    assert result.additional_bytes_at_95 == 187


def test_flat_growth_does_not_invent_threshold_date(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_samples", 30)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_history_days", 30)
    result = subject._forecast(_rows(slope=0), datetime(2026, 1, 31))
    assert result is not None
    assert result.thresholds == {"80": None, "90": None, "95": None}


def test_collect_stores_cluster_all_pools_and_all_osds(dashboard_client, default_cluster_id, monkeypatch):
    df = {"stats": {"total_bytes": 1000, "total_used_bytes": 500, "total_avail_bytes": 500},
          "pools": [{"name": f"p{i}", "stats": {"bytes_used": 10, "max_avail": 90, "percent_used": .1}} for i in range(12)]}
    osd = {"nodes": [{"id": i, "kb": 100, "kb_used": 10, "kb_avail": 90, "utilization": 10} for i in range(12)]}
    monkeypatch.setattr(subject, "_query", lambda _cluster, command: osd if command == "ceph osd df" else df)
    assert subject.collect_and_store(default_cluster_id) == 25
