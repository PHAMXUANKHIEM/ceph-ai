import pytest

from shared.online_learning import RiverMeanLearner
from shared.online_model_backend import (
    BackendMetadata,
    OnlineModelBackend,
    metadata_for_backend,
)


def test_river_backend_exposes_metadata_and_bounded_resource_evidence():
    learner = RiverMeanLearner()

    assert isinstance(learner, OnlineModelBackend)
    metadata = metadata_for_backend(learner)
    assert metadata == BackendMetadata(
        backend_name="river",
        backend_version="0.25.0",
        algorithm="river_mean",
        model_version="river-mean-v1",
        feature_schema="scalar-v1",
    )
    learner.learn_one(42.0)
    assert learner.resource_cost()["state_bytes"] > 0
    assert learner.snapshot()["sample_count"] == 1


def test_backend_metadata_rejects_unknown_schema_or_version():
    class InvalidBackend(RiverMeanLearner):
        backend_version = "unknown"

    with pytest.raises(ValueError, match="unknown schema/version"):
        metadata_for_backend(InvalidBackend())
