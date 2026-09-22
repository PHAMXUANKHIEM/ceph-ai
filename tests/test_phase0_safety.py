import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from shared.db import Base
from shared.forecast_flags import candidate_enabled, candidate_flags_snapshot
from shared.models import (
    Action, ForecastModelEvaluation, ForecastModelRegistry, Incident,
    NodeResourceForecastAlert, OnlineLearnerState, RemediationCase, TelegramOutbox,
)
from shared.river_linear_v2 import RiverLinearV2
from scripts.self_learning_baseline import write_immutable_report
from watcher.forecast_replay import replay_resource_forecasts


def test_candidate_flags_are_independent_and_fail_closed():
    flags = candidate_flags_snapshot(raw="linear=true,rolling_quantile=false,unknown=true")
    assert flags["linear"] is True
    assert flags["rolling_quantile"] is False
    assert candidate_enabled("unknown", raw="unknown=true") is False


def test_immutable_baseline_report_refuses_overwrite(tmp_path):
    path = tmp_path / "baseline.md"
    write_immutable_report(path, {"schema": "test", "read_only": True})
    with pytest.raises(FileExistsError):
        write_immutable_report(path, {"schema": "replacement"})


def test_shadow_replay_creates_no_incident_action_telegram_or_remediation(db_session, monkeypatch):
    monkeypatch.setattr("config.settings.settings.node_resource_forecast_min_samples", 6)
    origin = datetime(2026, 8, 1, tzinfo=timezone.utc)
    points = [(origin.replace(hour=hour), 50.0) for hour in range(12)]

    result = replay_resource_forecasts(points, "ram", horizon_hours=1, window_hours=[6])

    assert result["consensus"].evaluated > 0
    assert db_session.query(Incident).count() == 0
    assert db_session.query(Action).count() == 0
    assert db_session.query(TelegramOutbox).count() == 0
    assert db_session.query(NodeResourceForecastAlert).count() == 0
    assert db_session.query(RemediationCase).count() == 0


def test_snapshot_registry_and_evaluation_survive_session_reopen(tmp_path):
    database = create_engine(f"sqlite:///{tmp_path / 'restart.db'}")
    Base.metadata.create_all(database)
    learner = RiverLinearV2()
    learner.learn_one(
        {"current": 10.0, "lag_1": 9.0, "rolling_mean_6": 8.0},
        12.0,
        outcome="VERIFIED_SUCCESS",
    )
    snapshot = learner.snapshot()
    target = datetime(2026, 9, 22, tzinfo=timezone.utc).replace(tzinfo=None)
    with Session(database) as session:
        session.add(OnlineLearnerState(
            cluster_key="cluster-a", host="node-a", metric="cpu",
            model_version="river-linear-v2", algorithm="river_linear_v2",
            feature_schema="resource-v2", state_json=json.dumps(snapshot, sort_keys=True),
            state_checksum=snapshot["checksum"], sample_count=learner.sample_count,
        ))
        active = ForecastModelRegistry(
            scope_type="NODE_RESOURCE", scope_key="cluster-a|node-a|cpu|h24",
            scope_schema="forecast-scope-v1", cluster_id="cluster-a",
            entity_type="node", entity_id="node-a", host="node-a", metric="cpu",
            horizon_hours=24, name="node-resource-forecast", version="linear:24h",
            algorithm="linear", feature_schema="node-resource-v1",
            training_window_hours=24, status="ACTIVE",
        )
        candidate = ForecastModelRegistry(
            scope_type="NODE_RESOURCE", scope_key="cluster-a|node-a|cpu|h24",
            scope_schema="forecast-scope-v1", cluster_id="cluster-a",
            entity_type="node", entity_id="node-a", host="node-a", metric="cpu",
            horizon_hours=24, name="node-resource-forecast", version="river:24h",
            algorithm="river_linear_v2", feature_schema="node-resource-v1",
            training_window_hours=24, status="SHADOW",
        )
        session.add_all([active, candidate])
        session.flush()
        session.add(ForecastModelEvaluation(
            candidate_model_id=candidate.id, active_model_id=active.id,
            target_at=target, evaluated_at=target, active_evaluated=1,
            candidate_evaluated=1, active_mae=2.0, candidate_mae=1.0,
            active_rmse=2.0, candidate_rmse=1.0, active_smape=10.0,
            candidate_smape=5.0, active_bias=0.1, candidate_bias=0.0,
            status="PROMISING", reason="test evidence",
        ))
        session.commit()

    with Session(database) as session:
        state = session.query(OnlineLearnerState).one()
        assert json.loads(state.state_json)["checksum"] == snapshot["checksum"]
        assert session.query(ForecastModelRegistry).filter_by(status="SHADOW").count() == 1
        assert session.query(ForecastModelEvaluation).count() == 1
