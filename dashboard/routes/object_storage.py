"""Read-only RGW bucket inventory scoped to the selected Ceph cluster."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from math import ceil
from typing import Literal
from urllib.parse import quote, urlparse

import boto3
from botocore.config import Config

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from dashboard.cluster_scope import cluster_selection, selected_cluster
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import to_utc_iso
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared import db
from shared.ceph_releases import codename_for_version
from shared.models import ObjectStorageAuditEntry
from dashboard.cluster_scope import cluster_connection
from watcher import ceph_client
from watcher.ceph_client import CephQueryError
from watcher.rgw_access_log import (
    RgwLogError,
    fetch_bucket_access_log,
    fetch_bucket_access_log_with,
    fetch_bucket_list,
    fetch_bucket_list_with,
    fetch_bucket_stats,
    fetch_bucket_stats_with,
    summarize_bucket_stats,
    create_s3_access_key,
    create_s3_access_key_with,
    fetch_s3_user_info,
    fetch_s3_user_info_with,
    revoke_s3_access_key,
    revoke_s3_access_key_with,
    build_bucket_quota_command,
    execute_bucket_quota,
    execute_bucket_quota_with,
    fetch_bucket_objects,
    fetch_bucket_objects_with,
)


router = APIRouter()
templates = make_templates()

PAGE_SIZE = 25
MAX_QUERY_LENGTH = 120
MAX_METADATA_SCAN = 500
MAX_LIFECYCLE_SCAN = 1000
MAX_PURGE_BATCHES = 10000
MAX_OBJECT_BROWSER_SCAN = 2000
MAX_PRESIGNED_EXPIRY_SECONDS = 900
MAX_PRESIGNED_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024
PRESIGNED_UPLOAD_TYPES = {
    "application/octet-stream", "application/json", "application/pdf", "text/plain",
    "text/csv", "image/jpeg", "image/png", "application/gzip", "application/zip",
}
LIFECYCLE_STORAGE_CLASSES = {
    "STANDARD_IA", "ONEZONE_IA", "INTELLIGENT_TIERING", "REDUCED_REDUNDANCY",
}
REEF_POLICY_ACTIONS = {
    "s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:GetObjectAcl",
    "s3:GetObjectVersionAcl", "s3:PutObjectAcl", "s3:PutObjectVersionAcl",
    "s3:DeleteObject", "s3:DeleteObjectVersion", "s3:ListMultipartUploadParts",
    "s3:AbortMultipartUpload", "s3:RestoreObject", "s3:CreateBucket", "s3:DeleteBucket",
    "s3:ListBucket", "s3:ListBucketVersions", "s3:ListAllMyBuckets",
    "s3:ListBucketMultipartUploads", "s3:GetBucketAcl", "s3:PutBucketAcl",
    "s3:GetBucketVersioning", "s3:PutBucketVersioning", "s3:GetBucketLocation",
    "s3:GetBucketPolicy", "s3:DeleteBucketPolicy", "s3:PutBucketPolicy",
    "s3:GetBucketLogging", "s3:PutBucketLogging", "s3:GetBucketTagging",
    "s3:PutBucketTagging", "s3:GetLifecycleConfiguration", "s3:PutLifecycleConfiguration",
    "s3:GetObjectTagging", "s3:PutObjectTagging", "s3:DeleteObjectTagging",
    "s3:GetObjectVersionTagging", "s3:PutObjectVersionTagging", "s3:DeleteObjectVersionTagging",
    "s3:PutBucketObjectLockConfiguration", "s3:GetBucketObjectLockConfiguration",
    "s3:PutObjectRetention", "s3:GetObjectRetention", "s3:PutObjectLegalHold",
    "s3:GetObjectLegalHold", "s3:BypassGovernanceRetention", "s3:GetBucketPolicyStatus",
    "s3:PutPublicAccessBlock", "s3:GetPublicAccessBlock", "s3:DeletePublicAccessBlock",
    "s3:GetBucketPublicAccessBlock", "s3:PutBucketPublicAccessBlock",
    "s3:DeleteBucketPublicAccessBlock", "s3:GetBucketEncryption", "s3:PutBucketEncryption",
}
OBJECT_LOCK_POLICY_ACTIONS = {action for action in REEF_POLICY_ACTIONS if "ObjectLock" in action or
                              "Retention" in action or "LegalHold" in action or "Governance" in action}
POLICY_ACTION_MIN_MAJOR = {action: 13 for action in REEF_POLICY_ACTIONS}
for _action in REEF_POLICY_ACTIONS:
    if any(token in _action for token in ("Notification", "Replication", "PublicAccessBlock")):
        POLICY_ACTION_MIN_MAJOR[_action] = 15
    if "BucketTagging" in _action:
        POLICY_ACTION_MIN_MAJOR[_action] = 15
    if _action in OBJECT_LOCK_POLICY_ACTIONS:
        POLICY_ACTION_MIN_MAJOR[_action] = 15
    if "BucketEncryption" in _action:
        POLICY_ACTION_MIN_MAJOR[_action] = 18
    if "BucketLogging" in _action:
        POLICY_ACTION_MIN_MAJOR[_action] = 20
logger = logging.getLogger(__name__)

QuotaFilter = Literal["all", "enabled", "disabled"]
UsageFilter = Literal["all", "nonempty", "empty"]
SortField = Literal["name", "owner", "objects", "size", "created"]
SortOrder = Literal["asc", "desc"]


class ObjectStorageError(RuntimeError):
    """A safe, operator-facing failure while querying the configured RGW."""


def _capabilities(cluster) -> dict:
    try:
        if cluster.is_default:
            versions = ceph_client.summarize_cluster_versions()
        else:
            connection = cluster_connection(cluster)
            _host, payload = ceph_client.run_ceph_json_command_with(*connection, "ceph versions")
            versions = ceph_client.summarize_versions_payload(payload)
    except CephQueryError as exc:
        raise ObjectStorageError(f"Không lấy được phiên bản Ceph của cluster: {exc}") from exc
    version = versions.get("current_version")
    if not version:
        raise ObjectStorageError("Cluster đang chạy lẫn phiên bản Ceph; từ chối suy đoán capability RGW.")
    release = codename_for_version(version)
    if not release:
        raise ObjectStorageError(f"Chưa có capability matrix cho Ceph {version}.")
    try:
        major = int(str(version).split(".", 1)[0])
    except ValueError as exc:
        raise ObjectStorageError(f"Không đọc được major version Ceph từ {version}.") from exc
    placement_supported = major >= 10  # documented as new in Jewel
    lifecycle_supported = major >= 13  # verified in the Mimic S3 support matrix
    lifecycle_transition_supported = major >= 14  # storage classes: Nautilus
    versioning_supported = major >= 13  # verified in the Mimic S3 support matrix
    object_lock_supported = major >= 15  # introduced in Ceph Octopus
    return {
        "ceph_version": version,
        "ceph_major": major,
        "ceph_release": release,
        "is_mixed": False,
        "bucket_create": {
            "supported": True,
            "method": "s3_api",
            "radosgw_admin_supported": False,
            "documentation": f"https://docs.ceph.com/en/{release}/radosgw/s3/",
            "placement_supported": placement_supported,
            "placement_min_ceph_major": 10,
            "placement_unavailable_reason": None if placement_supported else "Placement target cần Ceph Jewel 10 trở lên.",
            "storage_class_at_bucket_create": False,
        },
        "bucket_governance": {
            "quota": True,
            "versioning": versioning_supported,
            "versioning_min_ceph_major": 13,
            "versioning_unavailable_reason": None if versioning_supported else "Bucket versioning cần Ceph Mimic 13 trở lên.",
            "object_lock_at_create": object_lock_supported,
            "object_lock_min_ceph_major": 15,
            "object_lock_unavailable_reason": None if object_lock_supported else "Object Lock cần Ceph Octopus 15 trở lên.",
            "object_lock_after_create": False,
            "default_retention": object_lock_supported,
            "documentation": f"https://docs.ceph.com/en/{release}/radosgw/s3/bucketops/",
        },
        "lifecycle": {
            "supported": lifecycle_supported,
            "min_ceph_major": 13,
            "unavailable_reason": None if lifecycle_supported else "Lifecycle editor cần Ceph Mimic 13 trở lên.",
            "transition_supported": lifecycle_transition_supported,
            "transition_min_ceph_major": 14,
            "transition_unavailable_reason": None if lifecycle_transition_supported else "Lifecycle Transition cần Storage Classes của Ceph Nautilus 14 trở lên.",
            "max_preview_objects": MAX_LIFECYCLE_SCAN,
            "documentation": f"https://docs.ceph.com/en/{release}/radosgw/s3/",
        },
        "bucket_policy_acl": {
            "supported": major >= 13,
            "min_ceph_major": 12,
            "unavailable_reason": None if major >= 13 else "Bucket Policy cần Ceph Luminous 12 trở lên.",
            "object_lock_actions_supported": object_lock_supported,
            "action_compatibility": "Validated per action against the detected Ceph major release.",
            "public_access_requires_strong_confirmation": True,
            "documentation": f"https://docs.ceph.com/en/{release}/radosgw/bucketpolicy/",
        },
        "bucket_delete": {
            "supported": True,
            "non_empty_requires_purge_flow": True,
            "bypass_governance_retention": False,
            "documentation": f"https://docs.ceph.com/en/{release}/radosgw/s3/bucketops/",
        },
        "object_browser": {
            "supported": major >= 14,
            "min_ceph_major": 14,
            "unavailable_reason": None if major >= 14 else
                "Object Browser cần radosgw-admin bucket object listing của Ceph Nautilus 14 trở lên.",
            "max_page_size": 100,
            "max_filter_scan": MAX_OBJECT_BROWSER_SCAN,
            "metadata_supported": major >= 14,
            "tags_supported": major >= 15,
            "tags_unavailable_reason": None if major >= 15 else "Object Tagging detail cần Ceph Octopus 15 trở lên.",
            "retention_supported": object_lock_supported,
            "retention_unavailable_reason": None if object_lock_supported else "Object retention/legal hold cần Ceph Octopus 15 trở lên.",
            "documentation": f"https://docs.ceph.com/en/{release}/man/8/radosgw-admin/",
        },
    }


_SECRET_PATTERN = re.compile(
    r"(?i)\b(access[_ -]?key|secret(?:[_ -]?access)?[_ -]?key|session[_ -]?token)\b"
    r"(\s*[:=]\s*|\s+)([^\s,;]+)"
)


def _safe_error(exc: Exception) -> str:
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", str(exc))


_BUCKET_RE = re.compile(r"^(?!\d+\.\d+\.\d+\.\d+$)(?!-)(?!.*\.\.)(?!.*\.-)(?!.*-\.)[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def _create_payload(body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    owner = str(body.get("owner") or "").strip()
    endpoint = str(body.get("endpoint") or "").strip().rstrip("/")
    api_name = str(body.get("api_name") or "").strip()
    placement = str(body.get("placement") or "").strip()
    object_lock = body.get("object_lock") is True
    if not _BUCKET_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="Tên bucket phải dài 3–63 ký tự và đúng định dạng DNS S3")
    if not owner or len(owner) > 128 or "/" in owner or any(ord(char) < 32 for char in owner):
        raise HTTPException(status_code=400, detail="Owner S3 không hợp lệ")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise HTTPException(status_code=400, detail="RGW endpoint phải là URL HTTP(S) không chứa credential/query")
    if bool(api_name) != bool(placement):
        raise HTTPException(status_code=400, detail="Placement tùy chỉnh cần đủ zonegroup API name và placement target")
    for value in (api_name, placement):
        if value and (len(value) > 128 or not re.fullmatch(r"[A-Za-z0-9._-]+", value)):
            raise HTTPException(status_code=400, detail="Placement không hợp lệ")
    return {"name": name, "owner": owner, "endpoint": endpoint, "api_name": api_name,
            "placement": placement, "object_lock": object_lock}


def _bucket_audit_start(cluster_id: str, actor: str, payload: dict) -> str:
    location = f"{payload['api_name']}:{payload['placement']}" if payload["placement"] else "default"
    preview = (f"S3 CreateBucket name={payload['name']} owner={payload['owner']} "
               f"endpoint={payload['endpoint']} placement={location} object_lock={payload['object_lock']}")
    with db.SessionLocal() as session:
        row = ObjectStorageAuditEntry(cluster_id=cluster_id, actor=actor, action="create_bucket",
            target_type="bucket", target_id=payload["name"], preview=preview, result="pending")
        session.add(row)
        session.commit()
        return row.id


def _bucket_audit_finish(audit_id: str, result: str, error: str | None = None) -> None:
    with db.SessionLocal() as session:
        row = session.get(ObjectStorageAuditEntry, audit_id)
        if row:
            row.result = result
            row.error_message = error
            row.completed_at = datetime.utcnow()
            session.commit()


def _start_governance_audit(cluster_id: str, actor: str, payload: dict, preview: str) -> str:
    with db.SessionLocal() as session:
        row = ObjectStorageAuditEntry(cluster_id=cluster_id, actor=actor, action=payload["action"],
            target_type="bucket", target_id=payload["bucket"], preview=preview, result="pending")
        session.add(row)
        session.commit()
        return row.id


def _owner_info(cluster, host: str, uid: str) -> dict | None:
    if cluster.is_default:
        return fetch_s3_user_info(host, uid)
    ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
    return fetch_s3_user_info_with(host, uid, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name)


def _temporary_key(cluster, host: str, uid: str) -> dict:
    if cluster.is_default:
        return create_s3_access_key(host, uid)
    ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
    return create_s3_access_key_with(host, uid, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name)


def _revoke_temporary_key(cluster, host: str, uid: str, access_key: str) -> None:
    if cluster.is_default:
        revoke_s3_access_key(host, uid, access_key)
        return
    ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
    revoke_s3_access_key_with(host, uid, access_key, ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name)


def _create_bucket(cluster, payload: dict) -> None:
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise ObjectStorageError("Chưa cấu hình node RGW cho cluster đang chọn.")
    host = hosts[0]
    owner = _owner_info(cluster, host, payload["owner"])
    if not owner:
        raise ObjectStorageError("Không tìm thấy owner S3 trên cluster đang chọn.")
    if bool(owner.get("suspended")):
        raise ObjectStorageError("Owner S3 đang bị vô hiệu hóa.")
    credential = _temporary_key(cluster, host, payload["owner"])
    access_key = str(credential.get("access_key") or "")
    secret_key = str(credential.get("secret_key") or "")
    if not access_key or not secret_key:
        raise ObjectStorageError("RGW không trả về credential tạm hợp lệ.")
    operation_error: Exception | None = None
    try:
        client = boto3.client("s3", endpoint_url=payload["endpoint"],
            aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )
        kwargs = {"Bucket": payload["name"]}
        if payload["object_lock"]:
            kwargs["ObjectLockEnabledForBucket"] = True
        if payload["placement"]:
            kwargs["CreateBucketConfiguration"] = {
                "LocationConstraint": f"{payload['api_name']}:{payload['placement']}"
            }
        client.create_bucket(**kwargs)
    # SDK validation, TLS, DNS and RGW errors have different exception
    # families. Catch all ordinary failures here so the cleanup below still
    # revokes the temporary key on every failed CreateBucket attempt.
    except Exception as exc:
        operation_error = exc
    try:
        _revoke_temporary_key(cluster, host, payload["owner"], access_key)
    except RgwLogError as cleanup_exc:
        raise ObjectStorageError(
            "Không thu hồi được access key tạm sau thao tác CreateBucket; cần thu hồi key thủ công ngay."
        ) from cleanup_exc
    if operation_error:
        raise ObjectStorageError(f"S3 CreateBucket thất bại: {_safe_error(operation_error)}") from operation_error


def _ensure_capability(payload: dict, capability: dict) -> None:
    governance = capability["bucket_governance"]
    if payload.get("placement") and not capability["bucket_create"]["placement_supported"]:
        raise HTTPException(status_code=409, detail=capability["bucket_create"]["placement_unavailable_reason"])
    if str(payload.get("action") or "").startswith("versioning_") and not governance["versioning"]:
        raise HTTPException(status_code=409, detail=governance["versioning_unavailable_reason"])
    if payload.get("object_lock") and not governance["object_lock_at_create"]:
        raise HTTPException(status_code=409, detail=governance["object_lock_unavailable_reason"])
    if payload.get("action") == "retention_set" and not governance["default_retention"]:
        raise HTTPException(status_code=409, detail=governance["object_lock_unavailable_reason"])


def _ensure_lifecycle_capability(payload: dict, capability: dict) -> None:
    lifecycle = capability["lifecycle"]
    if not lifecycle["supported"]:
        raise HTTPException(status_code=409, detail=lifecycle["unavailable_reason"])
    if any("transition_days" in rule for rule in payload["rules"]) and not lifecycle["transition_supported"]:
        raise HTTPException(status_code=409, detail=lifecycle["transition_unavailable_reason"])


def _governance_payload(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action not in {"quota_set", "quota_enable", "quota_disable", "versioning_enable",
                      "versioning_suspend", "retention_set"}:
        raise HTTPException(status_code=400, detail="Thao tác quản trị bucket không hợp lệ")
    bucket = str(body.get("bucket") or "").strip()
    if not _BUCKET_RE.fullmatch(bucket):
        raise HTTPException(status_code=400, detail="Tên bucket không hợp lệ")
    payload = {"action": action, "bucket": bucket}
    if action == "quota_set":
        try:
            payload["max_size_bytes"] = int(body.get("max_size_bytes"))
            payload["max_objects"] = int(body.get("max_objects"))
            build_bucket_quota_command("set", bucket, payload["max_size_bytes"], payload["max_objects"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Quota phải là số dương hoặc -1") from exc
    if action.startswith("versioning_") or action == "retention_set":
        base = _create_payload({"name": bucket, "owner": body.get("owner"),
                                "endpoint": body.get("endpoint")})
        payload.update(owner=base["owner"], endpoint=base["endpoint"])
    if action == "retention_set":
        mode = str(body.get("mode") or "").upper()
        try:
            days = int(body.get("days"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Số ngày retention không hợp lệ") from exc
        if mode not in {"GOVERNANCE", "COMPLIANCE"} or not 1 <= days <= 36500:
            raise HTTPException(status_code=400, detail="Retention cần mode hợp lệ và 1–36500 ngày")
        payload.update(mode=mode, days=days)
    return payload


def _execute_governance(cluster, payload: dict) -> None:
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise ObjectStorageError("Chưa cấu hình node RGW cho cluster đang chọn.")
    host = hosts[0]
    action = payload["action"]
    if action.startswith("quota_"):
        verb = action.removeprefix("quota_")
        size = int(payload.get("max_size_bytes", -1))
        objects = int(payload.get("max_objects", -1))
        if cluster.is_default:
            execute_bucket_quota(host, verb, payload["bucket"], size, objects)
        else:
            ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
            execute_bucket_quota_with(host, verb, payload["bucket"], size, objects,
                ssh_user, ssh_key, mode, cluster.ceph_rgw_container_name)
        return
    owner = _owner_info(cluster, host, payload["owner"])
    if not owner or bool(owner.get("suspended")):
        raise ObjectStorageError("Owner S3 không tồn tại hoặc đang bị vô hiệu hóa.")
    credential = _temporary_key(cluster, host, payload["owner"])
    access_key = str(credential.get("access_key") or "")
    secret_key = str(credential.get("secret_key") or "")
    if not access_key or not secret_key:
        raise ObjectStorageError("RGW không trả về credential tạm hợp lệ.")
    operation_error: Exception | None = None
    try:
        client = boto3.client("s3", endpoint_url=payload["endpoint"], aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
        if action.startswith("versioning_"):
            status = "Enabled" if action == "versioning_enable" else "Suspended"
            client.put_bucket_versioning(Bucket=payload["bucket"], VersioningConfiguration={"Status": status})
        else:
            client.put_object_lock_configuration(Bucket=payload["bucket"],
                ObjectLockConfiguration={"ObjectLockEnabled": "Enabled", "Rule": {
                    "DefaultRetention": {"Mode": payload["mode"], "Days": payload["days"]}}})
    except Exception as exc:
        operation_error = exc
    try:
        _revoke_temporary_key(cluster, host, payload["owner"], access_key)
    except RgwLogError as cleanup_exc:
        raise ObjectStorageError("Không thu hồi được access key tạm; cần thu hồi key thủ công ngay.") from cleanup_exc
    if operation_error:
        raise ObjectStorageError(f"Thao tác S3 bucket thất bại: {_safe_error(operation_error)}") from operation_error


def _lifecycle_payload(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action not in {"lifecycle_put", "lifecycle_delete"}:
        raise HTTPException(status_code=400, detail="Thao tác lifecycle không hợp lệ")
    base = _create_payload({"name": body.get("bucket"), "owner": body.get("owner"),
                            "endpoint": body.get("endpoint")})
    payload = {"action": action, "bucket": base["name"], "owner": base["owner"],
               "endpoint": base["endpoint"]}
    if action == "lifecycle_delete":
        payload["rules"] = []
        return payload
    rules = body.get("rules")
    if not isinstance(rules, list) or not 1 <= len(rules) <= 100:
        raise HTTPException(status_code=400, detail="Lifecycle cần từ 1 đến 100 rule")
    normalized = []
    ids = set()
    for raw in rules:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="Lifecycle rule phải là object JSON")
        rule_id = str(raw.get("id") or "").strip()
        prefix = str(raw.get("prefix") or "")
        status = str(raw.get("status") or "Enabled")
        if not rule_id or len(rule_id) > 255 or rule_id in ids or any(ord(c) < 32 for c in rule_id):
            raise HTTPException(status_code=400, detail="Lifecycle rule ID thiếu, trùng hoặc không hợp lệ")
        if len(prefix) > 1024 or any(ord(c) < 32 for c in prefix) or status not in {"Enabled", "Disabled"}:
            raise HTTPException(status_code=400, detail=f"Lifecycle rule {rule_id} có prefix/status không hợp lệ")
        rule = {"id": rule_id, "prefix": prefix, "status": status}
        for key in ("expiration_days", "noncurrent_expiration_days", "abort_multipart_days", "transition_days"):
            value = raw.get(key)
            if value in (None, ""):
                continue
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"{key} của rule {rule_id} phải là số") from exc
            if not 1 <= value <= 36500:
                raise HTTPException(status_code=400, detail=f"{key} của rule {rule_id} phải từ 1–36500")
            rule[key] = value
        storage_class = str(raw.get("storage_class") or "").strip()
        if storage_class:
            if "transition_days" not in rule or storage_class not in LIFECYCLE_STORAGE_CLASSES:
                raise HTTPException(status_code=400, detail=f"Transition của rule {rule_id} không hợp lệ")
            rule["storage_class"] = storage_class
        if "transition_days" in rule and not storage_class:
            raise HTTPException(status_code=400, detail=f"Rule {rule_id} thiếu storage_class")
        if not any(key in rule for key in ("expiration_days", "noncurrent_expiration_days",
                                           "abort_multipart_days", "transition_days")):
            raise HTTPException(status_code=400, detail=f"Rule {rule_id} chưa có hành động")
        ids.add(rule_id)
        normalized.append(rule)
    payload["rules"] = normalized
    return payload


def _boto_lifecycle_rules(rules: list[dict]) -> list[dict]:
    result = []
    for rule in rules:
        item = {"ID": rule["id"], "Status": rule["status"], "Filter": {"Prefix": rule["prefix"]}}
        if "expiration_days" in rule:
            item["Expiration"] = {"Days": rule["expiration_days"]}
        if "noncurrent_expiration_days" in rule:
            item["NoncurrentVersionExpiration"] = {"NoncurrentDays": rule["noncurrent_expiration_days"]}
        if "abort_multipart_days" in rule:
            item["AbortIncompleteMultipartUpload"] = {"DaysAfterInitiation": rule["abort_multipart_days"]}
        if "transition_days" in rule:
            item["Transitions"] = [{"Days": rule["transition_days"], "StorageClass": rule["storage_class"]}]
        result.append(item)
    return result


def _with_owner_s3(cluster, payload: dict, callback):
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise ObjectStorageError("Chưa cấu hình node RGW cho cluster đang chọn.")
    host = hosts[0]
    owner = _owner_info(cluster, host, payload["owner"])
    if not owner or bool(owner.get("suspended")):
        raise ObjectStorageError("Owner S3 không tồn tại hoặc đang bị vô hiệu hóa.")
    credential = _temporary_key(cluster, host, payload["owner"])
    access_key = str(credential.get("access_key") or "")
    secret_key = str(credential.get("secret_key") or "")
    if not access_key or not secret_key:
        raise ObjectStorageError("RGW không trả về credential tạm hợp lệ.")
    result = None
    operation_error = None
    try:
        client = boto3.client("s3", endpoint_url=payload["endpoint"], aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
        result = callback(client)
    except Exception as exc:
        operation_error = exc
    try:
        _revoke_temporary_key(cluster, host, payload["owner"], access_key)
    except RgwLogError as cleanup_exc:
        raise ObjectStorageError("Không thu hồi được access key tạm; cần thu hồi key thủ công ngay.") from cleanup_exc
    if operation_error:
        raise ObjectStorageError(f"Thao tác S3 thất bại: {_safe_error(operation_error)}") from operation_error
    return result


def _lifecycle_dry_run(cluster, payload: dict) -> dict:
    def inspect(client):
        previous = []
        try:
            previous = client.get_bucket_lifecycle_configuration(Bucket=payload["bucket"]).get("Rules") or []
        except client.exceptions.ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"NoSuchLifecycleConfiguration", "NoSuchLifecycle"}:
                raise
        impacted = 0
        scanned = 0
        truncated = False
        token = None
        now = datetime.now(timezone.utc)
        while scanned < MAX_LIFECYCLE_SCAN:
            kwargs = {"Bucket": payload["bucket"], "MaxKeys": min(1000, MAX_LIFECYCLE_SCAN - scanned)}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)
            objects = page.get("Contents") or []
            scanned += len(objects)
            for obj in objects:
                key = str(obj.get("Key") or "")
                modified = obj.get("LastModified")
                age = (now - modified).days if isinstance(modified, datetime) else -1
                for rule in payload["rules"]:
                    thresholds = [rule[k] for k in ("expiration_days", "transition_days") if k in rule]
                    if rule["status"] == "Enabled" and key.startswith(rule["prefix"]) and thresholds and age >= min(thresholds):
                        impacted += 1
                        break
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
            if not token:
                break
        if page.get("IsTruncated"):
            truncated = True
        return {"previous_rules": previous, "scanned_objects": scanned,
                "estimated_current_objects_affected": impacted, "truncated": truncated,
                "multipart_and_noncurrent_estimate": "Không thể ước lượng bằng ListObjectsV2"}
    return _with_owner_s3(cluster, payload, inspect)


def _execute_lifecycle(cluster, payload: dict) -> None:
    def execute(client):
        if payload["action"] == "lifecycle_delete":
            client.delete_bucket_lifecycle(Bucket=payload["bucket"])
        else:
            client.put_bucket_lifecycle_configuration(Bucket=payload["bucket"],
                LifecycleConfiguration={"Rules": _boto_lifecycle_rules(payload["rules"])})
    _with_owner_s3(cluster, payload, execute)


def _is_public_principal(value: object) -> bool:
    if value == "*":
        return True
    if isinstance(value, list):
        return any(_is_public_principal(item) for item in value)
    if isinstance(value, dict):
        return any(_is_public_principal(item) for item in value.values())
    return False


def _policy_acl_payload(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action not in {"policy_put", "policy_delete", "acl_set"}:
        raise HTTPException(status_code=400, detail="Thao tác Bucket Policy/ACL không hợp lệ")
    base = _create_payload({"name": body.get("bucket"), "owner": body.get("owner"),
                            "endpoint": body.get("endpoint")})
    payload = {"action": action, "bucket": base["name"], "owner": base["owner"],
               "endpoint": base["endpoint"], "public": False, "required_actions": []}
    if action == "acl_set":
        acl = str(body.get("acl") or "")
        if acl not in {"private", "authenticated-read", "public-read", "public-read-write"}:
            raise HTTPException(status_code=400, detail="Canned ACL không hợp lệ")
        payload["acl"] = acl
        payload["public"] = acl != "private"
        return payload
    if action == "policy_delete":
        payload["policy"] = None
        return payload
    policy = body.get("policy")
    if not isinstance(policy, dict) or policy.get("Version") != "2012-10-17":
        raise HTTPException(status_code=400, detail="Policy cần Version 2012-10-17 và phải là JSON object")
    statements = policy.get("Statement")
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list) or not 1 <= len(statements) <= 100:
        raise HTTPException(status_code=400, detail="Policy cần từ 1 đến 100 Statement")
    bucket_arn = f"arn:aws:s3:::{base['name']}"
    required_actions = set()
    public = False
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") not in {"Allow", "Deny"}:
            raise HTTPException(status_code=400, detail="Statement/Effect không hợp lệ")
        actions = statement.get("Action")
        actions = [actions] if isinstance(actions, str) else actions
        resources = statement.get("Resource")
        resources = [resources] if isinstance(resources, str) else resources
        if not isinstance(actions, list) or not actions or not all(action in REEF_POLICY_ACTIONS for action in actions):
            raise HTTPException(status_code=400, detail="Policy chứa S3 action không có trong allowlist Reef")
        if not isinstance(resources, list) or not resources or not all(
            isinstance(resource, str) and resource in {bucket_arn, bucket_arn + "/*"} for resource in resources
        ):
            raise HTTPException(status_code=400, detail="Policy chỉ được tham chiếu bucket hiện tại")
        if "Principal" not in statement:
            raise HTTPException(status_code=400, detail="Statement thiếu Principal")
        if "${" in json.dumps(statement, ensure_ascii=False):
            raise HTTPException(status_code=400, detail="Ceph RGW không hỗ trợ string interpolation trong policy")
        required_actions.update(actions)
        if statement["Effect"] == "Allow" and _is_public_principal(statement["Principal"]):
            public = True
    encoded = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 64 * 1024:
        raise HTTPException(status_code=400, detail="Policy vượt quá giới hạn 64 KiB của editor")
    payload.update(policy=policy, policy_json=encoded, public=public,
                   required_actions=sorted(required_actions))
    return payload


def _ensure_policy_capability(payload: dict, capability: dict) -> None:
    matrix = capability["bucket_policy_acl"]
    if not matrix["supported"]:
        raise HTTPException(status_code=409, detail=matrix["unavailable_reason"])
    major = int(capability["ceph_major"])
    unavailable = [(action, POLICY_ACTION_MIN_MAJOR[action]) for action in payload["required_actions"]
                   if POLICY_ACTION_MIN_MAJOR[action] > major]
    if unavailable:
        action, minimum = unavailable[0]
        release = {15: "Octopus", 18: "Reef"}.get(minimum, f"major {minimum}")
        raise HTTPException(status_code=409, detail=f"Policy action {action} cần Ceph {release} {minimum} trở lên.")


def _policy_acl_preview(cluster, payload: dict) -> dict:
    def inspect(client):
        current_policy = None
        try:
            raw = client.get_bucket_policy(Bucket=payload["bucket"]).get("Policy")
            current_policy = json.loads(raw) if isinstance(raw, str) else raw
        except client.exceptions.ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code", "")) not in {"NoSuchBucketPolicy", "NoSuchPolicy"}:
                raise
        current_acl = client.get_bucket_acl(Bucket=payload["bucket"])
        safe_acl = {"Owner": current_acl.get("Owner"), "Grants": current_acl.get("Grants") or []}
        candidate = payload.get("policy") if payload["action"].startswith("policy_") else {"canned_acl": payload["acl"]}
        return {"before_policy": current_policy, "before_acl": safe_acl, "after": candidate}
    return _with_owner_s3(cluster, payload, inspect)


def _execute_policy_acl(cluster, payload: dict) -> None:
    def execute(client):
        if payload["action"] == "policy_put":
            client.put_bucket_policy(Bucket=payload["bucket"], Policy=payload["policy_json"])
        elif payload["action"] == "policy_delete":
            client.delete_bucket_policy(Bucket=payload["bucket"])
        else:
            client.put_bucket_acl(Bucket=payload["bucket"], ACL=payload["acl"])
    _with_owner_s3(cluster, payload, execute)


def _delete_payload(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action not in {"delete_empty", "purge_delete"}:
        raise HTTPException(status_code=400, detail="Luồng xóa bucket không hợp lệ")
    base = _create_payload({"name": body.get("bucket"), "owner": body.get("owner"),
                            "endpoint": body.get("endpoint")})
    payload = {"action": action, "bucket": base["name"], "owner": base["owner"],
               "endpoint": base["endpoint"]}
    if body.get("expected_objects") is not None:
        try:
            payload["expected_objects"] = max(0, int(body["expected_objects"]))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Số object xác nhận không hợp lệ") from exc
    return payload


def _delete_bucket_inspect(cluster, payload: dict, detail: dict) -> dict:
    def inspect(client):
        versions = client.list_object_versions(Bucket=payload["bucket"], MaxKeys=1000)
        current = client.list_objects_v2(Bucket=payload["bucket"], MaxKeys=1000)
        version_count = len(versions.get("Versions") or [])
        marker_count = len(versions.get("DeleteMarkers") or [])
        current_count = len(current.get("Contents") or [])
        return {"sample_current_objects": current_count, "sample_versions": version_count,
                "sample_delete_markers": marker_count,
                "sample_truncated": bool(versions.get("IsTruncated") or current.get("IsTruncated"))}
    sample = _with_owner_s3(cluster, payload, inspect)
    count = int(detail.get("num_objects") or 0)
    return {**sample, "object_count": count, "size_bytes": int(detail.get("size_bytes") or 0),
            "size": detail.get("size"), "object_lock_status": detail.get("object_lock_status", "unknown"),
            "empty_delete_allowed": count == 0 and sample["sample_current_objects"] == 0 and
                                    sample["sample_versions"] == 0 and sample["sample_delete_markers"] == 0}


def _execute_delete_bucket(cluster, payload: dict) -> None:
    def execute(client):
        if payload["action"] == "purge_delete":
            for _index in range(MAX_PURGE_BATCHES):
                page = client.list_object_versions(Bucket=payload["bucket"], MaxKeys=1000)
                objects = [{"Key": item["Key"], "VersionId": item["VersionId"]}
                           for item in (page.get("Versions") or []) + (page.get("DeleteMarkers") or [])]
                if not objects:
                    break
                result = client.delete_objects(Bucket=payload["bucket"], Delete={"Objects": objects, "Quiet": True})
                if result.get("Errors"):
                    raise ObjectStorageError("RGW từ chối xóa một hoặc nhiều object version; không tiếp tục xóa bucket.")
            else:
                raise ObjectStorageError("Dừng purge vì vượt giới hạn batch an toàn; bucket chưa bị xóa.")
            for _index in range(MAX_PURGE_BATCHES):
                page = client.list_objects_v2(Bucket=payload["bucket"], MaxKeys=1000)
                objects = [{"Key": item["Key"]} for item in (page.get("Contents") or [])]
                if not objects:
                    break
                result = client.delete_objects(Bucket=payload["bucket"], Delete={"Objects": objects, "Quiet": True})
                if result.get("Errors"):
                    raise ObjectStorageError("RGW từ chối xóa một hoặc nhiều object; không tiếp tục xóa bucket.")
            else:
                raise ObjectStorageError("Dừng purge object vì vượt giới hạn batch an toàn; bucket chưa bị xóa.")
        client.delete_bucket(Bucket=payload["bucket"])
    _with_owner_s3(cluster, payload, execute)


def _format_bytes(value: object) -> str:
    try:
        size = max(0, int(value or 0))
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return "—"


def _rgw_hosts(cluster) -> list[str]:
    nodes = configured_nodes() if cluster.is_default else configured_nodes(cluster)
    return [str(node["host"]) for node in nodes if "RGW" in node["roles"]]


def _list_from_first_reachable_rgw(cluster, hosts: list[str]) -> tuple[str, list[str]]:
    """Use any configured RGW endpoint; bucket metadata is shared per zone."""
    errors = []
    for host in hosts:
        try:
            if cluster.is_default:
                return host, fetch_bucket_list(host)
            ssh_user, ssh_key_path, exec_mode, _mon_container = resolve_ssh_creds(cluster)
            container = cluster.ceph_rgw_container_name
            return host, fetch_bucket_list_with(host, ssh_user, ssh_key_path, exec_mode, container)
        except RgwLogError as exc:
            errors.append(_safe_error(exc))
    raise ObjectStorageError("Không thể lấy danh sách bucket từ RGW. " + "; ".join(errors))


def _bucket_summary(cluster, host: str, name: str) -> dict:
    try:
        if cluster.is_default:
            raw = fetch_bucket_stats(host, name)
        else:
            ssh_user, ssh_key_path, exec_mode, _mon_container = resolve_ssh_creds(cluster)
            container = cluster.ceph_rgw_container_name
            raw = fetch_bucket_stats_with(host, name, ssh_user, ssh_key_path, exec_mode, container)
        if raw is None:
            return {"name": name, "stats_available": False, "stats_error": None}
        result = {"name": name, "stats_available": True, "stats_error": None}
        result.update(summarize_bucket_stats(raw))
        created_at = result.get("creation_time")
        result["creation_time"] = to_utc_iso(created_at) if created_at else None
        result["size"] = _format_bytes(result.get("size_bytes"))
        result["quota_size"] = _format_bytes(result.get("quota_max_size_bytes"))
        return result
    except RgwLogError as exc:
        # A bucket may be removed just after the list query, or one stats call
        # may fail while the remainder of the inventory is useful.  Preserve a
        # per-row unavailable state instead of failing the full page.
        return {"name": name, "stats_available": False, "stats_error": _safe_error(exc)}


def _bucket_activity(cluster, host: str, name: str) -> dict:
    """Best-effort request/error trend from the bounded RGW log excerpt."""
    try:
        if cluster.is_default:
            records = fetch_bucket_access_log(host, name)
        else:
            ssh_user, ssh_key_path, exec_mode, _mon_container = resolve_ssh_creds(cluster)
            records = fetch_bucket_access_log_with(
                host, name, ssh_user, ssh_key_path, exec_mode, cluster.ceph_rgw_container_name
            )
    except RgwLogError as exc:
        return {"available": False, "error": _safe_error(exc), "total": 0, "errors": 0, "points": []}

    hourly: dict[str, dict[str, int | str]] = {}
    for record in records:
        timestamp = record.get("timestamp")
        if timestamp is None:
            continue
        hour = timestamp.replace(minute=0, second=0, microsecond=0).isoformat()
        point = hourly.setdefault(hour, {"hour": hour, "requests": 0, "errors": 0})
        point["requests"] = int(point["requests"]) + 1
        if int(record.get("status") or 0) >= 400:
            point["errors"] = int(point["errors"]) + 1
    errors = sum(1 for record in records if int(record.get("status") or 0) >= 400)
    latest = records[0].get("timestamp") if records else None
    return {
        "available": True,
        "error": None,
        "total": len(records),
        "errors": errors,
        "error_rate": round(errors * 100 / len(records), 1) if records else 0.0,
        "latest_request": latest.isoformat() if latest else None,
        "points": sorted(hourly.values(), key=lambda point: str(point["hour"]))[-12:],
    }


def _inventory(
    cluster,
    query: str,
    page: int,
    owner: str = "",
    quota: QuotaFilter = "all",
    usage: UsageFilter = "all",
    sort: SortField = "name",
    order: SortOrder = "asc",
) -> dict:
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise ObjectStorageError("Chưa cấu hình node RGW cho cluster đang chọn.")
    host, names = _list_from_first_reachable_rgw(cluster, hosts)
    normalized_query = query.strip().casefold()
    if normalized_query:
        names = [name for name in names if normalized_query in name.casefold()]
    normalized_owner = owner.strip().casefold()
    needs_metadata_scan = bool(normalized_owner or quota != "all" or usage != "all" or sort != "name")
    if needs_metadata_scan and len(names) > MAX_METADATA_SCAN:
        raise ObjectStorageError(
            f"Bộ lọc/sắp xếp metadata chỉ hỗ trợ tối đa {MAX_METADATA_SCAN} bucket; "
            "hãy lọc thêm theo tên bucket."
        )

    if needs_metadata_scan:
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(names)))) as executor:
            rows = list(executor.map(lambda name: _bucket_summary(cluster, host, name), names))
        if normalized_owner:
            rows = [row for row in rows if normalized_owner in str(row.get("owner") or "").casefold()]
        if quota != "all":
            expected = quota == "enabled"
            rows = [row for row in rows if row.get("stats_available") and bool(row.get("quota_enabled")) is expected]
        if usage != "all":
            rows = [
                row for row in rows
                if row.get("stats_available") and (int(row.get("size_bytes") or 0) > 0) is (usage == "nonempty")
            ]
        sort_keys = {
            "name": lambda row: str(row.get("name") or "").casefold(),
            "owner": lambda row: str(row.get("owner") or "").casefold(),
            "objects": lambda row: int(row.get("num_objects") or 0),
            "size": lambda row: int(row.get("size_bytes") or 0),
            "created": lambda row: str(row.get("creation_time") or ""),
        }
        rows.sort(key=sort_keys[sort], reverse=order == "desc")
        total = len(rows)
        page_count = max(1, ceil(total / PAGE_SIZE))
        page = min(max(page, 1), page_count)
        rows = rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    else:
        names.sort(key=str.casefold, reverse=order == "desc")
        total = len(names)
        page_count = max(1, ceil(total / PAGE_SIZE))
        page = min(max(page, 1), page_count)
        page_names = names[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(page_names)))) as executor:
            rows = list(executor.map(lambda name: _bucket_summary(cluster, host, name), page_names))
    return {
        "host": host,
        "items": rows,
        "query": query.strip(),
        "owner": owner.strip(),
        "quota": quota,
        "usage": usage,
        "sort": sort,
        "order": order,
        "page": page,
        "page_size": PAGE_SIZE,
        "page_count": page_count,
        "total": total,
    }


def _detail(cluster, name: str, include_activity: bool = False) -> dict:
    bucket_name = name.strip()
    if not bucket_name or len(bucket_name) > 255 or "/" in bucket_name:
        raise HTTPException(status_code=404, detail="Tên bucket không hợp lệ")
    hosts = _rgw_hosts(cluster)
    if not hosts:
        raise ObjectStorageError("Chưa cấu hình node RGW cho cluster đang chọn.")
    host, _names = _list_from_first_reachable_rgw(cluster, hosts)
    result = _bucket_summary(cluster, host, bucket_name)
    if not result["stats_available"]:
        if result["stats_error"]:
            raise ObjectStorageError(result["stats_error"])
        raise HTTPException(status_code=404, detail="Không tìm thấy bucket")
    detail = {"host": host, **result}
    if include_activity:
        detail["activity"] = _bucket_activity(cluster, host, bucket_name)
    return detail


def _object_browser(cluster, bucket: str, marker: str, prefix: str, query: str,
                    page_size: int, sort: str, order: str) -> dict:
    detail = _detail(cluster, bucket)
    host = detail["host"]
    rows: list[dict] = []
    cursor = marker
    continuation_marker = marker
    last_return_marker = marker
    scanned = 0
    truncated = False
    while len(rows) <= page_size and scanned < MAX_OBJECT_BROWSER_SCAN:
        limit = min(101, MAX_OBJECT_BROWSER_SCAN - scanned)
        if cluster.is_default:
            chunk = fetch_bucket_objects(host, bucket, cursor, limit)
        else:
            ssh_user, ssh_key, mode, _container = resolve_ssh_creds(cluster)
            chunk = fetch_bucket_objects_with(host, bucket, cursor, limit, ssh_user, ssh_key,
                                                mode, cluster.ceph_rgw_container_name)
        if not chunk:
            break
        scanned += len(chunk)
        cursor = str(chunk[-1]["name"])
        for raw in chunk:
            name = str(raw.get("name") or "")
            continuation_marker = name
            if prefix and not name.startswith(prefix):
                continue
            if query and query.casefold() not in name.casefold():
                continue
            if len(rows) >= page_size:
                truncated = True
                continuation_marker = last_return_marker
                break
            meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
            rows.append({
                "key": name, "size_bytes": int(meta.get("size") or 0),
                "size": _format_bytes(meta.get("size")), "content_type": meta.get("content_type") or "—",
                "last_modified": str(meta.get("mtime") or ""), "etag": meta.get("etag") or None,
                "version_id": str(raw.get("instance") or "") or None,
            })
            last_return_marker = name
        if len(chunk) < limit or truncated:
            break
    if scanned >= MAX_OBJECT_BROWSER_SCAN and len(rows) <= page_size:
        truncated = True
    visible = rows[:page_size]
    sort_keys = {"key": lambda row: row["key"].casefold(),
                 "size": lambda row: row["size_bytes"],
                 "modified": lambda row: row["last_modified"]}
    visible.sort(key=sort_keys[sort], reverse=order == "desc")
    next_marker = continuation_marker if truncated and visible else None
    return {"bucket": bucket, "items": visible, "prefix": prefix, "query": query,
            "marker": marker or None, "next_marker": next_marker, "truncated": truncated,
            "scanned": scanned, "page_size": page_size, "sort": sort, "order": order,
            "filter_scan_limit": MAX_OBJECT_BROWSER_SCAN}


def _object_detail(cluster, bucket: str, key: str, version_id: str, owner: str,
                   endpoint: str, capability: dict) -> dict:
    if not key or len(key) > 1024 or any(ord(char) < 32 for char in key):
        raise HTTPException(status_code=400, detail="Object key không hợp lệ")
    base = _create_payload({"name": bucket, "owner": owner, "endpoint": endpoint})
    bucket_detail = _detail(cluster, bucket)
    if bucket_detail.get("owner") != owner:
        raise HTTPException(status_code=409, detail="Owner không khớp bucket trên cluster đang chọn")
    payload = {"bucket": base["name"], "owner": base["owner"], "endpoint": base["endpoint"]}
    browser = capability["object_browser"]

    def read(client):
        version_args = {"VersionId": version_id} if version_id else {}
        head = client.head_object(Bucket=bucket, Key=key, **version_args)
        tags = None
        retention = None
        legal_hold = None
        if browser["tags_supported"]:
            try:
                tags = client.get_object_tagging(Bucket=bucket, Key=key, **version_args).get("TagSet") or []
            except client.exceptions.ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code not in {"NoSuchTagSet", "NoSuchKey"}:
                    raise
                tags = []
        if browser["retention_supported"]:
            try:
                retention = client.get_object_retention(Bucket=bucket, Key=key, **version_args).get("Retention")
            except client.exceptions.ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code not in {"NoSuchObjectLockConfiguration", "ObjectLockConfigurationNotFoundError", "NoSuchKey"}:
                    raise
            try:
                legal_hold = client.get_object_legal_hold(Bucket=bucket, Key=key, **version_args).get("LegalHold")
            except client.exceptions.ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code not in {"NoSuchObjectLockConfiguration", "ObjectLockConfigurationNotFoundError", "NoSuchKey"}:
                    raise
        modified = head.get("LastModified")
        return {
            "key": key, "version_id": head.get("VersionId") or version_id or None,
            "size_bytes": int(head.get("ContentLength") or 0), "size": _format_bytes(head.get("ContentLength")),
            "content_type": head.get("ContentType") or None, "etag": str(head.get("ETag") or "").strip('"') or None,
            "last_modified": modified.isoformat() if isinstance(modified, datetime) else str(modified or "") or None,
            "storage_class": head.get("StorageClass") or "STANDARD",
            "cache_control": head.get("CacheControl"), "content_disposition": head.get("ContentDisposition"),
            "metadata": {str(k): str(v) for k, v in (head.get("Metadata") or {}).items()},
            "tags": tags, "tags_supported": browser["tags_supported"],
            "tags_unavailable_reason": browser["tags_unavailable_reason"],
            "retention": retention, "legal_hold": legal_hold,
            "retention_supported": browser["retention_supported"],
            "retention_unavailable_reason": browser["retention_unavailable_reason"],
        }
    return _with_owner_s3(cluster, payload, read)


def _presigned_payload(body: dict) -> dict:
    action = str(body.get("action") or "")
    if action not in {"upload", "download"}:
        raise HTTPException(status_code=400, detail="Thao tác presigned URL không hợp lệ")
    base = _create_payload({"name": body.get("bucket"), "owner": body.get("owner"),
                            "endpoint": body.get("endpoint")})
    key = str(body.get("key") or "")
    access_key = str(body.get("access_key") or "").strip()
    secret_key = str(body.get("secret_key") or "")
    try:
        expires = int(body.get("expires_seconds", 300))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Thời hạn URL không hợp lệ") from exc
    if not key or len(key) > 1024 or any(ord(char) < 32 for char in key):
        raise HTTPException(status_code=400, detail="Object key không hợp lệ")
    if not access_key or len(access_key) > 256 or not secret_key or len(secret_key) > 2048:
        raise HTTPException(status_code=400, detail="S3 credential không hợp lệ")
    if not 60 <= expires <= MAX_PRESIGNED_EXPIRY_SECONDS:
        raise HTTPException(status_code=400, detail="URL chỉ được có thời hạn 60–900 giây")
    payload = {"action": action, "bucket": base["name"], "owner": base["owner"],
               "endpoint": base["endpoint"], "key": key, "access_key": access_key,
               "secret_key": secret_key, "expires_seconds": expires}
    version_id = str(body.get("version_id") or "")
    if version_id:
        if action != "download" or len(version_id) > 1024 or any(ord(c) < 32 for c in version_id):
            raise HTTPException(status_code=400, detail="Version ID không hợp lệ")
        payload["version_id"] = version_id
    if action == "upload":
        content_type = str(body.get("content_type") or "")
        try:
            max_bytes = int(body.get("max_bytes"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Giới hạn upload không hợp lệ") from exc
        if content_type not in PRESIGNED_UPLOAD_TYPES:
            raise HTTPException(status_code=400, detail="Content-Type upload không nằm trong allowlist")
        if not 1 <= max_bytes <= MAX_PRESIGNED_UPLOAD_BYTES:
            raise HTTPException(status_code=400, detail="Upload phải giới hạn từ 1 byte đến 5 GiB")
        payload.update(content_type=content_type, max_bytes=max_bytes)
    return payload


def _presigned_result(cluster, payload: dict) -> dict:
    detail = _detail(cluster, payload["bucket"])
    if detail.get("owner") != payload["owner"]:
        raise HTTPException(status_code=409, detail="Owner không khớp bucket trên cluster đang chọn")
    client = boto3.client("s3", endpoint_url=payload["endpoint"],
        aws_access_key_id=payload["access_key"], aws_secret_access_key=payload["secret_key"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))
    if payload["action"] == "upload":
        return client.generate_presigned_post(Bucket=payload["bucket"], Key=payload["key"],
            Fields={"Content-Type": payload["content_type"]},
            Conditions=[{"Content-Type": payload["content_type"]},
                        ["content-length-range", 1, payload["max_bytes"]]],
            ExpiresIn=payload["expires_seconds"])
    params = {"Bucket": payload["bucket"], "Key": payload["key"]}
    if payload.get("version_id"):
        params["VersionId"] = payload["version_id"]
    return {"url": client.generate_presigned_url("get_object", Params=params,
                                                   ExpiresIn=payload["expires_seconds"])}


@router.get("/api/object-storage/buckets")
async def bucket_inventory_api(
    request: Request,
    query: str = Query("", max_length=MAX_QUERY_LENGTH),
    page: int = Query(1, ge=1),
    owner: str = Query("", max_length=MAX_QUERY_LENGTH),
    quota: QuotaFilter = "all",
    usage: UsageFilter = "all",
    sort: SortField = "name",
    order: SortOrder = "asc",
    user: str = Depends(require_login),
):
    del user
    try:
        return await asyncio.to_thread(
            _inventory, selected_cluster(request), query, page, owner, quota, usage, sort, order
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/object-storage/capabilities")
async def capabilities_api(request: Request, user: str = Depends(require_login)):
    del user
    try:
        return await asyncio.to_thread(_capabilities, selected_cluster(request))
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/object-storage/buckets/actions/preview")
async def bucket_create_preview(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được tạo bucket")
    payload = _create_payload(await request.json())
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        host = _rgw_hosts(cluster)[0]
        owner = await asyncio.to_thread(_owner_info, cluster, host, payload["owner"])
    except (ObjectStorageError, RgwLogError, IndexError) as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    _ensure_capability(payload, capability)
    if not owner:
        raise HTTPException(status_code=404, detail="Không tìm thấy owner S3 trên cluster đang chọn")
    if bool(owner.get("suspended")):
        raise HTTPException(status_code=409, detail="Owner S3 đang bị vô hiệu hóa")
    location = f"{payload['api_name']}:{payload['placement']}" if payload["placement"] else "mặc định của owner/zonegroup"
    return {"action": "create_bucket", "cluster_id": cluster.id, "cluster_name": cluster.name,
        "ceph_version": capability["ceph_version"], "ceph_release": capability["ceph_release"],
        "risk": "medium", "confirmation_required": payload["name"],
        "preview": f"S3 CreateBucket '{payload['name']}' cho owner '{payload['owner']}', placement {location}",
        "temporary_key": "Access key tạm được tạo nội bộ và thu hồi ngay sau request S3."}


@router.post("/api/object-storage/buckets/actions/execute")
async def bucket_create_execute(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được tạo bucket")
    body = await request.json()
    payload = _create_payload(body)
    if str(body.get("confirmation") or "") != payload["name"]:
        raise HTTPException(status_code=400, detail="Nhập chính xác tên bucket để xác nhận")
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        _ensure_capability(payload, capability)
        audit_id = await asyncio.to_thread(_bucket_audit_start, cluster.id, user, payload)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cannot persist create bucket audit entry")
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        await asyncio.to_thread(_create_bucket, cluster, payload)
    except (ObjectStorageError, RgwLogError) as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", safe_error)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_bucket_audit_finish, audit_id, "succeeded")
    return {"ok": True, "bucket": payload["name"], "owner": payload["owner"], "request_id": audit_id}


def _governance_preview(payload: dict) -> str:
    labels = {
        "quota_enable": "Bật enforcement quota",
        "quota_disable": "Tắt enforcement quota (giữ nguyên giới hạn)",
        "versioning_enable": "Bật versioning cho object mới",
        "versioning_suspend": "Suspend versioning; version cũ không bị xóa",
    }
    if payload["action"] == "quota_set":
        return (f"Đặt quota bucket max_size={payload['max_size_bytes']} bytes, "
                f"max_objects={payload['max_objects']}; cần enable để enforcement")
    if payload["action"] == "retention_set":
        return (f"Đặt default retention {payload['mode']} trong {payload['days']} ngày; "
                "chỉ thành công nếu Object Lock đã bật lúc tạo bucket")
    return labels[payload["action"]]


def _validate_governance_target(payload: dict, detail: dict) -> None:
    if payload["action"].startswith("versioning_") or payload["action"] == "retention_set":
        if payload["owner"] != str(detail.get("owner") or ""):
            raise HTTPException(status_code=409, detail="Owner UID không khớp owner hiện tại của bucket")
    if payload["action"] == "retention_set" and detail.get("object_lock_status") == "disabled":
        raise HTTPException(status_code=409, detail="Bucket không bật Object Lock lúc tạo; không thể cấu hình retention")


@router.post("/api/object-storage/buckets/governance/preview")
async def bucket_governance_preview(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi quản trị bucket")
    payload = _governance_payload(await request.json())
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    _ensure_capability(payload, capability)
    _validate_governance_target(payload, detail)
    risk = "high" if payload["action"] in {"quota_disable", "versioning_suspend", "retention_set"} else "medium"
    return {"action": payload["action"], "bucket": payload["bucket"], "owner": detail.get("owner"),
        "cluster_id": cluster.id, "cluster_name": cluster.name, "ceph_version": capability["ceph_version"],
        "ceph_release": capability["ceph_release"], "risk": risk,
        "confirmation_required": payload["bucket"], "preview": _governance_preview(payload),
        "object_lock_status": detail.get("object_lock_status", "unknown")}


@router.post("/api/object-storage/buckets/governance/execute")
async def bucket_governance_execute(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi quản trị bucket")
    body = await request.json()
    payload = _governance_payload(body)
    if str(body.get("confirmation") or "") != payload["bucket"]:
        raise HTTPException(status_code=400, detail="Nhập chính xác tên bucket để xác nhận")
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        _ensure_capability(payload, capability)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
        _validate_governance_target(payload, detail)
        audit_id = await asyncio.to_thread(
            _start_governance_audit, cluster.id, user, payload, _governance_preview(payload)
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cannot persist bucket governance audit entry")
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        await asyncio.to_thread(_execute_governance, cluster, payload)
    except (ObjectStorageError, RgwLogError) as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", safe_error)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_bucket_audit_finish, audit_id, "succeeded")
    return {"ok": True, "action": payload["action"], "bucket": payload["bucket"], "request_id": audit_id}


@router.post("/api/object-storage/buckets/lifecycle/preview")
async def bucket_lifecycle_preview(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi lifecycle")
    payload = _lifecycle_payload(await request.json())
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        _ensure_lifecycle_capability(payload, capability)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
        _validate_governance_target({**payload, "action": "versioning_enable"}, detail)
        dry_run = await asyncio.to_thread(_lifecycle_dry_run, cluster, payload)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    return {"action": payload["action"], "bucket": payload["bucket"], "cluster_id": cluster.id,
        "cluster_name": cluster.name, "ceph_version": capability["ceph_version"],
        "ceph_release": capability["ceph_release"],
        "risk": "high" if payload["action"] == "lifecycle_delete" else "medium",
        "confirmation_required": payload["bucket"], "rules": payload["rules"], "dry_run": dry_run}


@router.post("/api/object-storage/buckets/lifecycle/execute")
async def bucket_lifecycle_execute(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi lifecycle")
    body = await request.json()
    payload = _lifecycle_payload(body)
    if str(body.get("confirmation") or "") != payload["bucket"]:
        raise HTTPException(status_code=400, detail="Nhập chính xác tên bucket để xác nhận")
    cluster = selected_cluster(request)
    preview = ("Xóa toàn bộ lifecycle configuration" if payload["action"] == "lifecycle_delete"
               else "Áp dụng lifecycle rules: " + json.dumps(payload["rules"], ensure_ascii=False, separators=(",", ":")))
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        _ensure_lifecycle_capability(payload, capability)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
        _validate_governance_target({**payload, "action": "versioning_enable"}, detail)
        audit_id = await asyncio.to_thread(_start_governance_audit, cluster.id, user, payload, preview)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cannot persist lifecycle audit entry")
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        await asyncio.to_thread(_execute_lifecycle, cluster, payload)
    except ObjectStorageError as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", safe_error)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_bucket_audit_finish, audit_id, "succeeded")
    return {"ok": True, "action": payload["action"], "bucket": payload["bucket"], "request_id": audit_id}


@router.post("/api/object-storage/buckets/policy-acl/preview")
async def bucket_policy_acl_preview(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi Bucket Policy/ACL")
    payload = _policy_acl_payload(await request.json())
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        _ensure_policy_capability(payload, capability)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
        _validate_governance_target({**payload, "action": "versioning_enable"}, detail)
        diff = await asyncio.to_thread(_policy_acl_preview, cluster, payload)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    confirmation = f"PUBLIC:{payload['bucket']}" if payload["public"] else payload["bucket"]
    return {"action": payload["action"], "bucket": payload["bucket"], "cluster_id": cluster.id,
        "cluster_name": cluster.name, "ceph_version": capability["ceph_version"],
        "ceph_release": capability["ceph_release"], "public_access": payload["public"],
        "risk": "high" if payload["public"] else "medium", "confirmation_required": confirmation,
        "diff": diff, "warning": "Policy/ACL cho phép truy cập rộng hoặc anonymous." if payload["public"] else None}


@router.post("/api/object-storage/buckets/policy-acl/execute")
async def bucket_policy_acl_execute(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được thay đổi Bucket Policy/ACL")
    body = await request.json()
    payload = _policy_acl_payload(body)
    expected = f"PUBLIC:{payload['bucket']}" if payload["public"] else payload["bucket"]
    if str(body.get("confirmation") or "") != expected:
        raise HTTPException(status_code=400, detail=f"Nhập chính xác {expected} để xác nhận")
    cluster = selected_cluster(request)
    preview = (f"{payload['action']} public={payload['public']} " +
               (payload.get("policy_json") or str(payload.get("acl") or "delete")))
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        _ensure_policy_capability(payload, capability)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
        _validate_governance_target({**payload, "action": "versioning_enable"}, detail)
        audit_id = await asyncio.to_thread(_start_governance_audit, cluster.id, user, payload, preview)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("cannot persist bucket policy/acl audit entry")
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        await asyncio.to_thread(_execute_policy_acl, cluster, payload)
    except ObjectStorageError as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", safe_error)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_bucket_audit_finish, audit_id, "succeeded")
    return {"ok": True, "action": payload["action"], "bucket": payload["bucket"], "request_id": audit_id}


@router.post("/api/object-storage/buckets/delete/preview")
async def bucket_delete_preview(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được xóa bucket")
    payload = _delete_payload(await request.json())
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
        _validate_governance_target({**payload, "action": "versioning_enable"}, detail)
        impact = await asyncio.to_thread(_delete_bucket_inspect, cluster, payload, detail)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    if payload["action"] == "delete_empty" and not impact["empty_delete_allowed"]:
        allowed = False
        reason = "Bucket còn object/version/delete-marker; phải dùng luồng Purge & Delete riêng."
    else:
        allowed = True
        reason = None
    confirmation = (payload["bucket"] if payload["action"] == "delete_empty" else
                    f"PURGE:{payload['bucket']}:{impact['object_count']}")
    return {"action": payload["action"], "bucket": payload["bucket"], "cluster_id": cluster.id,
        "cluster_name": cluster.name, "ceph_version": capability["ceph_version"],
        "ceph_release": capability["ceph_release"], "risk": "critical" if payload["action"] == "purge_delete" else "high",
        "allowed": allowed, "blocked_reason": reason, "confirmation_required": confirmation,
        "expected_objects": impact["object_count"], "impact": impact,
        "retention_warning": "Không bypass Object Lock/Governance/Compliance retention; RGW sẽ từ chối object đang khóa."}


@router.post("/api/object-storage/buckets/delete/execute")
async def bucket_delete_execute(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được xóa bucket")
    body = await request.json()
    payload = _delete_payload(body)
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
        _validate_governance_target({**payload, "action": "versioning_enable"}, detail)
        impact = await asyncio.to_thread(_delete_bucket_inspect, cluster, payload, detail)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    del capability
    if payload["action"] == "delete_empty":
        if not impact["empty_delete_allowed"]:
            raise HTTPException(status_code=409, detail="Bucket không rỗng; từ chối luồng delete-empty")
        expected_confirmation = payload["bucket"]
    else:
        if "expected_objects" not in payload or payload["expected_objects"] != impact["object_count"]:
            raise HTTPException(status_code=409, detail="Số object đã thay đổi; cần preview lại trước khi purge")
        expected_confirmation = f"PURGE:{payload['bucket']}:{impact['object_count']}"
    if str(body.get("confirmation") or "") != expected_confirmation:
        raise HTTPException(status_code=400, detail=f"Nhập chính xác {expected_confirmation} để xác nhận")
    preview = (f"{payload['action']} objects={impact['object_count']} size_bytes={impact['size_bytes']} "
               f"versions_sample={impact['sample_versions']} delete_markers_sample={impact['sample_delete_markers']}")
    try:
        audit_id = await asyncio.to_thread(_start_governance_audit, cluster.id, user, payload, preview)
    except Exception as exc:
        logger.exception("cannot persist delete bucket audit entry")
        raise HTTPException(status_code=503, detail="Không ghi được audit; thao tác đã bị từ chối") from exc
    try:
        await asyncio.to_thread(_execute_delete_bucket, cluster, payload)
    except ObjectStorageError as exc:
        safe_error = _safe_error(exc)
        await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", safe_error)
        raise HTTPException(status_code=502, detail=safe_error) from exc
    await asyncio.to_thread(_bucket_audit_finish, audit_id, "succeeded")
    return {"ok": True, "action": payload["action"], "bucket": payload["bucket"], "request_id": audit_id}


@router.get("/api/object-storage/buckets/{bucket}")
async def bucket_detail_api(request: Request, bucket: str, user: str = Depends(require_login)):
    del user
    try:
        return await asyncio.to_thread(_detail, selected_cluster(request), bucket)
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/api/object-storage/buckets/{bucket}/objects")
async def bucket_objects_api(
    request: Request,
    bucket: str,
    user: str = Depends(require_login),
    marker: str = Query("", max_length=1024),
    prefix: str = Query("", max_length=1024),
    query: str = Query("", max_length=MAX_QUERY_LENGTH),
    page_size: int = Query(50, ge=1, le=100),
    sort: Literal["key", "size", "modified"] = "key",
    order: SortOrder = "asc",
):
    del user
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        browser = capability["object_browser"]
        if not browser["supported"]:
            raise HTTPException(status_code=409, detail=browser["unavailable_reason"])
        result = await asyncio.to_thread(
            _object_browser, cluster, bucket, marker, prefix, query.strip(), page_size, sort, order
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    result.update(cluster_id=cluster.id, cluster_name=cluster.name,
                  ceph_version=capability["ceph_version"], ceph_release=capability["ceph_release"])
    return result


@router.get("/api/object-storage/buckets/{bucket}/object-detail")
async def bucket_object_detail_api(
    request: Request,
    bucket: str,
    key: str = Query(..., min_length=1, max_length=1024),
    owner: str = Query(..., min_length=1, max_length=255),
    endpoint: str = Query(..., min_length=1, max_length=2048),
    version_id: str = Query("", max_length=1024),
    user: str = Depends(require_login),
):
    del user
    cluster = selected_cluster(request)
    try:
        capability = await asyncio.to_thread(_capabilities, cluster)
        browser = capability["object_browser"]
        if not browser["supported"]:
            raise HTTPException(status_code=409, detail=browser["unavailable_reason"])
        result = await asyncio.to_thread(
            _object_detail, cluster, bucket, key, version_id, owner, endpoint, capability
        )
    except ObjectStorageError as exc:
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    result.update(cluster_id=cluster.id, cluster_name=cluster.name,
                  ceph_version=capability["ceph_version"], ceph_release=capability["ceph_release"])
    return result


@router.post("/api/object-storage/objects/presign/preview")
async def object_presign_preview(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được tạo presigned URL")
    payload = _presigned_payload(await request.json())
    cluster = selected_cluster(request)
    capability = await asyncio.to_thread(_capabilities, cluster)
    if not capability["object_browser"]["supported"]:
        raise HTTPException(status_code=409, detail=capability["object_browser"]["unavailable_reason"])
    detail = await asyncio.to_thread(_detail, cluster, payload["bucket"])
    if detail.get("owner") != payload["owner"]:
        raise HTTPException(status_code=409, detail="Owner không khớp bucket trên cluster đang chọn")
    return {"action": payload["action"], "bucket": payload["bucket"], "key": payload["key"],
            "version_id": payload.get("version_id"), "expires_seconds": payload["expires_seconds"],
            "content_type": payload.get("content_type"), "max_bytes": payload.get("max_bytes"),
            "confirmation_required": payload["key"], "cluster_id": cluster.id,
            "cluster_name": cluster.name, "ceph_version": capability["ceph_version"],
            "risk": "medium", "credential_handling": "Secret chỉ dùng trong request ký URL; không lưu/audit/trả về."}


@router.post("/api/object-storage/objects/presign/execute")
async def object_presign_execute(request: Request, user: str = Depends(require_login)):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được tạo presigned URL")
    body = await request.json()
    payload = _presigned_payload(body)
    if str(body.get("confirmation") or "") != payload["key"]:
        raise HTTPException(status_code=400, detail="Nhập chính xác object key để xác nhận")
    cluster = selected_cluster(request)
    capability = await asyncio.to_thread(_capabilities, cluster)
    if not capability["object_browser"]["supported"]:
        raise HTTPException(status_code=409, detail=capability["object_browser"]["unavailable_reason"])
    preview = (f"presign_{payload['action']} bucket={payload['bucket']} key={payload['key']} "
               f"version={payload.get('version_id') or 'current'} expires={payload['expires_seconds']}s "
               f"content_type={payload.get('content_type') or '-'} max_bytes={payload.get('max_bytes') or '-'}")
    audit_payload = {"action": f"presign_{payload['action']}", "bucket": payload["bucket"]}
    try:
        audit_id = await asyncio.to_thread(_start_governance_audit, cluster.id, user, audit_payload, preview)
        result = await asyncio.to_thread(_presigned_result, cluster, payload)
    except ObjectStorageError as exc:
        if 'audit_id' in locals():
            await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", _safe_error(exc))
        raise HTTPException(status_code=502, detail=_safe_error(exc)) from exc
    except HTTPException:
        if 'audit_id' in locals():
            await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", "Target validation failed")
        raise
    except Exception as exc:
        if 'audit_id' in locals():
            await asyncio.to_thread(_bucket_audit_finish, audit_id, "failed", "Không ký được presigned URL")
        raise HTTPException(status_code=502, detail="Không ký được presigned URL") from exc
    await asyncio.to_thread(_bucket_audit_finish, audit_id, "succeeded")
    return {"ok": True, "action": payload["action"], "bucket": payload["bucket"],
            "key": payload["key"], "expires_seconds": payload["expires_seconds"],
            "request_id": audit_id, **result}


@router.get("/object-storage/buckets", response_class=HTMLResponse)
async def bucket_inventory_page(
    request: Request,
    user: str = Depends(require_login),
    query: str = Query("", max_length=MAX_QUERY_LENGTH),
    page: int = Query(1, ge=1),
    owner: str = Query("", max_length=MAX_QUERY_LENGTH),
    quota: QuotaFilter = "all",
    usage: UsageFilter = "all",
    sort: SortField = "name",
    order: SortOrder = "asc",
):
    clusters, cluster = cluster_selection(request)
    inventory = {"items": [], "query": query.strip(), "page": page, "page_count": 1, "total": 0}
    error = None
    try:
        inventory = await asyncio.to_thread(_inventory, cluster, query, page, owner, quota, usage, sort, order)
    except ObjectStorageError as exc:
        error = str(exc)
    return templates.TemplateResponse(request, "object_storage_buckets.html", {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "inventory": inventory,
        "error": error,
        "quote_bucket": lambda value: quote(value, safe=""),
        "quote_query": lambda value: quote(value, safe=""),
    })


@router.get("/object-storage/buckets/{bucket}", response_class=HTMLResponse)
async def bucket_detail_page(request: Request, bucket: str, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    detail = None
    error = None
    try:
        detail = await asyncio.to_thread(_detail, cluster, bucket, True)
    except ObjectStorageError as exc:
        error = str(exc)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        error = str(exc.detail)
    return templates.TemplateResponse(request, "object_storage_bucket_detail.html", {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": clusters,
        "selected_cluster": cluster,
        "bucket": bucket,
        "detail": detail,
        "error": error,
        "quote_bucket": lambda value: quote(value, safe=""),
    })
