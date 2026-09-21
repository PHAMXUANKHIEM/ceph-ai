"""Deterministic RGW alert rules and Incident lifecycle.

This module is intentionally separate from the Dashboard routes.  It consumes
bounded, already-collected RGW evidence and never executes a Ceph/S3 command.
The rules are advisory only: they create a scoped Incident and a Telegram
notification, but they do not create an Action or mutate RGW state.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from config.settings import settings
from shared.cluster_nodes import configured_nodes, resolve_ssh_creds
from shared import alert_lifecycle, db, telegram_alerts
from shared.models import (
    Cluster,
    Incident,
    IncidentStatus,
    RgwAccessAuditEvent,
    RgwMetricSnapshot,
)
from shared.time import utc_now
from watcher.rgw_audit_intelligence import build_rgw_audit_intelligence
from watcher.rgw_access_log import (
    fetch_bucket_list,
    fetch_bucket_list_with,
    fetch_bucket_stats,
    fetch_bucket_stats_with,
    summarize_bucket_stats,
)

logger = logging.getLogger(__name__)

# Keep the window and thresholds explicit and reviewable.  The values are
# intentionally conservative for a small Ceph lab; operators can tune them in
# code/config later without changing the Incident contract.
RGW_ALERT_WINDOW_SECONDS = 5 * 60
RGW_ALERT_SCAN_INTERVAL_SECONDS = max(
    60, int(getattr(settings, "rgw_metric_snapshot_interval_seconds", 5 * 60))
)
QUOTA_WARNING_RATIO = 0.80
QUOTA_HIGH_RATIO = 0.90
QUOTA_CRITICAL_RATIO = 0.95
MIN_5XX_COUNT = 10
MIN_5XX_RATE_PERCENT = 5.0
MIN_ACCESS_DENIED_COUNT = 5
MIN_ACCESS_DENIED_RATE_PERCENT = 10.0
MIN_HOT_BUCKET_REQUESTS = 50
HOT_BUCKET_SHARE = 0.50
MAX_EVIDENCE_ITEMS = 20
MAX_BUCKET_STATS = 100

RGW_ALERT_PREFIX = "RGW_ALERT_"
_OPEN_STATUSES = (
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    IncidentStatus.GRACE_PENDING.value,
    IncidentStatus.VERIFYING.value,
)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _mapping(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _status_counts(snapshot: dict) -> dict[int, int]:
    metrics = _mapping(snapshot.get("metrics"))
    raw = metrics.get("status_counts", snapshot.get("status_counts"))
    result: dict[int, int] = {}
    for key, value in _mapping(raw).items():
        try:
            status = int(key)
            count = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= status <= 599 and count >= 0:
            result[status] = count
    return result


def _request_count(snapshot: dict, counts: dict[int, int]) -> int:
    metrics = _mapping(snapshot.get("metrics"))
    value = _number(metrics.get("request_count", snapshot.get("request_count")))
    if value is not None and value >= 0:
        return int(value)
    return sum(counts.values())


def _bucket_counts(snapshot: dict) -> dict[str, int]:
    metrics = _mapping(snapshot.get("metrics"))
    raw = metrics.get("top_buckets", snapshot.get("top_buckets"))
    result = {}
    for key, value in _mapping(raw).items():
        count = _number(value)
        name = str(key).strip()
        if name and count is not None and count >= 0:
            result[name[:255]] = int(count)
    return result


def _ratio_alert(
    *,
    code: str,
    severity: str,
    title: str,
    summary: str,
    dedupe_key: str,
    evidence: Iterable[str],
    observed: dict,
) -> dict:
    return {
        "code": code,
        "severity": severity,
        "title": title,
        "summary": summary,
        "dedupe_key": dedupe_key[:256],
        "evidence": [str(item)[:240] for item in list(evidence)[:MAX_EVIDENCE_ITEMS]],
        "observed": observed,
        "read_only": True,
        "action_id": None,
    }


def _quota_alerts(snapshot: dict) -> list[dict]:
    alerts = []
    for raw in (snapshot.get("bucket_stats") or [])[:MAX_BUCKET_STATS]:
        row = _mapping(raw)
        bucket = str(row.get("bucket") or row.get("name") or "").strip()
        if not bucket:
            continue
        ratios: list[tuple[str, float]] = []
        for used_key, limit_key, label in (
            ("size_bytes", "quota_max_size_bytes", "size"),
            ("num_objects", "quota_max_objects", "objects"),
        ):
            used = _number(row.get(used_key))
            limit = _number(row.get(limit_key))
            if used is None or limit is None or limit <= 0 or used < 0:
                continue
            ratios.append((label, used / limit))
        if not ratios or not row.get("quota_enabled"):
            continue
        label, ratio = max(ratios, key=lambda item: item[1])
        if ratio < QUOTA_WARNING_RATIO:
            continue
        if ratio >= QUOTA_CRITICAL_RATIO:
            severity, level = "HEALTH_ERR", "95%"
        elif ratio >= QUOTA_HIGH_RATIO:
            severity, level = "HEALTH_WARN", "90%"
        else:
            severity, level = "HEALTH_WARN", "80%"
        used_key = "size_bytes" if label == "size" else "num_objects"
        limit_key = "quota_max_size_bytes" if label == "size" else "quota_max_objects"
        alerts.append(_ratio_alert(
            code="RGW_ALERT_BUCKET_QUOTA",
            severity=severity,
            title=f"RGW bucket quota {level}: {bucket}",
            summary=(f"Bucket {bucket} đang dùng {ratio * 100:.1f}% quota theo {label}; "
                     "cần theo dõi trước khi RGW chặn ghi."),
            dedupe_key=f"bucket:{bucket}",
            evidence=[
                f"bucket={bucket}", f"dimension={label}",
                f"used={row.get(used_key)}", f"limit={row.get(limit_key)}",
                f"ratio={ratio:.4f}",
            ],
            observed={"bucket": bucket, "ratio": round(ratio, 6), "dimension": label},
        ))
    return alerts


def evaluate_rgw_alerts(snapshot: dict | None) -> dict:
    """Evaluate all RGW rules from a bounded, secret-free snapshot.

    Missing evidence never becomes an alert.  It is returned as an explicit
    gap so the caller can show why a rule did not run.
    """
    snapshot = _mapping(snapshot)
    metrics = _mapping(snapshot.get("metrics"))
    counts = _status_counts(snapshot)
    request_count = _request_count(snapshot, counts)
    errors_5xx = sum(value for status, value in counts.items() if 500 <= status <= 599)
    denied = sum(counts.get(status, 0) for status in (401, 403))
    error_rate = errors_5xx * 100 / request_count if request_count else 0.0
    denied_rate = denied * 100 / request_count if request_count else 0.0
    alerts = _quota_alerts(snapshot)
    gaps = [str(item)[:240] for item in (snapshot.get("evidence_gaps") or [])]

    if errors_5xx >= MIN_5XX_COUNT and error_rate >= MIN_5XX_RATE_PERCENT:
        severity = "HEALTH_ERR" if errors_5xx >= 2 * MIN_5XX_COUNT and error_rate >= 10 else "HEALTH_WARN"
        alerts.append(_ratio_alert(
            code="RGW_ALERT_5XX_SPIKE", severity=severity,
            title="RGW 5xx spike", summary="RGW đang trả về nhiều lỗi 5xx trong cửa sổ quan sát.",
            dedupe_key="window:5xx", evidence=[
                f"5xx={errors_5xx}", f"requests={request_count}", f"rate={error_rate:.2f}%",
            ], observed={"count": errors_5xx, "rate_percent": round(error_rate, 4)},
        ))

    if denied >= MIN_ACCESS_DENIED_COUNT and denied_rate >= MIN_ACCESS_DENIED_RATE_PERCENT:
        alerts.append(_ratio_alert(
            code="RGW_ALERT_ACCESS_DENIED_SPIKE", severity="HEALTH_WARN",
            title="RGW access denied spike",
            summary="RGW đang có nhiều request bị từ chối xác thực hoặc phân quyền.",
            dedupe_key="window:access-denied", evidence=[
                f"401+403={denied}", f"requests={request_count}", f"rate={denied_rate:.2f}%",
            ], observed={"count": denied, "rate_percent": round(denied_rate, 4)},
        ))

    bucket_counts = _bucket_counts(snapshot)
    if request_count >= MIN_HOT_BUCKET_REQUESTS and bucket_counts:
        bucket, count = max(bucket_counts.items(), key=lambda item: item[1])
        share = count / request_count if request_count else 0.0
        if share >= HOT_BUCKET_SHARE:
            alerts.append(_ratio_alert(
                code="RGW_ALERT_HOT_BUCKET", severity="HEALTH_WARN",
                title=f"RGW hot bucket: {bucket}",
                summary="Một bucket chiếm tỷ trọng request bất thường trong cửa sổ quan sát.",
                dedupe_key=f"bucket:{bucket}", evidence=[
                    f"bucket={bucket}", f"requests={count}", f"total={request_count}",
                    f"share={share:.2%}",
                ], observed={"bucket": bucket, "count": count, "share": round(share, 6)},
            ))

    allowed_findings = {
        "ANONYMOUS_SUCCESSFUL_WRITE": "HEALTH_ERR",
        "AUTHORIZATION_FAILURE_BURST": "HEALTH_WARN",
        "REQUEST_BURST_OUTLIER": "HEALTH_WARN",
    }
    for finding in (snapshot.get("audit_findings") or [])[:MAX_EVIDENCE_ITEMS]:
        row = _mapping(finding)
        code = str(row.get("code") or "")
        severity = allowed_findings.get(code)
        if severity is None:
            continue
        resource = str(row.get("resource") or row.get("bucket") or "window").strip()[:180]
        alerts.append(_ratio_alert(
            code=f"RGW_ALERT_{code}", severity=severity,
            title=f"RGW abnormal access: {code}",
            summary=str(row.get("summary") or "RGW audit intelligence phát hiện access bất thường.")[:400],
            dedupe_key=f"{code}:{resource}",
            evidence=row.get("evidence") if isinstance(row.get("evidence"), list) else [code],
            observed={"finding_code": code, "resource": resource},
        ))

    if not counts and not snapshot.get("bucket_stats") and not snapshot.get("audit_findings"):
        gaps.append("Chưa có RGW evidence đủ mới để chạy alert rules.")
    return {
        "status": "alerting" if alerts else "no_alert",
        "alerts": alerts,
        "evidence_gaps": sorted(set(gaps))[:MAX_EVIDENCE_ITEMS],
        "window_seconds": RGW_ALERT_WINDOW_SECONDS,
        "thresholds": {
            "quota_warning": QUOTA_WARNING_RATIO,
            "quota_high": QUOTA_HIGH_RATIO,
            "quota_critical": QUOTA_CRITICAL_RATIO,
            "5xx_min_count": MIN_5XX_COUNT,
            "5xx_min_rate_percent": MIN_5XX_RATE_PERCENT,
            "access_denied_min_count": MIN_ACCESS_DENIED_COUNT,
            "access_denied_min_rate_percent": MIN_ACCESS_DENIED_RATE_PERCENT,
            "hot_bucket_min_requests": MIN_HOT_BUCKET_REQUESTS,
            "hot_bucket_share": HOT_BUCKET_SHARE,
        },
        "read_only": True,
        "action_id": None,
        "metrics": {
            "request_count": request_count,
            "5xx_count": errors_5xx,
            "5xx_rate_percent": round(error_rate, 4),
            "access_denied_count": denied,
            "access_denied_rate_percent": round(denied_rate, 4),
            "top_buckets": bucket_counts,
            "source": metrics.get("source") or snapshot.get("source") or "rgw_evidence",
        },
    }


def _excerpt(alert: dict) -> str:
    evidence = "; ".join(str(item)[:180] for item in alert.get("evidence", [])[:8])
    return f"{alert.get('title')}: {alert.get('summary')} Evidence: {evidence}"[:700]


def _evidence_json(alert: dict, snapshot: dict) -> str:
    observed = _mapping(alert.get("observed"))
    bucket = str(observed.get("bucket") or "").strip()[:255]
    payload = {
        "rule": alert.get("code"),
        "severity": alert.get("severity"),
        "dedupe_key": alert.get("dedupe_key"),
        "observed": observed,
        "evidence": alert.get("evidence", []),
        "window_seconds": snapshot.get("window_seconds", RGW_ALERT_WINDOW_SECONDS),
        "target": {"type": "bucket", "id": bucket} if bucket else None,
        "read_only": True,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)[:12000]


def _threshold_band(alert: dict) -> str:
    """Return the notification band, including 80/90/95 quota transitions."""
    observed = _mapping(alert.get("observed"))
    if alert.get("code") == "RGW_ALERT_BUCKET_QUOTA":
        ratio = _number(observed.get("ratio")) or 0.0
        if ratio >= QUOTA_CRITICAL_RATIO:
            return "quota-critical-95"
        if ratio >= QUOTA_HIGH_RATIO:
            return "quota-high-90"
        return "quota-warning-80"
    return str(alert.get("severity") or "HEALTH_WARN")


def sync_rgw_alerts(
    session,
    cluster: Cluster,
    snapshot: dict,
    *,
    now: datetime | None = None,
    send_notifications: bool = True,
) -> dict:
    """Create/update/resolve RGW alert Incidents for one cluster only."""
    now = now or utc_now()
    evaluation = evaluate_rgw_alerts(snapshot)
    current_keys = {(alert["code"], alert["dedupe_key"]) for alert in evaluation["alerts"]}
    query = session.query(Incident).filter(
        Incident.cluster_id == cluster.id,
        Incident.ceph_code.like(f"{RGW_ALERT_PREFIX}%"),
        Incident.status.in_(_OPEN_STATUSES),
    )
    active = {(row.ceph_code, row.dedupe_key): row for row in query.all()}
    resolved = 0
    for key, incident in active.items():
        if key not in current_keys:
            incident.status = IncidentStatus.RESOLVED.value
            incident.updated_at = now
            resolved += 1

    pending_notifications: list[tuple[dict, bool]] = []
    created = 0
    updated = 0
    for alert in evaluation["alerts"]:
        key = (alert["code"], alert["dedupe_key"])
        incident = active.get(key)
        if incident is None:
            incident = Incident(
                cluster_id=cluster.id,
                ceph_code=alert["code"],
                dedupe_key=alert["dedupe_key"],
                status=IncidentStatus.NEW.value,
                severity=alert["severity"],
                detected_at=now,
                log_excerpt=_excerpt(alert),
                signal_evidence_json=_evidence_json(alert, evaluation),
            )
            session.add(incident)
            session.flush()
            muted = alert_lifecycle.inherit_active_mute(session, incident, now=now)
            pending_notifications.append((alert, muted))
            created += 1
        else:
            previous_band = ""
            try:
                previous = json.loads(incident.signal_evidence_json or "{}")
                previous_observed = _mapping(previous.get("observed"))
                if alert["code"] == "RGW_ALERT_BUCKET_QUOTA":
                    previous_ratio = _number(previous_observed.get("ratio")) or 0.0
                    if previous_ratio >= QUOTA_CRITICAL_RATIO:
                        previous_band = "quota-critical-95"
                    elif previous_ratio >= QUOTA_HIGH_RATIO:
                        previous_band = "quota-high-90"
                    else:
                        previous_band = "quota-warning-80"
                else:
                    previous_band = str(previous.get("severity") or "")
            except (TypeError, ValueError):
                previous_band = ""
            incident.severity = alert["severity"]
            incident.log_excerpt = _excerpt(alert)
            incident.signal_evidence_json = _evidence_json(alert, evaluation)
            incident.updated_at = now
            if previous_band and previous_band != _threshold_band(alert):
                pending_notifications.append((
                    alert,
                    alert_lifecycle.is_active_mute(incident, now=now),
                ))
            updated += 1
    session.commit()

    delivered = 0
    if send_notifications:
        has_cluster_channel = bool(cluster.telegram_bot_token and cluster.telegram_chat_id)
        for alert, muted in pending_notifications:
            if muted:
                continue
            telegram_alerts.send_incident_alert(
                alert["code"], alert["severity"], _excerpt(alert),
                cluster_name=cluster.name,
                bot_token=cluster.telegram_bot_token if has_cluster_channel else None,
                chat_id=cluster.telegram_chat_id if has_cluster_channel else None,
                enabled=cluster.telegram_enabled if has_cluster_channel else None,
                rationale=alert["summary"],
                background=settings.telegram_ai_humanize_enabled,
            )
            delivered += 1
            with db.SessionLocal() as delivery_session:
                incident = delivery_session.query(Incident).filter(
                    Incident.cluster_id == cluster.id,
                    Incident.ceph_code == alert["code"],
                    Incident.dedupe_key == alert["dedupe_key"],
                    Incident.status.in_(_OPEN_STATUSES),
                ).first()
                if incident is not None:
                    incident.telegram_reminded_at = utc_now()
                    delivery_session.commit()
    return {
        "cluster_id": cluster.id,
        "created": created,
        "updated": updated,
        "resolved": resolved,
        "delivered": delivered,
        "evaluation": evaluation,
    }


def snapshot_from_audit_rows(rows: Iterable[RgwAccessAuditEvent | dict], *, now: datetime | None = None) -> dict:
    """Build a rule snapshot from durable audit rows without exposing secrets."""
    now = now or utc_now()
    status_counts: dict[str, int] = {}
    bucket_counts: dict[str, int] = {}
    normalized = []
    intelligence_rows = []
    requester_counts: dict[str, int] = {}
    bytes_total = 0
    latencies: list[float] = []
    for row in rows:
        if isinstance(row, dict):
            status = row.get("status", row.get("http_status"))
            bucket = row.get("bucket")
            method = row.get("method")
            requester = row.get("requester")
            remote_addr = row.get("remote_addr")
            latency_ms = row.get("latency_ms")
            encryption = row.get("encryption")
            timestamp = row.get("timestamp") or row.get("event_at")
            requester = row.get("requester")
            bytes_sent = row.get("bytes_sent")
        else:
            status = row.http_status
            bucket = row.bucket
            method = row.method
            requester = row.requester
            remote_addr = row.remote_addr
            latency_ms = row.latency_ms
            encryption = row.encryption
            timestamp = row.event_at
            requester = row.requester
            bytes_sent = row.bytes_sent
        try:
            status_int = int(status)
        except (TypeError, ValueError):
            continue
        status_key = str(status_int)
        status_counts[status_key] = status_counts.get(status_key, 0) + 1
        if bucket:
            bucket_name = str(bucket)[:255]
            bucket_counts[bucket_name] = bucket_counts.get(bucket_name, 0) + 1
        requester_name = str(requester or "-")[:255]
        requester_counts[requester_name] = requester_counts.get(requester_name, 0) + 1
        byte_count = _number(bytes_sent)
        if byte_count is not None and byte_count >= 0:
            bytes_total += int(byte_count)
        latency = _number(latency_ms)
        if latency is not None and latency >= 0:
            latencies.append(latency)
        normalized.append((status_int, bucket))
        intelligence_rows.append({
            "method": str(method or "GET")[:16],
            "status": status_int,
            "requester": str(requester or "-")[:255],
            "remote_addr": str(remote_addr or "-")[:255],
            "latency_ms": latency_ms,
            "encryption": str(encryption or "unknown")[:64],
            "bucket": str(bucket or "")[:255] or None,
            "timestamp": str(timestamp or ""),
        })
    intelligence = build_rgw_audit_intelligence(intelligence_rows)
    error_count = sum(count for status, count in status_counts.items() if int(status) >= 400)
    ordered_latencies = sorted(latencies)
    p95_index = min(len(ordered_latencies) - 1, int(len(ordered_latencies) * 0.95)) if ordered_latencies else 0
    request_count = len(normalized)
    return {
        "captured_at": now.isoformat(timespec="seconds"),
        "metrics": {
            "request_count": request_count,
            "bytes_total": bytes_total,
            "error_count": error_count,
            "error_rate_percent": round(error_count * 100 / max(1, request_count), 4),
            "latency_p95_ms": round(ordered_latencies[p95_index], 2) if ordered_latencies else None,
            "status_counts": status_counts,
            "top_buckets": dict(sorted(bucket_counts.items(), key=lambda item: (-item[1], item[0]))[:100]),
            "top_requesters": dict(sorted(requester_counts.items(), key=lambda item: (-item[1], item[0]))[:100]),
            "source": "rgw_access_audit_events",
        },
        "audit_findings": intelligence.get("findings", []),
        "evidence_gaps": intelligence.get("evidence_gaps", []),
        "read_only": True,
        "action_id": None,
    }


def persist_rgw_metric_snapshot(
    cluster_id: str,
    snapshot: dict,
    *,
    now: datetime | None = None,
) -> RgwMetricSnapshot:
    """Persist one bounded aggregate snapshot and prune its cluster history."""
    now = now or utc_now()
    metrics = _mapping(snapshot.get("metrics"))
    gaps = [str(item)[:240] for item in (snapshot.get("evidence_gaps") or [])[:MAX_EVIDENCE_ITEMS]]
    row = RgwMetricSnapshot(
        cluster_id=cluster_id,
        captured_at=now,
        available=bool(metrics.get("request_count") or snapshot.get("bucket_stats")),
        request_count=int(_number(metrics.get("request_count")) or 0),
        bytes_total=int(_number(metrics.get("bytes_total")) or 0),
        error_count=int(_number(metrics.get("error_count")) or 0),
        error_rate_percent=float(_number(metrics.get("error_rate_percent")) or 0),
        latency_p95_ms=_number(metrics.get("latency_p95_ms")),
        top_buckets_json=json.dumps(_mapping(metrics.get("top_buckets")), ensure_ascii=False, sort_keys=True),
        top_requesters_json=json.dumps(_mapping(metrics.get("top_requesters")), ensure_ascii=False, sort_keys=True),
        evidence_gaps_json=json.dumps(gaps, ensure_ascii=False),
        source=str(metrics.get("source") or "rgw_access_audit_events")[:64],
    )
    cutoff = now - timedelta(days=max(1, int(getattr(settings, "rgw_metric_snapshot_retention_days", 30))))
    with db.SessionLocal() as session:
        session.add(row)
        session.query(RgwMetricSnapshot).filter(
            RgwMetricSnapshot.cluster_id == cluster_id,
            RgwMetricSnapshot.captured_at < cutoff,
        ).delete(synchronize_session=False)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row


def collect_bucket_quota_stats(cluster: Cluster, *, max_buckets: int = MAX_BUCKET_STATS) -> tuple[list[dict], list[str]]:
    """Collect bounded bucket usage/quota stats for one cluster.

    The collector reads only RGW metadata.  It uses the selected cluster's
    RGW host and SSH/container settings, caps fan-out, and returns gaps instead
    of turning a partial RGW outage into a false quota alert.
    """
    limit = max(0, min(int(max_buckets), MAX_BUCKET_STATS))
    hosts = [
        str(node["host"])
        for node in configured_nodes(cluster)
        if "RGW" in node.get("roles", [])
    ]
    if not hosts:
        return [], ["Chưa cấu hình node RGW để đọc bucket stats cho quota alert."]
    host = hosts[0]
    gaps: list[str] = []
    try:
        if cluster.is_default:
            bucket_names = fetch_bucket_list(host)
        else:
            ssh_user, ssh_key_path, exec_mode, rgw_container_name = resolve_ssh_creds(cluster)
            bucket_names = fetch_bucket_list_with(
                host, ssh_user, ssh_key_path, exec_mode, rgw_container_name,
            )
    except Exception as exc:
        return [], [f"Không đọc được bucket list cho quota alert: {type(exc).__name__}"]

    names = [str(name)[:255] for name in bucket_names if str(name).strip()][:limit]
    if len(bucket_names) > len(names):
        gaps.append(f"Chỉ quét {len(names)}/{len(bucket_names)} bucket đầu tiên theo giới hạn fan-out.")

    def read(bucket: str) -> tuple[dict | None, str | None]:
        try:
            if cluster.is_default:
                raw = fetch_bucket_stats(host, bucket)
            else:
                ssh_user, ssh_key_path, exec_mode, rgw_container_name = resolve_ssh_creds(cluster)
                raw = fetch_bucket_stats_with(
                    host, bucket, ssh_user, ssh_key_path, exec_mode, rgw_container_name,
                )
            if not isinstance(raw, dict):
                return None, f"bucket={bucket}:empty_stats"
            return {"bucket": bucket, **summarize_bucket_stats(raw)}, None
        except Exception as exc:
            return None, f"bucket={bucket}:{type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=min(4, max(1, len(names)))) as executor:
        results = list(executor.map(read, names))
    stats = [row for row, error in results if row is not None]
    gaps.extend(error for _row, error in results if error)
    if not stats and names:
        gaps.append("Không đọc được bucket stats nào; quota alert đang fail-closed.")
    return stats, sorted(set(gaps))[:MAX_EVIDENCE_ITEMS]


def scan_and_alert(cluster_id: str | None = None, *, now: datetime | None = None) -> dict | None:
    """Evaluate recent durable RGW audit evidence for one cluster."""
    now = now or utc_now()
    cutoff = now - timedelta(seconds=RGW_ALERT_WINDOW_SECONDS)
    with db.SessionLocal() as session:
        query = session.query(Cluster).filter(Cluster.is_active.is_(True))
        cluster = session.get(Cluster, cluster_id) if cluster_id else query.filter(Cluster.is_default.is_(True)).first()
        if cluster is None or not cluster.is_active:
            return None
        session.expunge(cluster)
        rows = session.query(RgwAccessAuditEvent).filter(
            RgwAccessAuditEvent.cluster_id == cluster.id,
            RgwAccessAuditEvent.event_at >= cutoff,
        ).order_by(RgwAccessAuditEvent.event_at.desc()).limit(5000).all()
        snapshot = snapshot_from_audit_rows(rows, now=now)

    quota_stats, quota_gaps = collect_bucket_quota_stats(cluster)
    snapshot["bucket_stats"] = quota_stats
    snapshot["evidence_gaps"].extend(quota_gaps)
    persist_rgw_metric_snapshot(cluster.id, snapshot, now=now)
    with db.SessionLocal() as session:
        return sync_rgw_alerts(session, cluster, snapshot, now=now)
