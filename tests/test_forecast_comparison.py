import pytest

from shared.forecast_comparison import (
    PairedCase,
    alert_metrics,
    bootstrap_mae_delta,
    interval_coverage,
    paired_comparison,
    point_metrics,
)


def test_point_metrics_mae_rmse_smape_bias():
    metrics = point_metrics([(12.0, 10.0), (8.0, 10.0), (10.0, 10.0)])
    assert metrics["n"] == 3
    assert metrics["mae"] == pytest.approx(4 / 3, abs=1e-6)
    assert metrics["rmse"] == pytest.approx((8 / 3) ** 0.5, abs=1e-6)
    assert metrics["bias"] == 0.0
    assert metrics["smape"] == pytest.approx((200 * 2 / 22 + 200 * 2 / 18) / 3, abs=1e-6)
    assert point_metrics([])["mae"] is None


def test_alert_metrics_at_the_trigger_threshold():
    pairs = [(95.0, 96.0), (92.0, 70.0), (60.0, 93.0), (50.0, 40.0)]
    assert alert_metrics(pairs, 90.0) == {"alert_volume": 2, "false_positive_rate": 0.5, "missed_alerts": 1}
    assert alert_metrics([(10.0, 10.0)], 90.0)["false_positive_rate"] is None


def test_interval_coverage_ignores_missing_intervals():
    assert interval_coverage([(5.0, 15.0, 10.0), (5.0, 8.0, 10.0), (None, None, 1.0)]) == 0.5
    assert interval_coverage([(None, None, 1.0)]) is None


def test_bootstrap_is_deterministic_and_brackets_the_delta():
    cases = [PairedCase(actual=50.0, champion=50.0 + (i % 5), candidate=50.0 + (i % 5) / 2) for i in range(40)]
    first = bootstrap_mae_delta(cases, seed=7)
    assert first == bootstrap_mae_delta(cases, seed=7)
    assert first["ci_low"] <= first["delta"] <= first["ci_high"]
    assert first["delta"] < 0


def test_paired_comparison_verdicts():
    better = [PairedCase(actual=50.0, champion=50.0 + 4 + (i % 3), candidate=50.0 + (i % 2)) for i in range(60)]
    report = paired_comparison(better, threshold=90.0, resamples=500)
    assert report["paired_cases"] == 60
    assert report["verdict"] == "CANDIDATE_BETTER"
    assert report["candidate"]["interval_coverage"] is None

    worse = [PairedCase(actual=50.0, champion=50.0, candidate=58.0 + (i % 3)) for i in range(60)]
    assert paired_comparison(worse, threshold=90.0, resamples=500)["verdict"] == "CHAMPION_BETTER"

    mixed = [PairedCase(actual=50.0, champion=51.0, candidate=49.0 + 2 * (i % 2)) for i in range(4)]
    assert paired_comparison(mixed, threshold=90.0, resamples=500)["verdict"] == "INCONCLUSIVE"
    empty = paired_comparison([], threshold=90.0)
    assert empty["verdict"] == "INCONCLUSIVE" and empty["champion"]["mae"] is None


def test_champion_interval_coverage_is_reported():
    cases = [PairedCase(actual=10.0, champion=11.0, candidate=10.5, champion_low=8.0, champion_high=12.0),
             PairedCase(actual=20.0, champion=11.0, candidate=19.0, champion_low=8.0, champion_high=12.0)]
    assert paired_comparison(cases, threshold=90.0)["champion"]["interval_coverage"] == 0.5
