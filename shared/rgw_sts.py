"""RGW STS temporary-credential exchange adapter.

The adapter deliberately keeps the web-identity token and returned credential
secrets in memory only.  Callers must persist session metadata separately and
must never put the returned secret/session token in logs or audit evidence.
"""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlsplit

import boto3

from config.settings import settings


class StsIssueError(RuntimeError):
    """A safe, credential-free STS exchange error."""


_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9+=,.@_-]{2,64}$")


def role_arn(role_name: str) -> str:
    if not re.fullmatch(r"ceph-ai-[A-Za-z0-9][A-Za-z0-9_.:-]{1,127}", role_name or ""):
        raise StsIssueError("RGW role name không hợp lệ")
    return f"arn:aws:iam:::role/{role_name}"


def validate_session_name(value: str) -> str:
    name = str(value or "").strip()
    if not _SESSION_NAME_RE.fullmatch(name):
        raise StsIssueError("session_name chỉ được chứa A-Z, a-z, số và +=,.@_-; dài 2-64 ký tự")
    return name


def validate_tags(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 20:
        raise StsIssueError("session_tags phải là object, tối đa 20 tag")
    tags: dict[str, str] = {}
    for key, raw in value.items():
        tag_key = str(key).strip()
        tag_value = str(raw).strip()
        if not re.fullmatch(r"[A-Za-z0-9_.:/+=@-]{1,128}", tag_key):
            raise StsIssueError("session tag key không hợp lệ")
        if not tag_value or len(tag_value) > 256:
            raise StsIssueError("session tag value không hợp lệ")
        if any(word in tag_key.casefold() for word in ("password", "secret", "token")):
            raise StsIssueError("session tag không được chứa thông tin secret")
        tags[tag_key] = tag_value
    return tags


def _endpoint(endpoint_url: str) -> str:
    endpoint = str(endpoint_url or settings.ceph_rgw_s3_endpoint or "").strip().rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.query or parsed.fragment:
        raise StsIssueError("RGW STS endpoint chưa được cấu hình hợp lệ")
    return endpoint


def _expiration(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    text = str(value or "").strip()
    if not text:
        raise StsIssueError("RGW STS không trả thời điểm hết hạn")
    return text


def assume_role_with_web_identity(
    *,
    role_name: str,
    session_name: str,
    web_identity_token: str,
    duration_seconds: int,
    session_tags: dict[str, str] | None = None,
    endpoint_url: str = "",
    region_name: str = "us-east-1",
    verify_tls: bool = True,
) -> dict:
    """Exchange an external identity token for one RGW STS credential set."""
    session_name = validate_session_name(session_name)
    token = str(web_identity_token or "").strip()
    if not token or len(token) > 16384:
        raise StsIssueError("web_identity_token không hợp lệ")
    if not 900 <= int(duration_seconds) <= 43200:
        raise StsIssueError("duration_seconds phải trong khoảng 900-43200")
    tags = validate_tags(session_tags)
    access_key = str(settings.ceph_rgw_s3_access_key or "").strip()
    secret_key = str(settings.ceph_rgw_s3_secret_key or "").strip()
    if not access_key or not secret_key:
        raise StsIssueError("Ceph RGW S3 credential của ceph-ai chưa được cấu hình")
    endpoint = _endpoint(endpoint_url)
    client = boto3.client(
        "sts",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=(str(region_name or "us-east-1").strip() or "us-east-1"),
        verify=bool(verify_tls),
    )
    request = {
        "RoleArn": role_arn(role_name),
        "RoleSessionName": session_name,
        "WebIdentityToken": token,
        "DurationSeconds": int(duration_seconds),
    }
    if tags:
        request["Tags"] = [{"Key": key, "Value": value} for key, value in tags.items()]
    try:
        response = client.assume_role_with_web_identity(**request)
    except Exception as exc:
        # Botocore errors can contain request details; never return the token or
        # full provider response to the API/audit layer.
        raise StsIssueError(f"RGW STS assume role thất bại: {type(exc).__name__}") from exc
    credentials = response.get("Credentials") if isinstance(response, dict) else None
    if not isinstance(credentials, dict):
        raise StsIssueError("RGW STS response thiếu Credentials")
    required = ("AccessKeyId", "SecretAccessKey", "SessionToken")
    if any(not str(credentials.get(key) or "") for key in required):
        raise StsIssueError("RGW STS response thiếu credential field")
    return {
        "access_key_id": str(credentials["AccessKeyId"]),
        "secret_access_key": str(credentials["SecretAccessKey"]),
        "session_token": str(credentials["SessionToken"]),
        "expiration": _expiration(credentials.get("Expiration")),
        "role_arn": role_arn(role_name),
    }
