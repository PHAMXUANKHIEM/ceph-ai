"""Loki-backed CPU/RAM history and deterministic resource forecasting.

Loki is the source of truth: the live SSH sample is first pushed to Loki;
forecasting always queries the stored Loki stream back.  This keeps the
prediction path auditable in Grafana and prevents an in-process cache from
silently becoming a second metrics store.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config.settings import settings

logger = logging.getLogger(__name__)
JOB = "ceph-ai-node-metrics"


@dataclass(frozen=True)
class ResourceForecast:
    metric: str
    current_percent: float
    slope_percent_per_hour: float
    predicted_percent: float
    hours_to_90: float | None
    confidence: float
    samples: int
    window_hours: float


def _headers() -> dict[str, str]:
    return ({"X-Scope-OrgID": settings.log_intel_loki_tenant}
            if settings.log_intel_loki_tenant else {})


def _base_url() -> str:
    return (settings.log_intel_loki_url or "").rstrip("/")


def push_sample(cluster: str, host: str, metrics: dict, *, timestamp_ns: int | None = None) -> bool:
    """Push one structured sample to Loki.  Never breaks node monitoring."""
    if not settings.node_resource_forecast_enabled or not _base_url():
        return False
    import httpx

    line = json.dumps({
        "cpu_percent": float(metrics["cpu_percent"]),
        "mem_percent": float(metrics["mem_percent"]),
        "mem_used_mb": float(metrics.get("mem_used_mb") or 0),
        "mem_total_mb": float(metrics.get("mem_total_mb") or 0),
    }, separators=(",", ":"))
    payload = {"streams": [{"stream": {
        "job": JOB, "cluster": cluster or "default", "host": host,
        "metric_type": "node_resource",
    }, "values": [[str(timestamp_ns or time.time_ns()), line]]}]}
    try:
        response = httpx.post(f"{_base_url()}/loki/api/v1/push", json=payload,
                              headers=_headers(), timeout=settings.log_intel_loki_timeout_seconds)
        response.raise_for_status()
        return True
    except Exception:
        logger.warning("node forecast: cannot push sample for %s to Loki", host, exc_info=True)
        return False


def fetch_samples(cluster: str, host: str, *, now: datetime | None = None) -> list[tuple[datetime, float, float]]:
    """Read CPU/RAM samples from Loki, ordered and deduplicated by timestamp."""
    if not _base_url():
        return []
    import httpx

    end = now or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(days=max(1, settings.node_resource_forecast_history_days))
    selector = '{job="%s", cluster="%s", host="%s"}' % (
        JOB, cluster.replace('"', '\\"'), host.replace('"', '\\"'))
    params = {"query": selector, "start": str(int(start.timestamp() * 1e9)),
              "end": str(int(end.timestamp() * 1e9)), "limit": "5000", "direction": "forward"}
    response = httpx.get(f"{_base_url()}/loki/api/v1/query_range", params=params,
                         headers=_headers(), timeout=settings.log_intel_loki_timeout_seconds)
    response.raise_for_status()
    rows: dict[int, tuple[datetime, float, float]] = {}
    for stream in ((response.json().get("data") or {}).get("result") or []):
        for ts_ns, line in stream.get("values") or []:
            try:
                raw = json.loads(line)
                ts_int = int(ts_ns)
                rows[ts_int] = (datetime.fromtimestamp(ts_int / 1e9, timezone.utc),
                                float(raw["cpu_percent"]), float(raw["mem_percent"]))
            except (TypeError, ValueError, KeyError, json.JSONDecodeError):
                continue
    return [rows[key] for key in sorted(rows)]


def _linear_forecast(points: list[tuple[datetime, float]], metric: str) -> ResourceForecast | None:
    minimum = max(3, settings.node_resource_forecast_min_samples)
    if len(points) < minimum:
        return None
    origin = points[0][0]
    xs = [(ts - origin).total_seconds() / 3600 for ts, _ in points]
    ys = [value for _, value in points]
    window = xs[-1] - xs[0]
    if window < 6:
        return None
    x_mean, y_mean = sum(xs) / len(xs), sum(ys) / len(ys)
    denominator = sum((x - x_mean) ** 2 for x in xs)
    if denominator <= 0:
        return None
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys)) / denominator
    intercept = y_mean - slope * x_mean
    fitted = [intercept + slope * x for x in xs]
    ss_total = sum((y - y_mean) ** 2 for y in ys)
    ss_residual = sum((y - fit) ** 2 for y, fit in zip(ys, fitted))
    confidence = max(0.0, min(1.0, 1 - ss_residual / ss_total)) if ss_total > 0 else 0.0
    horizon = max(1, settings.node_resource_forecast_horizon_hours)
    predicted = max(0.0, min(100.0, intercept + slope * (xs[-1] + horizon)))
    hours_to_90 = None
    if slope > 0 and ys[-1] < 90:
        crossing = (90 - intercept) / slope
        if crossing >= xs[-1]:
            hours_to_90 = crossing - xs[-1]
    return ResourceForecast(metric, ys[-1], slope, predicted, hours_to_90,
                            confidence, len(points), window)


def forecast(cluster: str, host: str, *, now: datetime | None = None) -> dict[str, ResourceForecast]:
    samples = fetch_samples(cluster, host, now=now)
    result: dict[str, ResourceForecast] = {}
    for index, metric in ((1, "cpu"), (2, "ram")):
        value = _linear_forecast([(row[0], row[index]) for row in samples], metric)
        if value is not None:
            result[metric] = value
    return result


def risky_forecasts(values: dict[str, ResourceForecast]) -> list[ResourceForecast]:
    """Return credible threshold crossings inside the configured horizon."""
    return [value for value in values.values()
            if value.hours_to_90 is not None
            and value.hours_to_90 <= settings.node_resource_forecast_horizon_hours
            and value.confidence >= settings.node_resource_forecast_min_confidence
            and math.isfinite(value.hours_to_90)]
