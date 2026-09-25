"""Small in-process API timing registry for bounded admin diagnostics."""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock

_lock = Lock()
_totals = {
    "requests_total": 0,
    "errors_total": 0,
}
_DURATION_BUCKETS = (50, 100, 250, 500, 1000, 2000, 5000, 10000, 30000)
_by_route: dict[str, dict[str, float | int]] = defaultdict(
    lambda: {
        "requests_total": 0,
        "errors_total": 0,
        "last_duration_ms": 0.0,
        "duration_buckets": {str(bound): 0 for bound in _DURATION_BUCKETS},
    }
)
_recent: deque[dict[str, object]] = deque(maxlen=200)
_MAX_ROUTE_METRICS = 500
_ROUTE_OVERFLOW = "__other__"


def record_request(method: str, path: str, status_code: int, duration_ms: float, request_id: str) -> None:
    """Record only route, status, duration and correlation ID—never query data."""
    route = path[:200] or "/"
    with _lock:
        if route not in _by_route and len(_by_route) >= _MAX_ROUTE_METRICS:
            route = _ROUTE_OVERFLOW
        _totals["requests_total"] += 1
        if status_code >= 500:
            _totals["errors_total"] += 1
        route_metrics = _by_route[route]
        route_metrics["requests_total"] += 1
        if status_code >= 500:
            route_metrics["errors_total"] += 1
        duration = round(max(0.0, duration_ms), 2)
        route_metrics["last_duration_ms"] = duration
        buckets = route_metrics.setdefault(
            "duration_buckets", {str(bound): 0 for bound in _DURATION_BUCKETS}
        )
        for bound in _DURATION_BUCKETS:
            if duration <= bound:
                buckets[str(bound)] = int(buckets.get(str(bound), 0)) + 1
                break
        _recent.append(
            {
                "request_id": request_id,
                "method": method[:12],
                "path": route,
                "status_code": int(status_code),
                "duration_ms": round(max(0.0, duration_ms), 2),
            }
        )


def get_metrics() -> dict[str, object]:
    with _lock:
        def p95(buckets: dict[str, int], total: int) -> float | None:
            if total <= 0:
                return None
            target = max(1, int((total * 95 + 99) // 100))
            cumulative = 0
            for bound in _DURATION_BUCKETS:
                cumulative += int(buckets.get(str(bound), 0))
                if cumulative >= target:
                    return float(bound)
            return float(_DURATION_BUCKETS[-1])

        routes = {}
        for path, values in _by_route.items():
            row = dict(values)
            row["p95_duration_ms"] = p95(row["duration_buckets"], int(row["requests_total"]))
            routes[path] = row
        return {
            **_totals,
            "p95_duration_ms": p95(
                {
                    str(bound): sum(
                        int(values["duration_buckets"].get(str(bound), 0))
                        for values in _by_route.values()
                    )
                    for bound in _DURATION_BUCKETS
                },
                sum(
                    int(values["duration_buckets"].get(str(bound), 0))
                    for values in _by_route.values()
                    for bound in _DURATION_BUCKETS
                ),
            ),
            "by_route": routes,
            "recent": list(_recent),
        }
