"""Small in-process API timing registry for bounded admin diagnostics."""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock

_lock = Lock()
_totals = {
    "requests_total": 0,
    "errors_total": 0,
}
_by_route: dict[str, dict[str, float | int]] = defaultdict(
    lambda: {"requests_total": 0, "errors_total": 0, "last_duration_ms": 0.0}
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
        route_metrics["last_duration_ms"] = round(max(0.0, duration_ms), 2)
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
        return {
            **_totals,
            "by_route": {path: dict(values) for path, values in _by_route.items()},
            "recent": list(_recent),
        }
