"""Read-only, bounded Elasticsearch log source for Log Intelligence.

The shipper owns the index and the flat schema documented in the runbook.
This adapter never writes log documents or installs an agent on Ceph nodes.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import quote

from config.settings import settings
from shared.models import Cluster
from watcher.log_source.base import LogRecord, LogSourceError, LogSourceResult

SOURCE_NAME = "elasticsearch"
_INDEX_RE = re.compile(r"^[a-z0-9][a-z0-9._*-]*$")
_MAX_LINES = 5000


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _config() -> tuple[str, str, dict[str, str]]:
    url = settings.log_intel_elasticsearch_url.strip().rstrip("/")
    index = settings.log_intel_elasticsearch_index.strip()
    if not url.startswith(("http://", "https://")) or not index or not _INDEX_RE.fullmatch(index) or ".." in index:
        raise LogSourceError("Cần Elasticsearch URL hợp lệ và index pattern an toàn")
    headers = {"Content-Type": "application/json"}
    if settings.log_intel_elasticsearch_api_key:
        headers["Authorization"] = f"ApiKey {settings.log_intel_elasticsearch_api_key}"
    return url, index, headers


def fetch(host: str, daemon_type: str, window_start: datetime, window_end: datetime,
          cluster: Cluster | None = None) -> LogSourceResult:
    import httpx

    from watcher.log_intel import parse_log_line

    try:
        url, index, headers = _config()
    except LogSourceError as exc:
        return LogSourceResult(records=[], error=str(exc))
    expected_cluster = cluster.name if cluster is not None else "default"
    limit = min(max(1, settings.log_intel_max_lines_per_daemon), settings.learning_job_max_batch_size, _MAX_LINES)
    body = {
        "size": limit,
        "track_total_hits": False,
        "_source": ["@timestamp", "cluster", "host", "daemon_type", "message"],
        "query": {"bool": {"filter": [
            {"term": {"cluster.keyword": expected_cluster}},
            {"term": {"host.keyword": host}},
            {"term": {"daemon_type.keyword": daemon_type}},
            {"range": {"@timestamp": {"gte": _iso_utc(window_start), "lte": _iso_utc(window_end)}}},
        ]}},
        "sort": [{"@timestamp": {"order": "desc"}}],
    }
    try:
        response = httpx.post(
            f"{url}/{quote(index, safe='*,-')}/_search",
            json=body, headers=headers,
            timeout=min(settings.log_intel_elasticsearch_timeout_seconds, settings.learning_job_timeout_seconds),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("timed_out") or payload.get("_shards", {}).get("failed", 0):
            raise LogSourceError("Elasticsearch trả kết quả thiếu hoặc query timeout")
        hits = payload["hits"]["hits"]
        if not isinstance(hits, list):
            raise ValueError("hits không phải danh sách")
    except Exception as exc:
        # Never include exception bodies: HTTP errors may contain a credential-bearing URL.
        return LogSourceResult(records=[], error=f"{host}/{daemon_type}: Elasticsearch query thất bại ({type(exc).__name__})")

    records: list[LogRecord] = []
    invalid = 0
    for hit in reversed(hits[:limit]):
        source = hit.get("_source") if isinstance(hit, dict) else None
        if not isinstance(source, dict) or any(source.get(key) != value for key, value in (
            ("cluster", expected_cluster), ("host", host), ("daemon_type", daemon_type),
        )):
            invalid += 1
            continue
        raw = source.get("message")
        stamp = source.get("@timestamp")
        if not isinstance(raw, str) or not isinstance(stamp, str):
            invalid += 1
            continue
        try:
            ts = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
        except ValueError:
            invalid += 1
            continue
        if not _iso_utc(window_start) <= _iso_utc(ts) <= _iso_utc(window_end):
            invalid += 1
            continue
        record = parse_log_line(raw, host=host, daemon_type=daemon_type)
        if record is not None:
            records.append(LogRecord(ts=ts, host=host, daemon_type=daemon_type,
                                     message=record.message, raw=record.raw, severity=record.severity))
    return LogSourceResult(records=records,
                           error=f"Elasticsearch bỏ qua {invalid} document sai schema/label/time" if invalid else None)


def check_reachable() -> None:
    import httpx

    url, index, headers = _config()
    try:
        response = httpx.post(f"{url}/{quote(index, safe='*,-')}/_search",
                              json={"size": 0, "query": {"match_none": {}}}, headers=headers,
                              timeout=settings.log_intel_elasticsearch_timeout_seconds)
        response.raise_for_status()
    except Exception as exc:
        raise LogSourceError(f"Không truy vấn được Elasticsearch ({type(exc).__name__})") from None
