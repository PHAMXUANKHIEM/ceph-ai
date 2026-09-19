import math

from shared.forecast_metrics import outcome_metrics, update_rolling_metrics


def test_outcome_metrics_reports_signed_error_and_smape():
    metrics = outcome_metrics(12, 10)
    assert metrics["bias"] == 2
    assert metrics["absolute_error"] == 2
    assert metrics["squared_error"] == 4
    assert math.isclose(metrics["smape"], 18.181818, rel_tol=1e-6)


def test_rolling_metrics_is_bounded_and_aggregated():
    history, first = update_rolling_metrics(None, 12, 10, limit=2)
    history, second = update_rolling_metrics(history, 8, 10, limit=2)
    history, third = update_rolling_metrics(history, 20, 10, limit=2)

    assert first["count"] == 1
    assert second["count"] == 2
    assert third["count"] == 2
    assert math.isclose(third["mae"], 6.0)
    assert math.isclose(third["rmse"], math.sqrt(52), rel_tol=1e-6)
    assert third["bias"] == 4.0
