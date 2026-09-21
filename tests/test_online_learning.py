import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from shared.db import Base
from shared.online_learning import (
    RiverMeanLearner,
    guarded_update,
    load_or_reset_state,
    save_state,
)


class Decision:
    def __init__(self, *, shadow, active, reason="blocked"):
        self.can_update_shadow = shadow
        self.can_update_active = active
        self.reason = reason


def test_river_mean_learns_incrementally_without_heavy_batch_state():
    learner = RiverMeanLearner()
    assert learner.predict_one(50.0) == 50.0
    learner.learn_one(40)
    learner.learn_one(60)
    assert learner.sample_count == 2
    assert learner.predict_one() == 50.0
    assert learner.snapshot() == {
        "schema_version": 1,
        "algorithm": "river_mean",
        "version": "river-mean-v1",
        "sample_count": 2,
        "mean": 50.0,
    }


def test_snapshot_round_trip_is_json_safe():
    learner = RiverMeanLearner()
    learner.learn_one(10)
    learner.learn_one(30)
    restored = RiverMeanLearner.from_snapshot(learner.snapshot())
    assert restored.sample_count == 2
    assert restored.value == 20.0


def test_replay_of_same_input_and_snapshot_is_deterministic():
    values = [10.0, 20.0, 30.0, 40.0]
    first = RiverMeanLearner()
    second = RiverMeanLearner()
    for value in values:
        first.learn_one(value)
        second.learn_one(value)
    assert first.snapshot() == second.snapshot()
    assert first.predict_one() == second.predict_one()


def test_legacy_snapshot_without_schema_version_is_migrated_in_memory():
    legacy = {
        "algorithm": "river_mean",
        "version": "river-mean-v1",
        "sample_count": 2,
        "mean": 20.0,
    }
    restored = RiverMeanLearner.from_snapshot(legacy)
    assert restored.sample_count == 2
    assert restored.value == 20.0
    assert restored.snapshot()["schema_version"] == 1


def test_guarded_update_respects_audit_shadow_and_active_modes():
    learner = RiverMeanLearner()
    audit = guarded_update(learner, 90, Decision(shadow=False, active=False, reason="audit-only"))
    assert audit.applied is False
    assert learner.sample_count == 0

    shadow = guarded_update(learner, 90, Decision(shadow=True, active=False))
    assert shadow.applied is True
    assert learner.sample_count == 1

    active = guarded_update(learner, 100, Decision(shadow=True, active=True), target="active")
    assert active.applied is True
    assert learner.sample_count == 2


def test_invalid_values_and_snapshots_fail_closed():
    learner = RiverMeanLearner()
    with pytest.raises(ValueError):
        learner.learn_one(float("nan"))
    with pytest.raises(ValueError):
        RiverMeanLearner.from_snapshot({"algorithm": "other", "version": "x"})
    with pytest.raises(ValueError):
        RiverMeanLearner.from_snapshot({
            "algorithm": "river_mean", "version": "river-mean-v1",
            "sample_count": 1, "mean": None,
        })
    with pytest.raises(ValueError):
        RiverMeanLearner.from_snapshot({
            "schema_version": 1,
            "algorithm": "river_mean",
            "version": "river-mean-v1",
            "sample_count": 0,
        })
    with pytest.raises(ValueError):
        RiverMeanLearner.from_snapshot({
            "schema_version": 99,
            "algorithm": "river_mean",
            "version": "river-mean-v1",
            "sample_count": 0,
            "mean": None,
        })


def test_state_round_trip_is_durable_and_scoped():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        learner = RiverMeanLearner()
        learner.learn_one(10)
        save_state(session, learner, cluster_id="cluster-a", host="node-1", metric="cpu")
        session.commit()
        restored, row = load_or_reset_state(
            session, cluster_id="cluster-a", host="node-1", metric="cpu",
        )
        assert row is not None
        assert restored.sample_count == 1
        assert restored.value == 10.0
        other, missing = load_or_reset_state(
            session, cluster_id="cluster-b", host="node-1", metric="cpu",
        )
        assert missing is None
        assert other.sample_count == 0


def test_corrupt_state_resets_to_empty_baseline():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        learner = RiverMeanLearner()
        learner.learn_one(99)
        row = save_state(session, learner, cluster_id=None, host="node-1", metric="ram")
        session.commit()
        row.state_json = "{broken"
        session.commit()
        restored, same_row = load_or_reset_state(
            session, cluster_id=None, host="node-1", metric="ram",
        )
        assert same_row is row
        assert restored.sample_count == 0
        assert row.sample_count == 0
        session.commit()
        restored_again, _ = load_or_reset_state(
            session, cluster_id=None, host="node-1", metric="ram",
        )
        assert restored_again.sample_count == 0
