"""Deterministic, read-only diagnosis for RGW bucket access failures."""

from __future__ import annotations

from collections import Counter


MUTATING_OPERATIONS = {"create", "delete"}
KNOWN_OPERATIONS = {"list", "create", "delete", "access"}


def _operation(value: str | None) -> str:
    normalized = str(value or "access").strip().lower()
    return normalized if normalized in KNOWN_OPERATIONS else "access"


def _status_counts(records: list[dict]) -> Counter:
    counts = Counter()
    for record in records:
        try:
            status = int(record.get("status"))
        except (TypeError, ValueError):
            continue
        counts[status] += 1
    return counts


def _quota_full(bucket_stats: dict | None) -> bool:
    if not isinstance(bucket_stats, dict) or not bucket_stats.get("quota_enabled"):
        return False
    size = bucket_stats.get("size_bytes")
    max_size = bucket_stats.get("quota_max_size_bytes")
    objects = bucket_stats.get("num_objects")
    max_objects = bucket_stats.get("quota_max_objects")
    return bool(
        isinstance(size, (int, float)) and isinstance(max_size, (int, float))
        and max_size >= 0 and size >= max_size
    ) or bool(
        isinstance(objects, (int, float)) and isinstance(max_objects, (int, float))
        and max_objects >= 0 and objects >= max_objects
    )


def _finding(code: str, severity: str, summary: str, evidence: list[str], next_check: str) -> dict:
    return {
        "code": code,
        "severity": severity,
        "summary": summary,
        "evidence": evidence,
        "next_check": next_check,
        "read_only": True,
        "action_id": None,
    }


def build_bucket_access_diagnosis(
    records: list[dict] | None,
    *,
    bucket: str | None = None,
    operation: str | None = None,
    bucket_stats: dict | None = None,
    rgw_evidence: dict | None = None,
    admin_query_status: str = "not_attempted",
) -> dict:
    """Classify observed RGW access outcomes without claiming an unobserved cause.

    Access logs can prove an HTTP outcome, but they cannot by themselves prove
    DNS/TLS failure or distinguish every 403 policy case.  Those limits are
    returned as evidence gaps instead of being filled with a guess.
    """
    records = [record for record in (records or []) if isinstance(record, dict)]
    selected_operation = _operation(operation)
    counts = _status_counts(records)
    errors = sorted(status for status in counts if status >= 400)
    findings: list[dict] = []
    gaps: list[str] = []
    evidence = ["rgw_access_log"] if records else []
    endpoint_status = ((rgw_evidence or {}).get("endpoints") or {}).get("status")

    if admin_query_status == "error":
        findings.append(_finding(
            "CEPHX_ADMIN_OR_RGW_ADMIN_FAILURE",
            "warning",
            "Không đọc được metadata bằng radosgw-admin; lỗi CephX/admin hoặc RGW admin path cần được kiểm tra riêng.",
            ["radosgw-admin query failed"],
            "Kiểm tra quyền CephX của tài khoản admin và trạng thái radosgw-admin trên RGW node.",
        ))
        evidence.append("radosgw_admin")

    if counts[401]:
        findings.append(_finding(
            "S3_AUTHENTICATION_FAILURE",
            "warning",
            "RGW đã nhận request nhưng từ chối xác thực S3 credential.",
            [f"HTTP 401 x{counts[401]}"],
            "Kiểm tra access key, secret key, clock skew và tenant/user mapping; không gửi secret key vào log.",
        ))
    if counts[403]:
        if selected_operation in MUTATING_OPERATIONS and _quota_full(bucket_stats):
            findings.append(_finding(
                "S3_QUOTA_REACHED",
                "warning",
                "Request ghi/xóa bị từ chối trong khi bucket đã chạm quota đã quan sát.",
                [f"HTTP 403 x{counts[403]}", "bucket quota is full"],
                "Đối chiếu quota với owner và capacity trước khi thay đổi quota.",
            ))
        else:
            findings.append(_finding(
                "S3_POLICY_OR_PERMISSION_DENIED",
                "warning",
                "RGW đã xác thực request nhưng policy/ACL hoặc quyền user không cho phép thao tác.",
                [f"HTTP 403 x{counts[403]}"],
                "Đọc bucket policy, ACL, user caps và tenant scope; không tự nới quyền.",
            ))
    if counts[429]:
        findings.append(_finding(
            "RGW_RATE_LIMIT_OR_QUOTA",
            "warning",
            "RGW trả HTTP 429; có thể đang rate-limit hoặc áp quota request.",
            [f"HTTP 429 x{counts[429]}"],
            "Đối chiếu RGW rate limit/quota config và request burst theo timestamp.",
        ))
    if any(status in counts for status in (500, 502, 503, 504)):
        backend_statuses = [status for status in (500, 502, 503, 504) if counts[status]]
        findings.append(_finding(
            "RGW_BACKEND_OR_SERVICE_FAILURE",
            "critical" if 503 in counts else "warning",
            "RGW trả lỗi server/gateway; cần kiểm tra daemon, frontend và backend pool.",
            [f"HTTP {status} x{counts[status]}" for status in backend_statuses],
            "Đối chiếu RGW daemon health, log lỗi, placement pool và Ceph health detail.",
        ))
    if counts[404]:
        findings.append(_finding(
            "BUCKET_OR_OBJECT_NOT_FOUND",
            "info",
            "Request trỏ tới bucket/object không tồn tại hoặc không được expose trong tenant hiện tại.",
            [f"HTTP 404 x{counts[404]}"],
            "Xác nhận bucket name, tenant và object path; không kết luận bucket đã bị xóa nếu thiếu inventory.",
        ))

    if not records:
        gaps.append("Không có request access log phù hợp trong cửa sổ hiện tại; không thể kết luận lỗi HTTP.")
    if endpoint_status in {None, "not_available"}:
        gaps.append("Chưa có endpoint RGW đã xác nhận; DNS/TLS/connectivity không được suy luận từ access log rỗng.")
    if endpoint_status == "inferred":
        gaps.append("Endpoint chỉ được suy ra từ node/port mặc định, chưa xác nhận DNS/TLS bằng probe read-only.")
    if not bucket_stats and bucket:
        gaps.append("Chưa có bucket stats; chưa đủ evidence để phân biệt quota với policy trong các ca 403.")
    if not rgw_evidence:
        gaps.append("Chưa có RGW topology evidence để đối chiếu daemon/frontend/backend pool.")
    if records:
        evidence.append("bucket_stats" if bucket_stats else "bucket_stats_unavailable")
        evidence.append("rgw_evidence" if rgw_evidence else "rgw_evidence_unavailable")

    if not findings:
        summary = "Chưa thấy mẫu lỗi HTTP đủ mạnh trong evidence hiện có."
        status = "insufficient_evidence" if gaps else "no_anomaly"
    else:
        summary = "; ".join(finding["summary"] for finding in findings)
        status = "diagnosed"
    return {
        "status": status,
        "bucket": bucket or None,
        "operation": selected_operation,
        "summary": summary,
        "findings": findings,
        "observed": {
            "request_count": len(records),
            "error_count": sum(count for status, count in counts.items() if status >= 400),
            "status_counts": {str(status): count for status, count in sorted(counts.items())},
            "error_statuses": errors,
        },
        "evidence": evidence,
        "evidence_gaps": gaps,
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
    }
