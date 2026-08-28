from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.models import NodeResourceForecastAlert, NodeResourceForecastRun, NodeResourceModelState
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


def test_risky_forecast_rejects_poor_data_quality(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_confidence", 0.5)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_coverage", 0.8)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_max_gap_hours", 6.0)
    value = forecast.ResourceForecast(
        "ram", 80, 1, 95, 10, 0.9, 30, 29,
        coverage_ratio=0.5, max_gap_hours=1,
    )
    assert forecast.risky_forecasts({"ram": value}) == []
    value = forecast.ResourceForecast(
        "ram", 80, 1, 95, 10, 0.9, 30, 29,
        coverage_ratio=1.0, max_gap_hours=7,
    )
    assert forecast.risky_forecasts({"ram": value}) == []


def test_linear_forecast_reports_loki_gap_quality(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 6)
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    points = [(origin + timedelta(hours=index), 20 + index) for index in range(6)]
    points.append((origin + timedelta(hours=24), 44))
    result = forecast._linear_forecast(points, "cpu", training_window_hours=24)
    assert result is not None
    assert result.max_gap_hours == 19
    assert result.coverage_ratio == 1.0


def test_forecast_reads_loki_samples(monkeypatch):
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 24)
    samples = [(ts, value, value / 2) for ts, value in _points()]
    monkeypatch.setattr(forecast, "fetch_samples", lambda cluster, host, now=None: samples)
    result = forecast.forecast("CS-LAB", "node-1")
    assert set(result) == {"cpu", "ram"}
    assert result["cpu"].samples == 30


def test_latest_metrics_comes_from_newest_loki_sample(monkeypatch):
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        forecast, "fetch_samples",
        lambda cluster, host, now=None: [(now - timedelta(seconds=10), 42.5, 61.2)],
    )

    result = forecast.fetch_latest_metrics("CS-LAB", "node-1", now=now)

    assert result["cpu_percent"] == 42.5
    assert result["mem_percent"] == 61.2
    assert result["source"] == "loki"


def test_latest_metrics_rejects_stale_loki_sample(monkeypatch):
    now = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(
        forecast, "fetch_samples",
        lambda cluster, host, now=None: [(now - timedelta(minutes=10), 42.5, 61.2)],
    )

    try:
        forecast.fetch_latest_metrics("CS-LAB", "node-1", now=now, max_age_seconds=60)
    except forecast.NodeResourceLokiError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale Loki data must be rejected")


def _learning_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(forecast.db, "SessionLocal", factory)
    return factory


def test_adaptive_forecast_persists_candidates_and_selects_longest_during_warmup(monkeypatch):
    factory = _learning_db(monkeypatch)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 6)
    monkeypatch.setattr(forecast.settings, "node_resource_learning_candidate_hours", "24,72")
    monkeypatch.setattr(forecast.settings, "node_resource_learning_evaluation_hours", 24)
    samples = [(ts, value, value / 2) for ts, value in _points(count=80, slope=.1)]
    monkeypatch.setattr(forecast, "fetch_samples", lambda cluster, host, now=None: samples)

    result = forecast.adaptive_forecast("CS-LAB", "node-1", now=samples[-1][0])

    assert result["cpu"].training_window_hours == 72
    with factory() as session:
        assert session.query(NodeResourceForecastRun).count() == 4
        selected = session.query(NodeResourceModelState).filter_by(selected=True).all()
        assert {(row.metric, row.window_hours) for row in selected} == {("cpu", 72), ("ram", 72)}


def test_adaptive_forecast_evaluates_due_run_and_updates_mae(monkeypatch):
    factory = _learning_db(monkeypatch)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 6)
    monkeypatch.setattr(forecast.settings, "node_resource_learning_candidate_hours", "24")
    monkeypatch.setattr(forecast.settings, "node_resource_learning_evaluation_hours", 1)
    monkeypatch.setattr(forecast.settings, "node_resource_learning_min_outcomes", 1)
    samples = [(ts, value, value / 2) for ts, value in _points(count=30, slope=.1)]
    now = samples[-1][0]
    with factory() as session:
        session.add(NodeResourceForecastRun(
            cluster_name="CS-LAB", host="node-1", metric="cpu", algorithm="linear",
            window_hours=24, predicted_at=(now - timedelta(hours=2)).replace(tzinfo=None),
            target_at=(now - timedelta(hours=1)).replace(tzinfo=None), current_percent=20,
            predicted_percent=30, confidence=.8, status="PENDING", idempotency_key="due",
        ))
        session.commit()
    monkeypatch.setattr(forecast, "fetch_samples", lambda cluster, host, now=None: samples)

    forecast.adaptive_forecast("CS-LAB", "node-1", now=now)

    with factory() as session:
        run = session.query(NodeResourceForecastRun).filter_by(idempotency_key="due").one()
        state = session.query(NodeResourceModelState).filter_by(metric="cpu", window_hours=24).one()
        assert run.status == "EVALUATED"
        assert run.actual_percent == samples[-2][1]
        assert state.evaluated_count == 1
        assert state.mean_absolute_error == run.absolute_error


