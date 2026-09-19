from collections import defaultdict

from shared import api_observability


def test_api_route_metrics_cap_uses_bounded_overflow_bucket(monkeypatch):
    monkeypatch.setattr(
        api_observability,
        "_by_route",
        defaultdict(
            lambda: {"requests_total": 0, "errors_total": 0, "last_duration_ms": 0.0}
        ),
    )
    monkeypatch.setattr(api_observability, "_MAX_ROUTE_METRICS", 1)

    api_observability.record_request("GET", "/first", 200, 1, "trace-1")
    api_observability.record_request("GET", "/second", 200, 2, "trace-2")

    metrics = api_observability.get_metrics()
    assert set(metrics["by_route"]) == {"/first", "__other__"}
    assert metrics["by_route"]["__other__"]["requests_total"] == 1
