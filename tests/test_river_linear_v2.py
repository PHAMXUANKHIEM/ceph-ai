import math

import pytest

from shared.online_model_backend import metadata_for_backend
from shared.online_model_registry import (
    create_online_model, get_online_model_registration, registry_snapshot,
)
from shared.river_linear_v2 import RiverLinearV2


FEATURES = {"current": 10.0, "lag_1": 9.0, "rolling_mean_6": 8.0}


def test_registered_river_linear_v2_uses_scaler_and_linear_regression():
    learner = create_online_model("river_linear_v2")
    assert isinstance(learner, RiverLinearV2)
    assert "StandardScaler" in repr(learner._model)
    assert "LinearRegression" in repr(learner._model)
    metadata = metadata_for_backend(learner)
    assert metadata.algorithm == "river_linear_v2"
    assert metadata.model_version == "river-linear-v2"
    assert get_online_model_registration("river_linear_v2").execution_mode == "SHADOW_ONLY"
    assert registry_snapshot()[0]["algorithm"] == "river_linear_v2"


def test_model_refuses_unverified_learning_and_accepts_verified_outcomes():
    learner = RiverLinearV2()
    with pytest.raises(ValueError, match="verified outcomes"):
        learner.learn_one(FEATURES, 12.0)
    with pytest.raises(ValueError, match="verified outcomes"):
        learner.learn_one(FEATURES, 12.0, outcome="PENDING")
    learner.learn_one(FEATURES, 12.0, outcome="VERIFIED_SUCCESS")
    learner.learn_verified_one(FEATURES, 13.0, outcome="VERIFIED_FAILED")
    assert learner.sample_count == 2
    assert math.isfinite(learner.predict_one(FEATURES))
    assert learner.score_one(FEATURES, 12.5).absolute_error >= 0


def test_json_snapshot_round_trip_and_checksum_fail_closed():
    learner = RiverLinearV2()
    learner.learn_one(FEATURES, 12.0, outcome="VERIFIED_SUCCESS")
    snapshot = learner.snapshot()
    assert snapshot["checksum"]
    restored = RiverLinearV2.from_snapshot(snapshot)
    assert restored.snapshot() == snapshot
    assert restored.predict_one(FEATURES) == learner.predict_one(FEATURES)
    assert restored.resource_usage() == restored.resource_cost()
    blank = RiverLinearV2()
    assert blank.restore(snapshot) is blank
    assert blank.snapshot() == snapshot

    tampered = dict(snapshot)
    tampered["sample_count"] = 999
    with pytest.raises(ValueError, match="checksum mismatch"):
        RiverLinearV2.from_snapshot(tampered)
    bad_version = dict(snapshot)
    bad_version["version"] = "river-linear-v1"
    with pytest.raises(ValueError, match="checksum mismatch"):
        RiverLinearV2.from_snapshot(bad_version)


def test_schema_and_input_validation_are_bounded():
    with pytest.raises(ValueError):
        RiverLinearV2(feature_names=("x", "x"))
    learner = RiverLinearV2()
    with pytest.raises(ValueError, match="feature keys"):
        learner.predict_one({"current": 1.0})
    with pytest.raises(ValueError, match="finite"):
        learner.predict_one({"current": float("nan"), "lag_1": 1.0, "rolling_mean_6": 1.0})


def test_sequential_replay_scores_before_learning_and_never_reads_future_rows():
    learner = RiverLinearV2()
    rows = [
        ({"current": 10.0, "lag_1": 9.0, "rolling_mean_6": 8.0}, 11.0),
        ({"current": 11.0, "lag_1": 10.0, "rolling_mean_6": 9.0}, 12.0),
        ({"current": 12.0, "lag_1": 11.0, "rolling_mean_6": 10.0}, 13.0),
    ]

    predictions = []
    for features, actual in rows:
        predictions.append(learner.predict_one(features))
        learner.learn_one(features, actual, outcome="VERIFIED_SUCCESS")

    assert predictions[0] is None
    assert predictions[1] is not None
    assert predictions[2] is not None
    assert learner.sample_count == len(rows)
