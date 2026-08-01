"""Structured per-request bucket access log — an equivalent to Ceph's
native "S3 Bucket Logging" feature (added ~Squid 19.x) for OLDER Ceph
versions that don't have it, built from data that's ALREADY there rather
than a new one: RGW's Beast HTTP frontend (default since Nautilus 14.x —
well below Squid) writes one Apache-Combined-Log-Format access log line
per request to the radosgw daemon log by default, with no extra
config needed. A real example line (verified against Ceph's own PR that
added it, ceph/ceph#33083):

    beast: 0x7fc49be9a710: 10.0.12.5 - anonymous [12/Jun/2024:13:10:07.404
    +0000] "HEAD / HTTP/1.0" 200 5 - - - latency=0.000000000s

— remote_addr, timestamp, method+path, http_status are all right there.
This module only parses lines `watcher/rgw_log.py::fetch_rgw_log` (SSH,
already-established RGW log access — no S3 access/secret key needed at
all, unlike an earlier design considered for this feature: RGW's S3 API
and Admin Ops API expose object CRUD and AGGREGATE usage stats
respectively, neither carries per-request remote_addr/http_status).

Known limitation, NOT verified against a real cluster this session: only
PATH-STYLE requests (bucket as the first path segment, e.g.
`/mybucket/key`) carry a bucket name in this log line at all —
virtual-hosted-style requests (bucket in the Host header, e.g.
`mybucket.s3.example.com`) have no bucket name recoverable from the path,
same "can't recover data that isn't there" gap
watcher/collector.py::identify_relevant_nodes already has for OSD_/PG_
node mapping. Those rows come back with bucket=None rather than a guess.
"""

from __future__ import annotations

import re
from datetime import datetime

from watcher.rgw_log import RgwLogError, fetch_rgw_log  # noqa: F401 — re-exported for callers

_BEAST_LOG_RE = re.compile(
    r"beast:\s+\S+:\s+"
    r"(?P<remote_addr>\S+)\s+-\s+(?P<user>\S+)\s+"
    r"\[(?P<timestamp>[^\]]+)\]\s+"
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[\d.]+"\s+'
    r"(?P<status>\d+)\s+(?P<bytes_sent>\S+)"
)

# strptime format for the beast access log's own timestamp field, e.g.
# "12/Jun/2024:13:10:07.404 +0000" — Apache Combined Log Format's own
# timestamp shape.
_TIMESTAMP_FORMAT = "%d/%b/%Y:%H:%M:%S.%f %z"

_ACTION_VI = {
    ("GET", True): "Tải xuống",
    ("GET", False): "Liệt kê",
    ("PUT", True): "Tải lên",
    ("PUT", False): "Tạo Bucket",
    ("POST", True): "Tải lên (multipart)",
    ("POST", False): "Khác (POST)",
    ("DELETE", True): "Xoá tệp",
    ("DELETE", False): "Xoá Bucket",
    ("HEAD", True): "Kiểm tra tệp",
    ("HEAD", False): "Kiểm tra Bucket",
}


def _action_label(method: str, has_object: bool) -> str:
    return _ACTION_VI.get((method, has_object), method)


def _parse_bucket_and_object(path: str) -> tuple[str | None, str | None]:
    stripped = path.split("?", 1)[0].lstrip("/")
    if not stripped:
        return None, None
    parts = stripped.split("/", 1)
    bucket = parts[0] or None
    obj = parts[1] if len(parts) > 1 and parts[1] else None
    return bucket, obj


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _TIMESTAMP_FORMAT)
    except ValueError:
        return None


def parse_beast_access_log(raw_text: str) -> list[dict]:
    """Parses every Beast access-log line found in `raw_text` (a raw RGW
    daemon log excerpt) into a structured record. Lines that don't match
    (debug/info lines from anything else the daemon logs) are silently
    skipped — this is a best-effort extraction over a general-purpose log,
    not a strict line-format validator."""
    records: list[dict] = []
    for line in raw_text.splitlines():
        match = _BEAST_LOG_RE.search(line)
        if not match:
            continue
        method = match.group("method")
        path = match.group("path")
        bucket, obj = _parse_bucket_and_object(path)
        records.append(
            {
                "remote_addr": match.group("remote_addr"),
                "timestamp": _parse_timestamp(match.group("timestamp")),
                "timestamp_raw": match.group("timestamp"),
                "method": method,
                "path": path,
                "bucket": bucket,
                "object": obj,
                "action": _action_label(method, obj is not None),
                "status": int(match.group("status")),
                "bytes_sent": match.group("bytes_sent"),
            }
        )
    records.sort(key=lambda r: r["timestamp"] or datetime.min.replace(tzinfo=None), reverse=True)
    return records


def fetch_bucket_access_log(host: str, bucket: str | None = None) -> list[dict]:
    """Fetches + parses `host`'s RGW access log, optionally scoped to one
    bucket. `bucket`, when given, is ALSO passed to fetch_rgw_log as its
    server-side grep filter (bounds the SSH round trip's output size to
    lines that at least MENTION the bucket name somewhere) — this
    function then re-filters precisely on the parsed `bucket` field
    afterward, since a coarse text grep can't tell a bucket name in the
    path from the same substring appearing in an object key or elsewhere
    on the line.

    Raises RgwLogError (propagated from fetch_rgw_log) if the log can't be
    fetched from `host` at all. An empty list (log fetched fine, nothing
    matched) is a normal outcome, not an error.
    """
    bucket = (bucket or "").strip() or None
    raw = fetch_rgw_log(host, bucket)
    records = parse_beast_access_log(raw)
    if bucket:
        records = [r for r in records if r["bucket"] == bucket]
    return records
