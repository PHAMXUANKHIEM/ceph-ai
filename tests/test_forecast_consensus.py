import pytest

from shared.forecast_consensus import (
    ConsensusStatus,
    aggregate_forecasts,
)


def test_consensus_uses_median_and_agreement_interval():
    result = aggregate_forecasts([90.0, 91.0, 89.5, 150.0])

    assert result.status == ConsensusStatus.CONSENSUS.value
    assert result.value == 90.5
    assert result.agreeing_count == 3
    assert result.candidate_count == 4
    assert result.lower == 89.5
    assert result.upper == 91.0


def test_consensus_rejects_disagreement():
    result = aggregate_forecasts(
        [50.0, 70.0, 90.0],
        absolute_tolerance=2.0,
        relative_tolerance=0.0,
    )

    assert result.status == ConsensusStatus.LOW_CONFIDENCE.value
    assert result.ratio < 0.67
    assert not result.usable


def test_consensus_requires_minimum_candidates():
    result = aggregate_forecasts([50.0], minimum_candidates=2)

    assert result.status == ConsensusStatus.INSUFFICIENT_CANDIDATES.value
    assert not result.usable


@pytest.mark.parametrize(
    "kwargs",
    [
        {"minimum_candidates": 0},
        {"minimum_ratio": 0},
        {"minimum_ratio": 2},
        {"absolute_tolerance": -1},
        {"relative_tolerance": -1},
    ],
)
def test_consensus_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        aggregate_forecasts([1.0, 1.0], **kwargs)

