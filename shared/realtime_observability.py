"""Low-cardinality telemetry for the snapshot/event path."""

from __future__ import annotations

from collections import defaultdict, deque
from threading import Lock
from time import time

_LOCK = Lock()
_MAX_CLUSTERS = 128
_MAX_QUERIES_PER_CLUSTER = 32
_MAX_RECENT = 300
_QUERY_BUCKETS = (50, 100, 250, 500, 1000, 2000, 5000, 10000)
_TOTALS = defaultdict(int)
_BY_CLUSTER: dict[str, dict[str, dict[str, object]]] = {}
_RECENT: deque[dict[str, object]] = deque(maxlen=_MAX_RECENT)


def _bounded(value: object, limit: int) -> str:
    return str(value or "")[:limit] or "__unknown__"


def _query_row(cluster_id: str, query: str) -> dict[str, object]:
    cluster = _BY_CLUSTER.setdefault(cluster_id, {})
    if query not in cluster and len(cluster) >= _MAX_QUERIES_PER_CLUSTER:
        query = "__other__"
    return cluster.setdefault(query, {
        "count": 0,
        "errors": 0,
        "circuit_open": 0,
        "duration_ms_last": 0.0,
        "duration_ms_max": 0.0,
        "duration_buckets": {str(bound): 0 for bound in _QUERY_BUCKETS},
        "stale_age_seconds_last": None,
        "collector_lag_seconds_last": None,
    })


def record_query(
    cluster_id: str,
    query: str,
    duration_ms: float,
    *,
    status: str = "success",
    stale_age_seconds: float | None = None,
    collector_lag_seconds: float | None = None,
) -> None:
    cluster_key = _bounded(cluster_id, 128)
    query_key = _bounded(query, 64)
    duration = round(max(0.0, float(duration_ms)), 2)
    with _LOCK:
        if cluster_key not in _BY_CLUSTER and len(_BY_CLUSTER) >= _MAX_CLUSTERS:
            cluster_key = "__other__"
        row = _query_row(cluster_key, query_key)
        row["count"] = int(row["count"]) + 1
        if status not in {"success", "stale", "error"}:
            row[status] = int(row.get(status, 0)) + 1
        if status == "error":
            row["errors"] = int(row["errors"]) + 1
        row["duration_ms_last"] = duration
        row["duration_ms_max"] = max(float(row["duration_ms_max"]), duration)
        for bound in _QUERY_BUCKETS:
            if duration <= bound:
                buckets = row["duration_buckets"]
                buckets[str(bound)] = int(buckets[str(bound)]) + 1
                break
        row["stale_age_seconds_last"] = None if stale_age_seconds is None else round(max(0.0, float(stale_age_seconds)), 2)
        row["collector_lag_seconds_last"] = None if collector_lag_seconds is None else round(max(0.0, float(collector_lag_seconds)), 2)
        _TOTALS[f"query_{status}_total"] += 1
        _RECENT.append({
            "at": time(), "cluster_id": cluster_key, "query": query_key,
            "duration_ms": duration, "status": status,
            "stale_age_seconds": row["stale_age_seconds_last"],
            "collector_lag_seconds": row["collector_lag_seconds_last"],
        })


def record_snapshot_freshness(cluster_id: str, *, age_seconds: float | None, collector_lag_seconds: float | None) -> None:
    record_query(
        cluster_id,
        "snapshot_read",
        0.0,
        status="stale" if age_seconds is not None and age_seconds > 30 else "success",
        stale_age_seconds=age_seconds,
        collector_lag_seconds=collector_lag_seconds,
    )


def get_metrics() -> dict[str, object]:
    with _LOCK:
        return {
            "totals": dict(_TOTALS),
            "by_cluster": {
                cluster: {query: dict(values) for query, values in queries.items()}
                for cluster, queries in _BY_CLUSTER.items()
            },
            "recent": list(_RECENT),
        }
