"""Canonical, signed cluster scope shared by Telegram and Full Executor."""

from __future__ import annotations

import hashlib
import hmac
import json


SCOPE_KEYS = frozenset({
    "cluster_id", "cluster_ref", "name", "database_source", "database_url",
    "ceph_mon_nodes",
    "ceph_mon_hostnames", "ceph_mgr_nodes", "ceph_osd_nodes",
    "ceph_rgw_nodes", "ceph_exec_mode", "ceph_container_name",
    "ceph_osd_container_name",
    "ceph_rgw_container_name", "ssh_user", "ssh_key_path",
    "ceph_keyring_path",
})
REQUIRED_SCOPE_KEYS = frozenset({
    "cluster_id", "cluster_ref", "name", "database_source", "database_url", "ceph_mon_nodes",
})


def normalize_scope(value: dict[str, str]) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError("cluster scope phải là object")
    unknown = set(value) - SCOPE_KEYS
    if unknown:
        raise ValueError(f"cluster scope có trường không hợp lệ: {sorted(unknown)}")
    normalized = {key: str(value.get(key) or "") for key in SCOPE_KEYS if key in value}
    if not REQUIRED_SCOPE_KEYS.issubset(normalized):
        raise ValueError("cluster scope thiếu định danh hoặc MON của cụm")
    if any(not normalized[key].strip() for key in REQUIRED_SCOPE_KEYS):
        raise ValueError("cluster scope thiếu định danh hoặc MON của cụm")
    if any(len(item) > 2000 for item in normalized.values()):
        raise ValueError("cluster scope có giá trị quá dài")
    return {key: normalized[key] for key in sorted(normalized)}


def canonical_scope(value: dict[str, str]) -> bytes:
    return json.dumps(
        normalize_scope(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sign_scope(value: dict[str, str], secret: str) -> str:
    if not secret:
        raise ValueError("scope signing secret không được để trống")
    return hmac.new(secret.encode("utf-8"), canonical_scope(value), hashlib.sha256).hexdigest()


def verify_scope(value: dict[str, str], signature: str, secret: str) -> bool:
    try:
        expected = sign_scope(value, secret)
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(expected, str(signature or "").strip().lower())
