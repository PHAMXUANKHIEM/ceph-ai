from datetime import datetime, timedelta
from types import SimpleNamespace

from watcher import capacity_forecast as subject


def _rows(count=31, slope=1.0):
    start = datetime(2026, 1, 1)
    return [SimpleNamespace(
        entity_type="cluster", entity_name="cluster", used_percent=50 + slope * i,
        used_bytes=500 + i * 10, total_bytes=1000, captured_at=start + timedelta(days=i),
    ) for i in range(count)]


def test_capacity_alert_lifecycle_exposes_recovery_and_pending_delivery(monkeypatch):
    class Query:
        def filter_by(self, **_kwargs):
            return self

        def order_by(self, *_args):
            return self

        def limit(self, _count):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    entity_type="pool", entity_name="images", current_threshold=0,
                    notified_threshold=90, updated_at=datetime(2026, 1, 2),
                    last_attempt_at=datetime(2026, 1, 2, 0, 1),
                ),
                SimpleNamespace(
                    entity_type="cluster", entity_name="cluster", current_threshold=95,
                    notified_threshold=90, updated_at=datetime(2026, 1, 3),
                    last_attempt_at=None,
                ),
            ]

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def query(self, *_args):
            return Query()

    monkeypatch.setattr(subject.db, "SessionLocal", lambda: Session())
    result = subject.capacity_alert_lifecycle("cluster-1")
    assert result[0]["status"] == "RESOLVED"
    assert result[0]["delivery_pending"] is True
    assert result[1]["status"] == "OPEN"
    assert result[1]["delivery_pending"] is True


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


def test_forecast_includes_confidence_interval_and_rolling_backtest(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_samples", 30)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_history_days", 30)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_confidence", .1)
    result = subject._forecast(_rows(45, slope=.5), datetime(2026, 2, 15))

    assert result is not None
    assert result.forecast_method == "linear"
    assert result.confidence_interval["low"] <= result.confidence_interval["high"]
    assert result.predicted_percent_at_horizon is not None
    assert result.backtest["status"] == "ready"
    assert result.backtest["samples"] > 0


def test_forecast_uses_weekly_correction_only_with_enough_history(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_samples", 30)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_history_days", 30)
    rows = []
    start = datetime(2026, 1, 1)
    weekday_effect = {0: 3.0, 1: -2.0, 2: 1.0, 3: -1.0, 4: 2.0, 5: -3.0, 6: 0.0}
    for index in range(35):
        captured_at = start + timedelta(days=index)
        rows.append(SimpleNamespace(
            entity_type="pool", entity_name="images", used_percent=(40 + index * .5 + weekday_effect[captured_at.weekday()]),
            used_bytes=500, total_bytes=1000, captured_at=captured_at,
        ))

    result = subject._forecast(rows, datetime(2026, 2, 5))

    assert result is not None
    assert result.forecast_method == "seasonal_linear"
    assert result.spike_detected is False


def test_forecast_marks_recent_spike_and_reduces_confidence(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_samples", 10)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_history_days", 10)
    rows = _rows(20, slope=.2)
    rows[-1].used_percent += 10

    result = subject._forecast(rows, datetime(2026, 1, 20))

    assert result is not None
    assert result.spike_detected is True
    assert result.forecast_method.endswith("spike_guarded")
    assert any("spike" in line.lower() for line in result.risk_explanation)


def test_collect_stores_cluster_all_pools_and_all_osds(dashboard_client, default_cluster_id, monkeypatch):
    df = {"stats": {"total_bytes": 1000, "total_used_bytes": 500, "total_avail_bytes": 500},
          "pools": [{"name": f"p{i}", "stats": {"bytes_used": 10, "max_avail": 90, "percent_used": .1}} for i in range(12)]}
    osd = {"nodes": [{"id": i, "kb": 100, "kb_used": 10, "kb_avail": 90, "utilization": 10} for i in range(12)]}
    monkeypatch.setattr(subject, "_query", lambda _cluster, command: osd if command == "ceph osd df" else df)
    monkeypatch.setattr(subject, "send_capacity_threshold_alert", lambda *args, **kwargs: True)
    monkeypatch.setattr(subject, "send_capacity_recovery_alert", lambda *args, **kwargs: True)
    assert subject.collect_and_store(default_cluster_id) == 25


def test_collect_alerts_only_when_crossing_a_higher_threshold(dashboard_client, default_cluster_id, monkeypatch):
    percent = {"value": 79.0}

    def query(_cluster, command):
        if command == "ceph osd df":
            return {"nodes": []}
        used = int(percent["value"] * 10)
        return {"stats": {"total_bytes": 1000, "total_used_bytes": used, "total_avail_bytes": 1000 - used}, "pools": []}

    alerts = []
    monkeypatch.setattr(subject, "_query", query)
    monkeypatch.setattr(subject, "send_capacity_threshold_alert", lambda *args, **kwargs: alerts.append((args, kwargs)) or True)
    monkeypatch.setattr(subject, "send_capacity_recovery_alert", lambda *args, **kwargs: True)

    subject.collect_and_store(default_cluster_id, now=datetime(2026, 1, 1))
    percent["value"] = 81
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 1, 2))
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 1, 3))
    percent["value"] = 91
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 1, 4))

    assert [item[0][3] for item in alerts] == [80, 90]


def test_collect_realerts_after_capacity_recovers_and_recrosses(dashboard_client, default_cluster_id, monkeypatch):
    percent = {"value": 96.0}
    alerts = []

    def query(_cluster, command):
        if command == "ceph osd df":
            return {"nodes": []}
        used = int(percent["value"] * 10)
        return {"stats": {"total_bytes": 1000, "total_used_bytes": used, "total_avail_bytes": 1000 - used}, "pools": []}

    monkeypatch.setattr(subject, "_query", query)
    monkeypatch.setattr(subject, "send_capacity_threshold_alert", lambda *args, **kwargs: alerts.append(args[3]) or True)
    monkeypatch.setattr(subject, "send_capacity_recovery_alert", lambda *args, **kwargs: True)
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 2, 1))
    percent["value"] = 70
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 2, 2))
    percent["value"] = 96
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 2, 3))

    assert alerts == [95, 95]


def test_failed_delivery_is_retried_and_recovery_is_sent(dashboard_client, default_cluster_id, monkeypatch):
    percent = {"value": 79.0}
    attempts = []
    recoveries = []

    def query(_cluster, command):
        if command == "ceph osd df":
            return {"nodes": []}
        used = int(percent["value"] * 10)
        return {"stats": {"total_bytes": 1000, "total_used_bytes": used, "total_avail_bytes": 1000 - used}, "pools": []}

    def send(*args, **kwargs):
        attempts.append(args[3])
        return len(attempts) > 1

    monkeypatch.setattr(subject, "_query", query)
    monkeypatch.setattr(subject, "send_capacity_threshold_alert", send)
    monkeypatch.setattr(subject, "send_capacity_recovery_alert", lambda *args, **kwargs: recoveries.append(args[3:5]) or True)
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 3, 1))
    percent["value"] = 81
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 3, 2))
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 3, 2, 0, 1))
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 3, 2, 0, 6))
    percent["value"] = 70
    subject.collect_and_store(default_cluster_id, now=datetime(2026, 3, 3))

    assert attempts == [80, 80]
    assert recoveries == [(80, 0)]
