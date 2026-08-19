"""Loki log-source adapter (tầng T1) -- the chosen centralized store
(Plan/log-intelligence-rca-plan.md, section 11.1).

Queries Loki's `/loki/api/v1/query_range` HTTP API directly. No Grafana
involved: Grafana is a UI for humans, and this is a collector. That also
means adopting Loki costs this app exactly one file (this one) plus a
config flip, which is the whole argument for the adapter boundary (R2).

Why Loki over Elasticsearch for this pipeline, restated where it matters:
tầng T2 does its own fingerprinting, so the only query shape ever needed
is "give me every line for these labels in this time window". That is
Loki's cheapest operation and the one an inverted full-text index would be
pure overhead for.

Label convention assumed (must match whatever ships the logs -- Promtail,
Alloy, Vector, or the RCA team's own agent):

    {cluster="<name>", host="<ip-or-hostname>", daemon_type="mon|mgr|osd|rgw"}

If the RCA team's shipper uses different label names, change `_selector`
below -- nothing else in this codebase knows about labels.
"""

from __future__ import annotations

from datetime import datetime

from config.settings import settings
from shared.models import Cluster
from watcher.log_source.base import LogRecord, LogSourceError, LogSourceResult

SOURCE_NAME = "loki"

# Loki's own server-side cap is 5000 by default; asking for more just gets
# silently truncated, so the adapter stays under it per request.
LOKI_MAX_LIMIT = 5000


def _selector(host: str, daemon_type: str, cluster: Cluster | None) -> str:
    cluster_name = cluster.name if cluster is not None else "default"
    return (
        '{cluster="%s", host="%s", daemon_type="%s"}'
        % (cluster_name, host, daemon_type)
    )


def fetch(
    host: str,
    daemon_type: str,
    window_start: datetime,
    window_end: datetime,
    cluster: Cluster | None = None,
) -> LogSourceResult:
    """Pull one (host, daemon_type) slice out of Loki.

    Same never-raise contract as the ssh adapter: a Loki outage degrades
    this scan to PARTIAL rather than killing the Watcher poll loop.
    """
    import httpx  # local import: an ssh-mode deployment never needs this

    from watcher.log_intel import parse_log_line

    base_url = (settings.log_intel_loki_url or "").rstrip("/")
    if not base_url:
        # Loud, not silent: a "loki" deployment with no URL configured
        # would otherwise look identical to a cluster that simply logged
        # nothing all window -- the exact failure mode that makes an
        # evidence base quietly worthless.
        return LogSourceResult(
            records=[],
            error="log_intel_source=loki nhưng chưa cấu hình log_intel_loki_url",
        )

    headers = {}
    if settings.log_intel_loki_tenant:
        headers["X-Scope-OrgID"] = settings.log_intel_loki_tenant

    params = {
        "query": _selector(host, daemon_type, cluster),
        "start": str(int(window_start.timestamp() * 1_000_000_000)),
        "end": str(int(window_end.timestamp() * 1_000_000_000)),
        "limit": str(min(max(1, settings.log_intel_max_lines_per_daemon), LOKI_MAX_LIMIT)),
        "direction": "forward",
    }

    try:
        response = httpx.get(
            f"{base_url}/loki/api/v1/query_range",
            params=params,
            headers=headers,
            timeout=settings.log_intel_loki_timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return LogSourceResult(records=[], error=f"{host}/{daemon_type}: Loki: {exc}")

    records: list[LogRecord] = []
    for stream in (payload.get("data") or {}).get("result") or []:
        for entry in stream.get("values") or []:
            # Each entry is [<unix-nanoseconds-as-string>, <line>].
            if not isinstance(entry, list) or len(entry) != 2:
                continue
            ts_ns, line = entry
            try:
                ts = datetime.utcfromtimestamp(int(ts_ns) / 1_000_000_000)
            except (TypeError, ValueError):
                ts = None
            record = parse_log_line(str(line), host=host, daemon_type=daemon_type)
            if record is None:
                continue
            # Loki's own ingestion timestamp is authoritative over whatever
            # the line's embedded one says -- the line may have no parseable
            # timestamp at all, and Loki's is what the window was queried by.
            records.append(
                LogRecord(
                    ts=ts or record.ts,
                    host=record.host,
                    daemon_type=record.daemon_type,
                    message=record.message,
                    raw=record.raw,
                    severity=record.severity,
                )
            )
    return LogSourceResult(records=records)


def check_reachable() -> None:
    """Config-check helper for a future Settings page 'test connection'
    button. Raises LogSourceError with a readable reason; not called by the
    scan path (which must degrade, never raise)."""
    import httpx

    base_url = (settings.log_intel_loki_url or "").rstrip("/")
    if not base_url:
        raise LogSourceError("Chưa cấu hình log_intel_loki_url")
    try:
        response = httpx.get(
            f"{base_url}/ready", timeout=settings.log_intel_loki_timeout_seconds
        )
        response.raise_for_status()
    except Exception as exc:
        raise LogSourceError(f"Không kết nối được Loki tại {base_url}: {exc}") from exc
