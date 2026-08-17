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

import json
import re
import shlex
from datetime import datetime

from config.settings import settings
from watcher import ceph_client
from watcher.ceph_client import run_command_on_node, run_command_on_node_with
from watcher.rgw_log import fetch_rgw_log_with
from watcher.rgw_log import RgwLogError, fetch_rgw_log  # noqa: F401 — re-exported for callers

_BEAST_LOG_RE = re.compile(
    r"beast:\s+\S+:\s+"
    r"(?P<remote_addr>\S+)\s+-\s+(?P<user>\S+)\s+"
    r"\[(?P<timestamp>[^\]]+)\]\s+"
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/[\d.]+"\s+'
    r"(?P<status>\d+)\s+(?P<bytes_sent>\S+)(?P<tail>.*)$"
)

# strptime format for the beast access log's own timestamp field, e.g.
# "12/Jun/2024:13:10:07.404 +0000" — Apache Combined Log Format's own
# timestamp shape.
_TIMESTAMP_FORMAT = "%d/%b/%Y:%H:%M:%S.%f %z"

# `radosgw-admin bucket stats`'s own `creation_time`/`mtime` shape, e.g.
# "2024-01-01 00:00:00.000000" — NOT the same shape as the beast access
# log's timestamp above (space separator, no explicit UTC offset). Always
# UTC in practice (same "naive datetime = UTC" convention shared/models.py
# uses throughout this codebase for its own DB columns) — NOT independently
# verified against a real cluster this session.
_CREATION_TIME_FORMAT = "%Y-%m-%d %H:%M:%S.%f"

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