def test_adaptive_forecast_scores_sample_near_target_time(monkeypatch):
    factory = _learning_db(monkeypatch)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_samples", 6)
    monkeypatch.setattr(forecast.settings, "node_resource_learning_candidate_hours", "24")
    origin = datetime(2026, 8, 24, tzinfo=timezone.utc)
    samples = [(origin + timedelta(hours=index), 20 + index, 40 + index) for index in range(11)]
    now = samples[-1][0]
    with factory() as session:
        session.add(NodeResourceForecastRun(
            cluster_name="CS-LAB", host="node-1", metric="cpu", algorithm="linear",
            window_hours=24, predicted_at=(now - timedelta(hours=7)).replace(tzinfo=None),
            target_at=(now - timedelta(hours=5)).replace(tzinfo=None), current_percent=20,
            predicted_percent=25, confidence=.8, status="PENDING", idempotency_key="target-time",
        ))
        session.commit()
    monkeypatch.setattr(forecast, "fetch_samples", lambda cluster, host, now=None: samples)

    forecast.adaptive_forecast("CS-LAB", "node-1", now=now)

    with factory() as session:
        run = session.query(NodeResourceForecastRun).filter_by(idempotency_key="target-time").one()
        assert run.status == "EVALUATED"
        assert run.actual_percent == 25  # sample at target, not latest value 30


def test_late_outcome_is_marked_unmeasurable_not_scored(monkeypatch):
    factory = _learning_db(monkeypatch)
    monkeypatch.setattr(forecast.settings, "node_resource_learning_max_outcome_gap_hours", 3.0)
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    with factory() as session:
        session.add(NodeResourceForecastRun(
            cluster_name="CS-LAB", host="node-1", metric="cpu", algorithm="linear",
            window_hours=24, predicted_at=(now - timedelta(hours=12)).replace(tzinfo=None),
            target_at=(now - timedelta(hours=8)).replace(tzinfo=None), current_percent=20,
            predicted_percent=25, confidence=.8, status="PENDING", idempotency_key="too-late",
        ))
        session.commit()

    count = forecast.evaluate_due_outcomes("CS-LAB", "node-1", 80, 40, observed_at=now)

    assert count == 1
    with factory() as session:
        run = session.query(NodeResourceForecastRun).filter_by(idempotency_key="too-late").one()
        assert run.status == "UNMEASURABLE"
        assert run.actual_percent is None
        assert session.query(NodeResourceModelState).count() == 0


def test_open_alert_becomes_data_quality_not_resolved(monkeypatch):
    factory = _learning_db(monkeypatch)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_min_coverage", .8)
    monkeypatch.setattr(forecast.settings, "node_resource_forecast_max_gap_hours", 6.0)
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    with factory() as session:
        session.add(NodeResourceForecastAlert(
            cluster_name="CS-LAB", host="node-1", metric="ram", status="OPEN",
            first_detected_at=now.replace(tzinfo=None), last_detected_at=now.replace(tzinfo=None),
            current_percent=80, predicted_percent=95, hours_to_90=10,
            confidence=.9, samples=30, window_hours=24,
        ))
        session.commit()
    poor = forecast.ResourceForecast(
        "ram", 80, 1, 95, 10, .9, 30, 20,
        training_window_hours=24, coverage_ratio=.5, max_gap_hours=1,
    )
    forecast.sync_forecast_alerts("CS-LAB", "node-1", {"ram": poor}, now=now)
    with factory() as session:
        alert = session.query(NodeResourceForecastAlert).one()
        assert alert.status == "DATA_QUALITY"
        assert alert.resolved_at is None


def test_direct_observation_evaluates_due_cpu_and_ram(monkeypatch):
    factory = _learning_db(monkeypatch)
    now = datetime(2026, 8, 24, 12, tzinfo=timezone.utc)
    with factory() as session:
        for metric, predicted in (("cpu", 30), ("ram", 40)):
            session.add(NodeResourceForecastRun(
                cluster_name="CS-LAB", host="node-1", metric=metric, algorithm="linear",
                window_hours=24, predicted_at=(now - timedelta(hours=25)).replace(tzinfo=None),
                target_at=(now - timedelta(hours=1)).replace(tzinfo=None), current_percent=20,
                predicted_percent=predicted, confidence=.8, status="PENDING",
                idempotency_key="due-" + metric,
            ))
        session.commit()
    count = forecast.evaluate_due_outcomes("CS-LAB", "node-1", 32, 39, observed_at=now)
    assert count == 2
    with factory() as session:
        rows = session.query(NodeResourceForecastRun).order_by(NodeResourceForecastRun.metric).all()
        assert all(row.status == "EVALUATED" for row in rows)
        assert {row.metric: row.absolute_error for row in rows} == {"cpu": 2, "ram": 1}
