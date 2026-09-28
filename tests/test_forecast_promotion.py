from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.forecast_promotion import (
    HealthVerification,
    PairedSample,
    PromotionConfig,
    append_promotion_audit,
    approve_promotion,
    evaluate_paired,
    persist_evaluation,
    pair_forecast_runs,
    persisted_pass_streak,
    promotion_gate,
    promote_model,
    verify_active_health,
)
from shared.models import (
    ForecastModelRegistry,
    ForecastPromotionAudit,
)


def _session(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr("shared.db.SessionLocal", factory)
    return factory


def _pairs(count=6, *, challenger_error=0.0, quality="OK", drift=False):
    origin = datetime(2026, 9, 22, 0, 0)
    return [
        PairedSample(
            input_window_id=f"window-{index}",
            input_window_start=origin + timedelta(hours=index - 24),
            input_window_end=origin + timedelta(hours=index),
            target_at=origin + timedelta(hours=index + 1),
            actual=100.0,
            champion_prediction=95.0,
            challenger_prediction=100.0 - challenger_error,
            quality_status=quality,
            champion_lower=90.0,
            champion_upper=110.0,
            challenger_lower=90.0,
            challenger_upper=110.0,
            event_observed=True,
            champion_detected_at=origin + timedelta(hours=index + 1),
            challenger_detected_at=origin + timedelta(hours=index + 1),
            drift=drift,
        )
        for index in range(count)
    ]


def _evaluation(samples, *, now=None):
    return evaluate_paired(
        samples,
        cluster_name="CS-LAB",
        host="10.20.1.153",
        metric="cpu",
        horizon_hours=1,
        champion_version="linear:24h:v1",
        challenger_version="river:v2",
        now=now or datetime(2026, 9, 22, 12, 0),
        ttl_hours=72,
    )


def test_paired_evaluation_computes_required_metrics_and_shared_provenance():
    result = _evaluation(_pairs())

    assert result.paired_count == 6
    assert result.input_window_start == datetime(2026, 9, 21, 0, 0)
    assert result.input_window_end == datetime(2026, 9, 22, 5, 0)
    assert result.target_start == datetime(2026, 9, 22, 1, 0)
    assert result.challenger.mae == 0
    assert result.challenger.rmse == 0
    assert result.challenger.smape == 0
    assert result.challenger.bias == 0
    assert result.challenger.p95_abs_error == 0
    assert result.challenger.interval_coverage == 1
    assert result.challenger.recall == 1
    assert result.challenger.delay_seconds == 0
    assert len(result.evidence_hash) == 64


def test_persisted_runs_are_paired_by_input_window_and_target_timestamp():
    origin = datetime(2026, 9, 22, 0, 0)
    common = dict(
        predicted_at=origin,
        window_hours=24,
        target_at=origin + timedelta(hours=1),
        actual_percent=100.0,
        status="EVALUATED",
        drift_status="OK",
        promotion_blocked=False,
    )
    champion = [SimpleNamespace(**common, predicted_percent=95.0)]
    challenger = [SimpleNamespace(**common, predicted_percent=100.0)]
    pairs = pair_forecast_runs(champion, challenger)

    assert len(pairs) == 1
    assert pairs[0].input_window_start == origin - timedelta(hours=24)
    assert pairs[0].quality_status == "OK"

    unpaired = pair_forecast_runs(champion, [])
    assert unpaired[0].quality_status == "UNPAIRED"


def test_quality_drift_and_missing_pairs_are_not_hidden():
    result = _evaluation(_pairs(4, quality="STALE", drift=True))

    assert result.quality_status == "PARTIAL_QUALITY"
    assert result.challenger.sample_count == 0
    assert result.drift_count == 4
    gate = promotion_gate(
        result,
        config=PromotionConfig(minimum_samples=1, minimum_evaluation_streak=1),
        prior_pass_streak=0,
        now=datetime(2026, 9, 22, 12, 0),
    )
    assert gate.passed is False
    assert any("quality" in reason for reason in gate.reasons)
    assert any("drift" in reason for reason in gate.reasons)


def test_gate_is_per_scope_and_requires_sample_and_streak():
    result = _evaluation(_pairs(6))
    gate = promotion_gate(
        result,
        config=PromotionConfig(minimum_samples=6, minimum_evaluation_streak=3),
        prior_pass_streak=1,
        now=datetime(2026, 9, 22, 12, 0),
    )
    assert gate.passed is False
    assert gate.evaluation_streak == 2
    assert any("streak" in reason for reason in gate.reasons)

    passed = promotion_gate(
        result,
        config=PromotionConfig(minimum_samples=6, minimum_evaluation_streak=3),
        prior_pass_streak=2,
        now=datetime(2026, 9, 22, 12, 0),
    )
    assert passed.passed is True
    assert passed.scope == ("CS-LAB", "10.20.1.153", "cpu", 1)


def test_operator_approval_is_bound_to_evidence_scope_version_and_expiry(monkeypatch):
    factory = _session(monkeypatch)
    now = datetime(2026, 9, 22, 12, 0)
    result = _evaluation(_pairs(), now=now)
    gate = promotion_gate(
        result,
        config=PromotionConfig(minimum_samples=1, minimum_evaluation_streak=1),
        now=now,
    )
    with factory() as session:
        evidence = persist_evaluation(session, result, passed=gate.passed)
        assert persisted_pass_streak(session, evaluation=result, now=now) == 1
        with pytest.raises(ValueError, match="expiry"):
            approve_promotion(
                session, gate=gate, evidence_id=evidence.id, actor="admin",
                expires_at=result.expires_at + timedelta(seconds=1), now=now,
            )
        approval = approve_promotion(
            session, gate=gate, evidence_id=evidence.id, actor="admin",
            expires_at=now + timedelta(hours=24), now=now,
        )
        session.commit()
        assert approval.status == "APPROVED"
        assert session.query(ForecastPromotionAudit).count() == 1


def test_promotion_health_failure_rolls_back_last_known_good(monkeypatch):
    factory = _session(monkeypatch)
    monkeypatch.setattr("config.settings.settings.forecast_promotion_enabled", True)
    now = datetime(2026, 9, 22, 12, 0)
    result = _evaluation(_pairs(), now=now)
    gate = promotion_gate(
        result,
        config=PromotionConfig(minimum_samples=1, minimum_evaluation_streak=1),
        now=now,
    )
    with factory() as session:
        evidence = persist_evaluation(session, result, passed=True)
        approval = approve_promotion(
            session, gate=gate, evidence_id=evidence.id, actor="admin",
            expires_at=now + timedelta(hours=24), now=now,
        )
        session.add_all([
            ForecastModelRegistry(
                id="champion", cluster_name="CS-LAB", host="10.20.1.153", metric="cpu",
                horizon_hours=1, version="linear:24h:v1", algorithm="linear", status="ACTIVE",
            ),
            ForecastModelRegistry(
                id="challenger", cluster_name="CS-LAB", host="10.20.1.153", metric="cpu",
                horizon_hours=1, version="river:v2", algorithm="river", status="SHADOW",
            ),
        ])
        promoted = promote_model(session, approval_id=approval.id, actor="admin", now=now)
        assert promoted.status == "ACTIVE"
        rolled_back = verify_active_health(
            session,
            scope=("CS-LAB", "10.20.1.153", "cpu", 1),
            version="river:v2",
            verification=HealthVerification(False, "SOURCE_ERROR", "health endpoint failed", now),
            actor="system",
        )
        session.commit()

        assert rolled_back.version == "linear:24h:v1"
        assert session.query(ForecastPromotionAudit).count() == 4
        assert session.query(ForecastModelRegistry).filter_by(status="ACTIVE").one().version == "linear:24h:v1"