def _parse_creation_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    for parser in (
        lambda raw: datetime.strptime(raw, _CREATION_TIME_FORMAT),
        lambda raw: datetime.fromisoformat(raw.replace("Z", "+00:00")),
    ):
        try:
            return parser(value)
        except ValueError:
            continue
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
        tail = match.group("tail") or ""
        latency_match = re.search(r"\blatency=([0-9.]+)s\b", tail)
        quoted = re.findall(r'"([^"]*)"', tail)
        user_agent = quoted[1] if len(quoted) > 1 else (quoted[0] if len(quoted) == 1 else None)
        bytes_sent = match.group("bytes_sent")
        records.append(
            {
                "remote_addr": match.group("remote_addr"),
                "requester": match.group("user"),
                "user_agent": user_agent if user_agent and user_agent != "-" else None,
                "timestamp": _parse_timestamp(match.group("timestamp")),
                "timestamp_raw": match.group("timestamp"),
                "method": method,
                "path": path,
                "bucket": bucket,
                "object": obj,
                "action": _action_label(method, obj is not None),
                "status": int(match.group("status")),
                "bytes_sent": int(bytes_sent) if bytes_sent.isdigit() else None,
                "latency_ms": float(latency_match.group(1)) * 1000 if latency_match else None,
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


def fetch_bucket_access_log_with(host: str, bucket: str | None, ssh_user: str, ssh_key_path: str,
                                 exec_mode: str, rgw_container_name: str) -> list[dict]:
    bucket = (bucket or "").strip() or None
    raw = fetch_rgw_log_with(
        host, bucket, ssh_user, ssh_key_path, exec_mode, rgw_container_name
    )
    records = parse_beast_access_log(raw)
    return [row for row in records if row["bucket"] == bucket] if bucket else records


def _bucket_names(payload: object) -> list[str]:
    """Normalize the JSON returned by ``radosgw-admin bucket list``.

    Current RGW versions return a JSON array of bucket names.  Accepting a
    ``{"buckets": [...]}`` wrapper and name-bearing objects makes the
    read-only inventory tolerant of older/vendor builds without ever treating
    arbitrary values as a command argument.
    """
    values = payload.get("buckets", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        return []
    names = set()
    for value in values:
        name = value.get("bucket") or value.get("name") if isinstance(value, dict) else value
        if isinstance(name, str) and name.strip():
            names.add(name.strip())
    return sorted(names, key=str.casefold)


def fetch_bucket_list(host: str) -> list[str]:
    """List bucket names from one configured RGW host, without S3 keys.

    The command is deliberately a fixed literal.  Bucket details use the
    existing, safely quoted ``fetch_bucket_stats`` path separately, so the
    page can fetch stats only for the current pagination window instead of
    launching one SSH command per bucket in the entire cluster.
    """
    exec_mode = settings.ceph_exec_mode
    if exec_mode not in ("cephadm", "none") and not settings.ceph_rgw_container_name:
        raise RgwLogError(
            "Chưa cấu hình tên container RGW — điền ở mục \"Cấu hình RGW\" phía trên."
        )
    command = ceph_client.build_exec_command(
        exec_mode, settings.ceph_rgw_container_name, "radosgw-admin bucket list --format json"
    )
    try:
        output = run_command_on_node(host, command)
    except Exception as exc:
        raise RgwLogError(f"Không lấy được danh sách bucket trên {host}: {exc}") from exc
    try:
        return _bucket_names(json.loads(output))
    except (TypeError, ValueError) as exc:
        raise RgwLogError(f"RGW {host} trả về danh sách bucket không hợp lệ") from exc


def fetch_bucket_list_with(host: str, ssh_user: str, ssh_key_path: str,
                           exec_mode: str, rgw_container_name: str) -> list[str]:
    """Cluster-scoped variant of :func:`fetch_bucket_list`."""
    if exec_mode not in ("cephadm", "none") and not rgw_container_name:
        raise RgwLogError("Chưa cấu hình tên container RGW cho cụm đang chọn.")
    command = ceph_client.build_exec_command(
        exec_mode, rgw_container_name, "radosgw-admin bucket list --format json"
    )
    try:
        output = run_command_on_node_with(host, command, ssh_user, ssh_key_path)
    except Exception as exc:
        raise RgwLogError(f"Không lấy được danh sách bucket trên {host}: {exc}") from exc
    try:
        return _bucket_names(json.loads(output))
    except (TypeError, ValueError) as exc:
        raise RgwLogError(f"RGW {host} trả về danh sách bucket không hợp lệ") from exc


def _user_ids(payload: object) -> list[str]:
    values = payload.get("users", []) if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        return []
    users = {
        value.strip() for value in values
        if isinstance(value, str) and value.strip()
    }
    return sorted(users, key=str.casefold)


def _parse_json_object(output: str) -> dict | None:
    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def fetch_s3_user_list(host: str) -> list[str]:
    exec_mode = settings.ceph_exec_mode
    if exec_mode not in ("cephadm", "none") and not settings.ceph_rgw_container_name:
        raise RgwLogError("Chưa cấu hình tên container RGW.")
    command = ceph_client.build_exec_command(
        exec_mode, settings.ceph_rgw_container_name, "radosgw-admin user list --format json"
    )
    try:
        output = run_command_on_node(host, command)
        return _user_ids(json.loads(output))
    except (TypeError, ValueError) as exc:
        raise RgwLogError(f"RGW {host} trả về danh sách S3 user không hợp lệ") from exc
    except Exception as exc:
        raise RgwLogError(f"Không lấy được danh sách S3 user trên {host}: {exc}") from exc


def fetch_s3_user_list_with(host: str, ssh_user: str, ssh_key_path: str,
                            exec_mode: str, rgw_container_name: str) -> list[str]:
    if exec_mode not in ("cephadm", "none") and not rgw_container_name:
        raise RgwLogError("Chưa cấu hình tên container RGW cho cụm đang chọn.")
    command = ceph_client.build_exec_command(
        exec_mode, rgw_container_name, "radosgw-admin user list --format json"
    )
    try:
        output = run_command_on_node_with(host, command, ssh_user, ssh_key_path)
        return _user_ids(json.loads(output))
    except (TypeError, ValueError) as exc:
        raise RgwLogError(f"RGW {host} trả về danh sách S3 user không hợp lệ") from exc
    except Exception as exc:
        raise RgwLogError(f"Không lấy được danh sách S3 user trên {host}: {exc}") from exc


def fetch_s3_user_info(host: str, uid: str) -> dict | None:
    if settings.ceph_exec_mode not in ("cephadm", "none") and not settings.ceph_rgw_container_name:
        raise RgwLogError("Chưa cấu hình tên container RGW.")
    command = ceph_client.build_exec_command(
        settings.ceph_exec_mode,
        settings.ceph_rgw_container_name,
        f"radosgw-admin user info --uid={shlex.quote(uid)} --format json",
    )
    try:
        return _parse_json_object(run_command_on_node(host, command))
    except Exception as exc:
        raise RgwLogError(f"Không lấy được thông tin S3 user trên {host}: {exc}") from exc


def fetch_s3_user_info_with(host: str, uid: str, ssh_user: str, ssh_key_path: str,
                            exec_mode: str, rgw_container_name: str) -> dict | None:
    if exec_mode not in ("cephadm", "none") and not rgw_container_name:
        raise RgwLogError("Chưa cấu hình tên container RGW cho cụm đang chọn.")
    command = ceph_client.build_exec_command(
        exec_mode, rgw_container_name,
        f"radosgw-admin user info --uid={shlex.quote(uid)} --format json",
    )
    try:
        return _parse_json_object(run_command_on_node_with(host, command, ssh_user, ssh_key_path))
    except Exception as exc:
        raise RgwLogError(f"Không lấy được thông tin S3 user trên {host}: {exc}") from exc


def summarize_s3_user(raw: dict) -> dict:
    """Allowlist non-secret fields; key objects are deliberately discarded."""
    user_quota = raw.get("user_quota") or {}
    bucket_quota = raw.get("bucket_quota") or {}
    return {
        "uid": raw.get("user_id") or raw.get("uid"),
        "display_name": raw.get("display_name"),
        "email": raw.get("email"),
        "suspended": bool(raw.get("suspended", False)),
        "max_buckets": raw.get("max_buckets"),
        "key_count": len(raw.get("keys") or []),
        "subuser_count": len(raw.get("subusers") or []),
        "caps": [str(cap.get("type")) for cap in (raw.get("caps") or []) if isinstance(cap, dict) and cap.get("type")],
        "user_quota_enabled": bool(user_quota.get("enabled", False)),
        "bucket_quota_enabled": bool(bucket_quota.get("enabled", False)),
    }


def build_s3_user_action_command(action: str, uid: str, params: dict) -> str:
    """Closed command builder. Creation explicitly generates no access key."""
    quoted_uid = shlex.quote(uid)
    if action == "create":
        command = f"radosgw-admin user create --uid={quoted_uid}"
        if params.get("display_name"):
            command += f" --display-name={shlex.quote(str(params['display_name']))}"
        if params.get("email"):
            command += f" --email={shlex.quote(str(params['email']))}"
        return command + " --format json"
    if action == "modify":
        command = f"radosgw-admin user modify --uid={quoted_uid}"
        if params.get("display_name"):
            command += f" --display-name={shlex.quote(str(params['display_name']))}"
        if params.get("email"):
            command += f" --email={shlex.quote(str(params['email']))}"
        return command + " --format json"
    if action in {"suspend", "enable"}:
        return f"radosgw-admin user {action} --uid={quoted_uid}"
    raise ValueError("Unsupported S3 user action")


def execute_s3_user_action(host: str, action: str, uid: str, params: dict) -> None:
    command = ceph_client.build_exec_command(
        settings.ceph_exec_mode, settings.ceph_rgw_container_name,
        build_s3_user_action_command(action, uid, params),
    )
    try:
        run_command_on_node(host, command)
    except Exception as exc:
        raise RgwLogError(f"Thao tác S3 user thất bại trên {host}: {exc}") from exc


def execute_s3_user_action_with(host: str, action: str, uid: str, params: dict,
                                ssh_user: str, ssh_key_path: str, exec_mode: str,
                                rgw_container_name: str) -> None:
    command = ceph_client.build_exec_command(
        exec_mode, rgw_container_name, build_s3_user_action_command(action, uid, params)
    )
    try:
        run_command_on_node_with(host, command, ssh_user, ssh_key_path)
    except Exception as exc:
        raise RgwLogError(f"Thao tác S3 user thất bại trên {host}: {exc}") from exc


def _new_s3_key(payload: object, existing: set[str]) -> dict:
    if isinstance(payload, dict) and payload.get("access_key") and payload.get("secret_key"):
        candidates = [payload]
    elif isinstance(payload, dict):
        candidates = payload.get("keys") or []
    else:
        candidates = []
    fresh = [key for key in candidates if isinstance(key, dict) and key.get("access_key") not in existing]
    if len(fresh) != 1 or not fresh[0].get("secret_key"):
        raise RgwLogError("RGW không trả về duy nhất một access key mới")
    return {"access_key": str(fresh[0]["access_key"]), "secret_key": str(fresh[0]["secret_key"])}


def create_s3_access_key(host: str, uid: str) -> dict:
    before = fetch_s3_user_info(host, uid) or {}
    existing = {str(key.get("access_key")) for key in (before.get("keys") or []) if isinstance(key, dict)}
    inner = f"radosgw-admin key create --uid={shlex.quote(uid)} --key-type=s3 --gen-access-key --gen-secret --format json"
    command = ceph_client.build_exec_command(settings.ceph_exec_mode, settings.ceph_rgw_container_name, inner)
    try:
        output = run_command_on_node(host, command)
        return _new_s3_key(json.loads(output), existing)
    except RgwLogError:
        raise
    except Exception as exc:
        raise RgwLogError(f"Không tạo được access key trên {host}: {exc}") from exc


def create_s3_access_key_with(host: str, uid: str, ssh_user: str, ssh_key_path: str,
                              exec_mode: str, rgw_container_name: str) -> dict:
    before = fetch_s3_user_info_with(host, uid, ssh_user, ssh_key_path, exec_mode, rgw_container_name) or {}
    existing = {str(key.get("access_key")) for key in (before.get("keys") or []) if isinstance(key, dict)}
    inner = f"radosgw-admin key create --uid={shlex.quote(uid)} --key-type=s3 --gen-access-key --gen-secret --format json"
    command = ceph_client.build_exec_command(exec_mode, rgw_container_name, inner)
    try:
        output = run_command_on_node_with(host, command, ssh_user, ssh_key_path)
        return _new_s3_key(json.loads(output), existing)
    except RgwLogError:
        raise
    except Exception as exc:
        raise RgwLogError(f"Không tạo được access key trên {host}: {exc}") from exc


def revoke_s3_access_key(host: str, uid: str, access_key: str) -> None:
    inner = f"radosgw-admin key rm --uid={shlex.quote(uid)} --key-type=s3 --access-key={shlex.quote(access_key)}"
    command = ceph_client.build_exec_command(settings.ceph_exec_mode, settings.ceph_rgw_container_name, inner)
    try:
        run_command_on_node(host, command)
    except Exception as exc:
        raise RgwLogError(f"Không revoke được access key trên {host}: {exc}") from exc


def revoke_s3_access_key_with(host: str, uid: str, access_key: str, ssh_user: str,
                              ssh_key_path: str, exec_mode: str, rgw_container_name: str) -> None:
    inner = f"radosgw-admin key rm --uid={shlex.quote(uid)} --key-type=s3 --access-key={shlex.quote(access_key)}"
    command = ceph_client.build_exec_command(exec_mode, rgw_container_name, inner)
    try:
        run_command_on_node_with(host, command, ssh_user, ssh_key_path)
    except Exception as exc:
        raise RgwLogError(f"Không revoke được access key trên {host}: {exc}") from exc


S3_CAP_TYPES = {"users", "buckets", "metadata", "usage"}
S3_CAP_PERMS = {"read", "write", "read,write"}


def build_s3_user_setting_command(action: str, uid: str, params: dict) -> str:
    quoted_uid = shlex.quote(uid)
    if action in {"quota_set", "quota_enable", "quota_disable"}:
        scope = str(params.get("scope") or "")
        if scope not in {"user", "bucket"}:
            raise ValueError("Unsupported quota scope")
        base = f"radosgw-admin quota {action.removeprefix('quota_')} --quota-scope={scope} --uid={quoted_uid}"
        if action == "quota_set":
            max_size = int(params["max_size_bytes"])
            max_objects = int(params["max_objects"])
            if max_size != -1 and max_size <= 0 or max_objects != -1 and max_objects <= 0:
                raise ValueError("Quota limits must be positive or -1")
            base += f" --max-size={max_size} --max-objects={max_objects}"
        return base
    if action in {"cap_add", "cap_remove"}:
        cap_type = str(params.get("cap_type") or "")
        cap_perm = str(params.get("cap_perm") or "")
        if cap_type not in S3_CAP_TYPES or cap_perm not in S3_CAP_PERMS:
            raise ValueError("Unsupported S3 capability")
        verb = "add" if action == "cap_add" else "rm"
        return f"radosgw-admin caps {verb} --uid={quoted_uid} --caps={shlex.quote(f'{cap_type}={cap_perm}')}"
    raise ValueError("Unsupported S3 user setting action")


def execute_s3_user_setting(host: str, action: str, uid: str, params: dict) -> None:
    inner = build_s3_user_setting_command(action, uid, params)
    command = ceph_client.build_exec_command(settings.ceph_exec_mode, settings.ceph_rgw_container_name, inner)
    try:
        run_command_on_node(host, command)
    except Exception as exc:
        raise RgwLogError(f"Không cập nhật được quota/capability trên {host}: {exc}") from exc


def execute_s3_user_setting_with(host: str, action: str, uid: str, params: dict,
                                 ssh_user: str, ssh_key_path: str, exec_mode: str,
                                 rgw_container_name: str) -> None:
    inner = build_s3_user_setting_command(action, uid, params)
    command = ceph_client.build_exec_command(exec_mode, rgw_container_name, inner)
    try:
        run_command_on_node_with(host, command, ssh_user, ssh_key_path)
    except Exception as exc:
        raise RgwLogError(f"Không cập nhật được quota/capability trên {host}: {exc}") from exc


def fetch_bucket_stats(host: str, bucket: str) -> dict | None:
    """Real bucket metadata (owner, creation time, object count, size,
    quota) via `radosgw-admin bucket stats --bucket=<name>` — deliberately
    NOT ceph_client.run_ceph_json_command (that always wraps into
    settings.ceph_container_name, the MON container, which has no reason
    to ship the `radosgw-admin` binary at all — that's the RGW package).
    Reuses ceph_client.build_exec_command with settings.ceph_rgw_container_name
    instead, same docker/podman/cephadm/none wrapping every other exec-mode
    branch in this codebase uses, just pointed at the RGW container.

    Returns None if the bucket doesn't exist, or if `radosgw-admin`'s
    output didn't parse as a JSON object — both treated as "nothing to
    show" rather than an error (an empty/typo'd bucket name is a normal,
    expected input here, not a system failure).

    Raises RgwLogError if the command couldn't even be attempted (SSH
    failure, or docker/podman mode with no RGW container name configured).

    NOT verified against a real cluster this session: assumes the RGW
    container image ships `radosgw-admin` (same package as `radosgw`
    itself) for docker/podman mode, and that `cephadm shell --
    radosgw-admin ...` resolves cluster-admin access without needing a
    specific daemon `--name` (true for single-realm/zone; unverified for
    multi-site RGW).
    """
    exec_mode = settings.ceph_exec_mode
    if exec_mode not in ("cephadm", "none") and not settings.ceph_rgw_container_name:
        raise RgwLogError(
            "Chưa cấu hình tên container RGW — điền ở mục \"Cấu hình RGW\" phía trên."
        )
    inner_command = f"radosgw-admin bucket stats --bucket={shlex.quote(bucket)}"
    command = ceph_client.build_exec_command(
        exec_mode, settings.ceph_rgw_container_name, f"{inner_command} --format json"
    )
    try:
        output = run_command_on_node(host, command)
    except Exception as exc:
        raise RgwLogError(f"Không lấy được thông tin bucket trên {host}: {exc}") from exc
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return None  # unknown bucket -> radosgw-admin prints a plain error, not JSON
    return parsed if isinstance(parsed, dict) else None


def fetch_bucket_stats_with(host: str, bucket: str, ssh_user: str, ssh_key_path: str,
                            exec_mode: str, rgw_container_name: str) -> dict | None:
    if exec_mode not in ("cephadm", "none") and not rgw_container_name:
        raise RgwLogError("Chưa cấu hình tên container RGW cho cụm đang chọn.")
    inner = f"radosgw-admin bucket stats --bucket={shlex.quote(bucket)} --format json"
    command = ceph_client.build_exec_command(exec_mode, rgw_container_name, inner)
    try:
        output = run_command_on_node_with(host, command, ssh_user, ssh_key_path)
    except Exception as exc:
        raise RgwLogError(f"Không lấy được thông tin bucket trên {host}: {exc}") from exc
    try:
        parsed = json.loads(output)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def summarize_bucket_stats(raw: dict) -> dict:
    """Picks the fields worth showing an operator out of `radosgw-admin
    bucket stats`'s full JSON (which also carries internal/rarely-useful
    fields like `marker`/`index_type`/`explicit_placement`) — field names
    (`usage.rgw.main.num_objects`/`size_utilized`, `bucket_quota.*`) are
    Ceph's own long-stable `radosgw-admin bucket stats` output shape, not
    independently verified against a real cluster this session (same
    caveat fetch_bucket_stats's own docstring already carries)."""
    usage = raw.get("usage") or {}
    main = usage.get("rgw.main") or {}
    quota = raw.get("bucket_quota") or {}
    def capability_status(*keys: str) -> str:
        value = next((raw[key] for key in keys if key in raw), None)
        if isinstance(value, dict):
            value = value.get("status", value.get("enabled"))
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if normalized in {"enabled", "enable", "on", "true"}:
                return "enabled"
            if normalized in {"disabled", "disable", "off", "false", "suspended"}:
                return "disabled"
        return "unknown"

    return {
        "owner": raw.get("owner"),
        "creation_time": _parse_creation_time(raw.get("creation_time")),
        "num_objects": main.get("num_objects", 0),
        "size_bytes": main.get("size_utilized", main.get("size", 0)),
        "num_shards": raw.get("num_shards"),
        "placement_rule": raw.get("placement_rule"),
        "quota_enabled": bool(quota.get("enabled", False)),
        "quota_max_size_bytes": quota.get("max_size"),
        "quota_max_objects": quota.get("max_objects"),
        # These keys occur in some newer/vendor RGW builds. Older releases do
        # not expose them through `bucket stats`; report unknown rather than
        # guessing from unrelated flags.
        "versioning_status": capability_status(
            "versioning_status", "versioning", "versioning_enabled"
        ),
        "object_lock_status": capability_status(
            "object_lock_status", "object_lock", "object_lock_enabled"
        ),
        "policy_available": isinstance(raw.get("policy"), (dict, list, str)),
        "lifecycle_available": isinstance(raw.get("lifecycle"), (dict, list, str)),
    }
