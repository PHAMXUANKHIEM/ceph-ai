from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from watcher import capacity_forecast as subject
from shared import db
from shared.models import CapacityAlertState, CephCapacitySample


@pytest.fixture(autouse=True)
def approved_capacity_thresholds(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_thresholds_approved", True)


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
    assert result.chart[-1]["forecast_percent"] is not None
    assert result.chart[-1]["confidence_low"] is not None


def test_forecast_excludes_missing_quality_samples(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_samples", 3)
    monkeypatch.setattr(subject.settings, "capacity_forecast_min_history_days", 2)
    rows = _rows(4)
    rows[1].quality_status = "MISSING_VOLUME_OBSERVATION"
    rows[2].quality_status = "PARTIAL_SNAPSHOT_USAGE"

    assert subject._forecast(rows, datetime(2026, 1, 5)) is None


def test_capacity_forecast_page_serializes_chart_timestamps(dashboard_client, default_cluster_id):
    start = datetime(2026, 8, 20)
    with db.SessionLocal() as session:
        session.add_all([
            CephCapacitySample(
                cluster_id=default_cluster_id, entity_type="cluster", entity_name="cluster",
                used_bytes=500 + index, total_bytes=1000,
                used_percent=50 + index * 0.5, captured_at=start + timedelta(days=index),
            ) for index in range(31)
        ])
        session.commit()

    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/capacity-forecast")

    assert response.status_code == 200
    assert 'data-chart=' in response.text
    assert 'confidence_low' in response.text


def test_operator_approval_is_required_for_capacity_thresholds(monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_thresholds_approved", False)

    assert subject._approved_thresholds() == ()


def test_unapproved_thresholds_do_not_create_alert_state(dashboard_client, default_cluster_id, monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_thresholds_approved", False)
    monkeypatch.setattr(subject, "_query", lambda _cluster, command: {
        "nodes": []
    } if command == "ceph osd df" else {
        "stats": {"total_bytes": 1000, "total_used_bytes": 900, "total_avail_bytes": 100},
        "pools": [],
    })

    subject.collect_and_store(default_cluster_id, now=datetime(2026, 6, 1))

    with db.SessionLocal() as session:
        assert session.query(CapacityAlertState).filter_by(
            cluster_id=default_cluster_id, entity_type="cluster", entity_name="cluster",
        ).count() == 0


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


def test_collect_stores_volume_snapshot_attribution_and_redundancy(
    dashboard_client, default_cluster_id, monkeypatch,
):
    df = {
        "stats": {"total_bytes": 10000, "total_used_bytes": 5000, "total_avail_bytes": 5000},
        "pools": [{"name": "rbd", "stats": {"bytes_used": 2000, "max_avail": 3000, "percent_used": 40}}],
    }

    def query(_cluster, command):
        if command == "ceph osd df":
            return {"nodes": []}
        if command == "ceph osd pool ls detail":
            return {"pools": [{"pool_name": "rbd", "size": 3}]}
        return df

    monkeypatch.setattr(subject, "_query", query)
    monkeypatch.setattr(subject.ceph_client, "query_rbd_capacity_inventory", lambda _pool: [{
        "name": "vm-a", "image_id": "1", "provisioned_size": 1000,
        "used_size": 400, "used_percent": 40.0, "snapshot_count": 1,
        "snapshot_provisioned_size": 1000, "snapshot_used_size": 100,
        "thin_provisioned_bytes": 600,
    }])

    assert subject.collect_and_store(
        default_cluster_id, now=datetime(2026, 4, 1), include_volume_inventory=True,
    ) == 2
    with db.SessionLocal() as session:
        pool = session.query(CephCapacitySample).filter_by(
            cluster_id=default_cluster_id, entity_type="pool", entity_name="rbd",
        ).one()
        volume = session.query(CephCapacitySample).filter_by(
            cluster_id=default_cluster_id, entity_type="volume", entity_name="rbd/vm-a",
        ).one()

    assert pool.provisioned_bytes == 1000
    assert pool.snapshot_bytes == 100
    assert pool.snapshot_provisioned_bytes == 1000
    assert pool.replica_factor == 3
    assert pool.failure_domain_reserve_percent == subject.settings.capacity_forecast_failure_domain_reserve_percent
    assert volume.logical_used_bytes == 400
    assert volume.snapshot_count == 1


def test_pool_redundancy_reads_ec_profile_without_guessing(dashboard_client, monkeypatch):
    monkeypatch.setattr(subject, "_query", lambda _cluster, _command: {
        "pools": [{"pool_name": "ec", "type": "erasure", "erasure_code_profile": "ec42"}],
    })
    monkeypatch.setattr(subject.ceph_client, "query_erasure_code_profile", lambda _profile: {"k": "4", "m": "2"})

    assert subject._pool_redundancy(None) == {
        "ec": {"replica_factor": None, "ec_k": 4, "ec_m": 2},
    }


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
    with db.SessionLocal() as session:
        state = session.query(CapacityAlertState).filter_by(
            cluster_id=default_cluster_id, entity_type="cluster", entity_name="cluster",
        ).one()
        assert state.status == "OPEN"
        assert state.notification_count == 3


def test_failed_delivery_is_retried_and_recovery_is_sent(dashboard_client, default_cluster_id, monkeypatch):
    monkeypatch.setattr(subject.settings, "capacity_forecast_notification_cooldown_seconds", 300)
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
