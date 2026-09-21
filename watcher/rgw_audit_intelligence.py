"""Deterministic, read-only intelligence for RGW audit-log evidence.

The result is deliberately a bounded set of signals, not an access-control
decision.  It never returns credentials, raw request paths, or an action id.
When there is no historical baseline, peer groups in the current window are
used only as a weak comparison and the evidence gap is kept explicit.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median


MAX_RECORDS = 5000
MIN_AUTH_FAILURES = 5
MIN_LATENCY_SAMPLES = 5
WRITE_METHODS = {"PUT", "POST", "DELETE"}
ANONYMOUS_NAMES = {"", "-", "anonymous", "unknown"}


def _number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clean_records(records: list[dict] | None) -> list[dict]:
    result = []
    for record in (records or [])[:MAX_RECORDS]:
        if not isinstance(record, dict):
            continue
        method = str(record.get("method") or "").upper()
        try:
            status = int(record.get("status"))
        except (TypeError, ValueError):
            continue
        result.append({
            "method": method,
            "status": status,
            "requester": str(record.get("requester") or "").strip() or "-",
            "remote_addr": str(record.get("remote_addr") or "").strip() or "-",
            "user_agent": str(record.get("user_agent") or "").strip() or "-",
            "bucket": str(record.get("bucket") or "").strip() or None,
            "latency_ms": _number(record.get("latency_ms"), -1.0),
            "bytes_sent": max(0.0, _number(record.get("bytes_sent"), 0.0)),
            "encryption": str(record.get("encryption") or "").strip() or "unknown",
            "timestamp": str(record.get("timestamp") or record.get("timestamp_raw") or "").strip(),
        })
    return result


def _finding(code: str, severity: str, summary: str, evidence: list[str], confidence: float) -> dict:
    return {
        "code": code,
        "severity": severity,
        "summary": summary,
        "evidence": evidence[:8],
        "confidence": round(max(0.0, min(1.0, confidence)), 2),
        "read_only": True,
        "action_id": None,
    }


def _group(records: list[dict], key: str) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        groups[str(record.get(key) or "-")].append(record)
    return groups


def _mad(values: list[float], center: float) -> float:
    return median([abs(value - center) for value in values]) if values else 0.0


def _bounded_counts(rows: list[dict], key: str, limit: int = 20) -> dict[str, int]:
    counts = Counter(str(row.get(key) or "-") for row in rows)
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit])


def build_rgw_audit_intelligence(
    records: list[dict] | None,
    *,
    source_hosts: list[str] | None = None,
    historical_records: list[dict] | None = None,
) -> dict:
    """Analyze bounded RGW audit rows without making an enforcement decision."""
    rows = _clean_records(records)
    historical = _clean_records(historical_records)
    findings: list[dict] = []
    gaps: list[str] = []

    if historical:
        baseline_rows = historical
        baseline_method = "historical_rows"
    else:
        baseline_rows = rows
        baseline_method = "current_window_peer_groups"
        gaps.append("Chưa có lịch sử audit-log bền vững; baseline hiện chỉ so sánh peer trong cửa sổ này.")
    if not rows:
        gaps.append("Không có audit-log RGW hợp lệ trong cửa sổ hiện tại.")

    anonymous_writes = [
        row for row in rows
        if row["requester"].casefold() in ANONYMOUS_NAMES
        and row["method"] in WRITE_METHODS
        and 200 <= row["status"] < 400
    ]
    if anonymous_writes:
        ips = sorted({row["remote_addr"] for row in anonymous_writes})
        findings.append(_finding(
            "ANONYMOUS_SUCCESSFUL_WRITE",
            "critical",
            "Audit-log ghi nhận request ghi/xóa thành công từ requester anonymous; cần xác minh bucket public và policy.",
            [f"{len(anonymous_writes)} request thành công", f"IP: {', '.join(ips[:4])}"],
            0.96,
        ))

    failed_by_ip = _group(
        [row for row in rows if row["status"] in {401, 403}], "remote_addr"
    )
    for ip, group in sorted(failed_by_ip.items()):
        if len(group) < MIN_AUTH_FAILURES:
            continue
        requester_count = len({row["requester"] for row in group})
        findings.append(_finding(
            "AUTHORIZATION_FAILURE_BURST",
            "warning",
            "Một nguồn IP có nhiều request 401/403 trong cùng cửa sổ; có thể là credential/policy lỗi hoặc probing.",
            [f"IP {ip}", f"{len(group)} request bị từ chối", f"{requester_count} requester"],
            0.72 if requester_count > 1 else 0.62,
        ))

    ip_counts = Counter(row["remote_addr"] for row in rows if row["remote_addr"] != "-")
    peer_counts = list(ip_counts.values())
    if len(peer_counts) >= 2:
        center = median(peer_counts)
        threshold = max(20.0, center + 6.0 * max(1.0, _mad([float(v) for v in peer_counts], center)))
        for ip, count in sorted(ip_counts.items()):
            if count <= threshold:
                continue
            findings.append(_finding(
                "REQUEST_BURST_OUTLIER",
                "warning",
                "Một IP có request volume lệch mạnh so với peer trong audit window; chưa kết luận là tấn công.",
                [f"IP {ip}", f"{count} request", f"peer median {center:.0f}", f"threshold {threshold:.0f}"],
                0.58,
            ))

    latencies = [row["latency_ms"] for row in rows if row["latency_ms"] >= 0]
    if len(latencies) >= MIN_LATENCY_SAMPLES:
        center = median(latencies)
        deviation = _mad(latencies, center)
        threshold = max(1000.0, center + 6.0 * max(1.0, deviation))
        outliers = [row for row in rows if row["latency_ms"] >= threshold]
        if outliers:
            findings.append(_finding(
                "AUDIT_LATENCY_OUTLIER",
                "info",
                "Một số request RGW có latency cao hơn baseline robust của cửa sổ; cần đối chiếu backend và network.",
                [f"{len(outliers)} request", f"median {center:.1f} ms", f"threshold {threshold:.1f} ms"],
                0.55,
            ))

    plaintext_writes = [
        row for row in rows
        if row["method"] in {"PUT", "POST"}
        and 200 <= row["status"] < 400
        and row["encryption"].casefold() == "plaintext"
    ]
    if plaintext_writes:
        findings.append(_finding(
            "SUCCESSFUL_WRITE_WITHOUT_ENCRYPTION_SIGNAL",
            "info",
            "Audit-log không thấy header mã hóa phía client ở một số request ghi; đây là tín hiệu cần kiểm tra, không phải bằng chứng dữ liệu plaintext.",
            [f"{len(plaintext_writes)} request ghi thành công", "encryption header absent"],
            0.48,
        ))

    valid_latencies = [row["latency_ms"] for row in rows if row["latency_ms"] >= 0]
    ordered_latencies = sorted(valid_latencies)
    p95_index = min(len(ordered_latencies) - 1, int(len(ordered_latencies) * 0.95)) if ordered_latencies else 0
    timestamps = sorted(row["timestamp"] for row in rows if row["timestamp"])
    status = "analyzed" if rows else "insufficient_evidence"
    return {
        "status": status,
        "summary": (
            "Đã phân tích audit-log bằng baseline tất định và giữ lại các gap cần operator xác minh."
            if rows else "Chưa đủ audit-log để phân tích."
        ),
        "window": {
            "record_count": len(rows),
            "source_hosts": sorted({str(host) for host in (source_hosts or []) if str(host).strip()}),
            "max_records": MAX_RECORDS,
            "observed": {
                "requester_counts": _bounded_counts(rows, "requester"),
                "remote_addr_counts": _bounded_counts(rows, "remote_addr"),
                "user_agent_counts": _bounded_counts(rows, "user_agent"),
                "bucket_counts": _bounded_counts(rows, "bucket"),
                "method_counts": _bounded_counts(rows, "method"),
                "status_counts": _bounded_counts(rows, "status"),
                "encryption_counts": _bounded_counts(rows, "encryption"),
                "bytes_total": int(sum(row["bytes_sent"] for row in rows)),
                "latency_median_ms": round(median(valid_latencies), 2) if valid_latencies else None,
                "latency_p95_ms": round(ordered_latencies[p95_index], 2) if ordered_latencies else None,
                "time_start": timestamps[0] if timestamps else None,
                "time_end": timestamps[-1] if timestamps else None,
            },
        },
        "baseline": {
            "method": baseline_method,
            "sample_count": len(baseline_rows),
            "historical_available": bool(historical),
            "retention": "process-window-only",
        },
        "findings": findings,
        "evidence_gaps": gaps,
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
    }
