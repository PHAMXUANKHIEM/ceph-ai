from datetime import datetime, timedelta

import pytest

from config.settings import settings
from shared.models import (
    Cluster, VolumeEarlyForecast, VolumeForecastRun, VolumeMetric, VolumeModelState,
)
from watcher import volume_learning as learning


NOW = datetime(2026, 8, 25, 3, 15)


@pytest.fixture(autouse=True)
def clear_attempt_cache():
    learning._last_attempt_bucket.clear()
    yield
    learning._last_attempt_bucket.clear()


def _cluster(db_session):
    row = Cluster(
        name="volume-learning", ceph_mon_nodes="", is_active=True, is_default=False,
        ssh_user="root", ssh_key_path="/key", ceph_exec_mode="none",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _history(db_session, cluster_id, *, hours=80):
    for offset in range(hours, -1, -1):
        timestamp = NOW - timedelta(hours=offset)
        db_session.add(VolumeMetric(
            cluster_id=cluster_id, pool="vms", image="disk-1",
            iops=100 + timestamp.hour, read_latency_ms=1 + timestamp.hour / 100,
            write_latency_ms=2 + timestamp.hour / 100, saturated=False,
            polled_at=timestamp,
        ))
    db_session.flush()


def _sample(iops=120, read=1.2, write=2.2):
    return {
        "pool": "vms", "image": "disk-1", "iops": iops,
        "read_latency_ms": read, "write_latency_ms": write,
    }


def test_observe_records_hourly_candidates_for_each_volume_metric(db_session, monkeypatch):
    cluster = _cluster(db_session)
    _history(db_session, cluster.id)
    monkeypatch.setattr(settings, "volume_learning_enabled", True)
    monkeypatch.setattr(settings, "volume_learning_min_samples", 24)
    monkeypatch.setattr(settings, "volume_learning_candidate_hours", "24,72")

    learning.observe_sample(db_session, cluster.id, _sample(), NOW)
    db_session.commit()

    runs = db_session.query(VolumeForecastRun).order_by(
        VolumeForecastRun.metric, VolumeForecastRun.window_hours
    ).all()
    assert {(run.metric, run.window_hours) for run in runs} == {
        (metric, window) for metric in learning.METRICS for window in (24, 72)
    }
    assert all(run.status == "PENDING" for run in runs)
    assert all(run.training_samples >= 3 for run in runs)
    assert {run.seasonal_scope for run in runs} <= {"hour_of_day", "all_history"}


def test_observe_records_fail_closed_early_forecasts_for_all_horizons(db_session, monkeypatch):
    cluster = _cluster(db_session)
    _history(db_session, cluster.id)
    monkeypatch.setattr(settings, "volume_learning_min_samples", 24)
    monkeypatch.setattr(settings, "volume_learning_candidate_hours", "72")
    monkeypatch.setattr(settings, "volume_forecast_enabled", True)
    monkeypatch.setattr(settings, "volume_forecast_horizons", "1,6,24")
    monkeypatch.setattr(settings, "volume_forecast_latency_slo_ms", 1.0)
    monkeypatch.setattr(settings, "volume_forecast_min_confidence", 0.0)

    learning.observe_sample(db_session, cluster.id, _sample(), NOW)
    db_session.commit()

    rows = db_session.query(VolumeEarlyForecast).all()
    assert len(rows) == 9
    assert {row.horizon_hours for row in rows} == {1, 6, 24}
    assert {row.model_version for row in rows} == {"seasonal-trend-v1"}
    assert all(row.training_samples >= 3 for row in rows)
    assert all(row.source_latest_at <= row.generated_at for row in rows)
    iops = [row for row in rows if row.metric == "iops"]
    assert {row.status for row in iops} == {"NO_THRESHOLD"}
    assert all(row.threshold_value is None for row in iops)
    latency = [row for row in rows if row.metric != "iops"]
    assert {row.status for row in latency} == {"WARNING"}


def test_early_forecast_is_hourly_idempotent(db_session, monkeypatch):
    cluster = _cluster(db_session)
    _history(db_session, cluster.id)
    monkeypatch.setattr(settings, "volume_learning_min_samples", 24)
    monkeypatch.setattr(settings, "volume_learning_candidate_hours", "72")
    monkeypatch.setattr(settings, "volume_forecast_horizons", "1,6,24")

    learning.observe_sample(db_session, cluster.id, _sample(), NOW)
    learning._last_attempt_bucket.clear()
    learning.observe_sample(db_session, cluster.id, _sample(), NOW + timedelta(minutes=20))
    db_session.commit()

    assert db_session.query(VolumeEarlyForecast).count() == 9


def test_early_forecast_is_created_when_baseline_candidates_already_exist(db_session, monkeypatch):
    cluster = _cluster(db_session)
    _history(db_session, cluster.id)
    monkeypatch.setattr(settings, "volume_learning_min_samples", 24)
    monkeypatch.setattr(settings, "volume_learning_candidate_hours", "72")
    monkeypatch.setattr(settings, "volume_forecast_enabled", False)
    learning.observe_sample(db_session, cluster.id, _sample(), NOW)
    db_session.commit()

    monkeypatch.setattr(settings, "volume_forecast_enabled", True)
    learning._last_attempt_bucket.clear()
    learning.observe_sample(db_session, cluster.id, _sample(), NOW + timedelta(minutes=20))
    db_session.commit()

    assert db_session.query(VolumeEarlyForecast).count() == 9


def test_same_hour_is_idempotent(db_session, monkeypatch):
    cluster = _cluster(db_session)
    _history(db_session, cluster.id)
    monkeypatch.setattr(settings, "volume_learning_min_samples", 24)
    monkeypatch.setattr(settings, "volume_learning_candidate_hours", "24")

    learning.observe_sample(db_session, cluster.id, _sample(), NOW)
    learning.observe_sample(db_session, cluster.id, _sample(), NOW + timedelta(minutes=20))
    db_session.commit()

    assert db_session.query(VolumeForecastRun).count() == 3


def test_due_prediction_is_scored_and_updates_persistent_mae(db_session, monkeypatch):
    cluster = _cluster(db_session)
    _history(db_session, cluster.id)
    monkeypatch.setattr(settings, "volume_learning_min_samples", 24)
    monkeypatch.setattr(settings, "volume_learning_candidate_hours", "24")
    monkeypatch.setattr(settings, "volume_learning_evaluation_hours", 1)

    learning.observe_sample(db_session, cluster.id, _sample(), NOW)
    evaluated = learning.observe_sample(
        db_session, cluster.id, _sample(iops=130, read=1.4, write=2.4),
        NOW + timedelta(hours=1, minutes=1),
    )
    db_session.commit()

    assert evaluated == 3
    old_runs = db_session.query(VolumeForecastRun).filter(
        VolumeForecastRun.predicted_at == NOW
    ).all()
    assert all(run.status == "EVALUATED" for run in old_runs)
    states = db_session.query(VolumeModelState).all()
    assert len(states) == 3
    assert all(state.evaluated_count == 1 for state in states)
    assert all(state.mean_absolute_error is not None for state in states)


def test_lowest_mae_window_is_selected_after_minimum_outcomes(db_session, monkeypatch):
    cluster = _cluster(db_session)
    monkeypatch.setattr(settings, "volume_learning_min_outcomes", 3)
    better = VolumeModelState(
        cluster_id=cluster.id, pool="vms", image="disk-1", metric="iops",
        algorithm=learning.ALGORITHM, window_hours=24, evaluated_count=3,
        mean_absolute_error=2, mean_percentage_error=2,
    )
    worse = VolumeModelState(
        cluster_id=cluster.id, pool="vms", image="disk-1", metric="iops",
        algorithm=learning.ALGORITHM, window_hours=72, evaluated_count=3,
        mean_absolute_error=10, mean_percentage_error=10,
    )
    db_session.add_all((better, worse))
    db_session.flush()

    learning._select_models(db_session, cluster.id, "vms", "disk-1", "iops")

    assert better.selected is True
    assert worse.selected is False


def test_learning_is_disabled_and_skips_legacy_null_cluster(db_session, monkeypatch):
    monkeypatch.setattr(settings, "volume_learning_enabled", False)
    assert learning.observe_sample(db_session, "cluster", _sample(), NOW) == 0
    monkeypatch.setattr(settings, "volume_learning_enabled", True)
    assert learning.observe_sample(db_session, None, _sample(), NOW) == 0
    assert db_session.query(VolumeForecastRun).count() == 0
