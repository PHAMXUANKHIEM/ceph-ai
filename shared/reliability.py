"""Operational reliability contract for the Ceph-AI control plane.

This module is intentionally read-only.  It combines process heartbeats,
snapshot freshness, durable queue state, SQLAlchemy pool pressure, resource
usage and existing in-process telemetry into one bounded diagnostics payload.
It must not trigger a Ceph command, restart, retry or remediation action.
"""

from __future__ import annotations

import os
import time
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func

from config.settings import settings
from shared import db, incident_outbox, telegram_outbox
from shared.api_observability import get_metrics as get_api_metrics
from shared.ceph_runner import get_metrics as get_ceph_runner_metrics
from shared.cluster_snapshot import read_snapshot
from shared.clusters import list_active_clusters
from shared.models import (
    Action,
    IncidentOutbox,
    RgwFederatedRoleMapping,
    TelegramOutbox,
    VitastorOperation,
)
from shared.service_health import status as service_status
from shared.time import utc_now
from watcher.cluster_snapshot_collector import get_metrics as get_collector_metrics


SLO_CONTRACT = {
    "window": "30d",
    "dashboard_freshness": {"target": 0.99, "error_budget": 0.01, "budget_unit": "fraction"},
    "incident_processing": {"target": 0.995, "error_budget": 0.005, "budget_unit": "fraction"},
    "notification_delivery": {"target": 0.99, "error_budget": 0.01, "budget_unit": "fraction"},
    "post_check": {"target": 0.995, "error_budget": 0.005, "budget_unit": "fraction"},
}

_QUEUE_ALERT_COUNT = 100
_QUEUE_ALERT_AGE_SECONDS = 300
_STALE_ALERT_SECONDS = 120
# A due retry should be claimed within one publisher cycle; past this it
# is overdue and the publisher is not draining the Outbox.
_RETRY_OVERDUE_SECONDS = 120


def _duration_seconds(value) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _proc_resources() -> dict[str, object]:
    """Read bounded cgroup/process counters without psutil or shell calls."""
    result: dict[str, object] = {"pid": os.getpid()}
    try:
        with open(f"/proc/{os.getpid()}/stat", encoding="utf-8") as handle:
            fields = handle.read().split()
        ticks = int(fields[13]) + int(fields[14])
        result["cpu_time_seconds"] = round(ticks / max(1, os.sysconf("SC_CLK_TCK")), 3)
    except (OSError, IndexError, ValueError):
        result["cpu_time_seconds"] = None
    try:
        with open(f"/proc/{os.getpid()}/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    result["rss_bytes"] = int(line.split()[1]) * 1024
                    break
    except (OSError, IndexError, ValueError):
        result["rss_bytes"] = None
    for name, path in (
        ("cgroup_memory_current_bytes", "/sys/fs/cgroup/memory.current"),
        ("cgroup_memory_max_bytes", "/sys/fs/cgroup/memory.max"),
        ("cgroup_cpu_usage_usec", "/sys/fs/cgroup/cpu.stat"),
    ):
        try:
            text = Path(path).read_text(encoding="utf-8").strip()
            if name == "cgroup_cpu_usage_usec":
                result[name] = int(next(line.split()[1] for line in text.splitlines() if line.startswith("usage_usec")))
            elif text != "max":
                result[name] = int(text)
        except (OSError, StopIteration, ValueError):
            result[name] = None
    return result


def _runtime_owner() -> dict[str, object]:
    containerized = os.environ.get("CEPH_AI_CONTAINERIZED", "").lower() == "true"
    return {
        "mode": "podman-compose" if containerized else "direct-process",
        "owner": "ceph-ai-containers.service" if containerized else "systemd service units",
        "scope": ["dashboard-web", "worker", "watcher", "remediation-watcher", "telegram-ai", "full-executor"],
        "single_owner": containerized,
        "restart_command": "systemctl restart ceph-ai-containers.service" if containerized else "scripts/deploy/restart_services.sh",
        "note": "Không được chạy song song Podman compose và các ceph-ai-*.service cho cùng process.",
    }


def _freshness(session) -> list[dict[str, object]]:
    rows = []
    for cluster in list_active_clusters(session):
        snapshot = read_snapshot(cluster.id)
        if snapshot is None:
            rows.append({"cluster_id": cluster.id, "available": False, "stale": True})
            continue
        age = _duration_seconds(snapshot.get("age_seconds"))
        rows.append({
            "cluster_id": cluster.id,
            "available": bool(snapshot.get("available", True)),
            "stale": bool(snapshot.get("stale")) or (age is not None and age > _STALE_ALERT_SECONDS),
            "age_seconds": age,
            "collector_lag_seconds": _duration_seconds(snapshot.get("collector_lag_seconds")),
            "last_error": str(snapshot.get("last_error") or "")[:240] or None,
            "collected_at": snapshot.get("collected_at"),
        })
    return rows


def _queue_row(session, model, *, statuses: tuple[str, ...], name: str) -> dict[str, object]:
    rows = session.query(model.status, func.count(model.id)).filter(model.status.in_(statuses)).group_by(model.status).all()
    counts = {str(status): int(count) for status, count in rows}
    oldest = session.query(func.min(model.created_at)).filter(model.status.in_(statuses)).scalar()
    age = None
    if oldest is not None:
        age = max(0.0, (utc_now() - oldest).total_seconds())
    pending = sum(counts.values())
    return {
        "name": name,
        "counts": counts,
        "pending_total": pending,
        "oldest_age_seconds": round(age, 1) if age is not None else None,
        "backlog_alert": pending >= _QUEUE_ALERT_COUNT or (age is not None and age >= _QUEUE_ALERT_AGE_SECONDS),
    }


def _outbox_row(session, model, *, name: str, attempt_limit: int, lease_seconds: int) -> dict[str, object]:
    """Live backlog plus the retry signals the Outbox recovery gate watches.

    Dead letters are reported separately: counting them in the live backlog
    would keep ``backlog_alert`` raised forever after one terminal failure and
    hide whether new traffic is flowing.
    """
    row = _queue_row(session, model, statuses=("PENDING", "PROCESSING"), name=name)
    now = utc_now()
    row["dead_total"] = int(session.query(func.count(model.id)).filter(model.status == "DEAD").scalar() or 0)
    row["max_attempts"] = int(
        session.query(func.max(model.attempts)).filter(model.status.in_(("PENDING", "PROCESSING"))).scalar() or 0
    )
    row["attempt_limit"] = attempt_limit
    row["overdue_retries"] = int(session.query(func.count(model.id)).filter(
        model.status == "PENDING",
        model.attempts > 0,
        model.next_attempt_at <= now - timedelta(seconds=_RETRY_OVERDUE_SECONDS),
    ).scalar() or 0)
    row["stuck_claims"] = int(session.query(func.count(model.id)).filter(
        model.status == "PROCESSING",
        model.claimed_at <= now - timedelta(seconds=lease_seconds),
    ).scalar() or 0)
    row["retry_exhaustion"] = row["max_attempts"] >= max(1, attempt_limit - 2)
    return row


def _db_pool() -> dict[str, object]:
    pool = getattr(db.engine, "pool", None)
    result = {"pool_size": None, "checked_in": None, "checked_out": None, "overflow": None, "exhausted": False}
    if pool is None:
        return result
    for key, method in (("pool_size", "size"), ("checked_in", "checkedin"), ("checked_out", "checkedout"), ("overflow", "overflow")):
        try:
            result[key] = int(getattr(pool, method)())
        except (AttributeError, TypeError, ValueError):
            pass
    size = result["pool_size"]
    checked_out = result["checked_out"]
    result["exhausted"] = bool(size is not None and checked_out is not None and checked_out >= size)
    result["database_url_kind"] = "postgresql" if str(db.engine.url).startswith("postgres") else "sqlite"
    return result


def _alerts(freshness, services, queues, pool, deploys, collector) -> list[dict[str, object]]:
    alerts = []
    for row in freshness:
        if not row.get("available") or row.get("stale"):
            alerts.append({"code": "stale_snapshot", "severity": "critical" if not row.get("available") else "warning", "cluster_id": row.get("cluster_id"), "message": "Snapshot không có hoặc vượt freshness SLO."})
        if row.get("last_error"):
            alerts.append({"code": "snapshot_refresh_error", "severity": "warning", "cluster_id": row.get("cluster_id"), "message": row["last_error"]})
    for name, value in services.items():
        if not value.get("healthy"):
            alerts.append({"code": "service_heartbeat_stale", "severity": "critical", "service": name, "message": f"{name} không có heartbeat hợp lệ."})
    for queue in queues:
        if queue["backlog_alert"]:
            alerts.append({"code": "queue_backlog", "severity": "warning", "queue": queue["name"], "pending_total": queue["pending_total"], "oldest_age_seconds": queue["oldest_age_seconds"], "message": "Queue backlog hoặc queue age vượt ngưỡng."})
        if queue.get("dead_total"):
            alerts.append({"code": "outbox_dead_letters", "severity": "critical", "queue": queue["name"], "count": queue["dead_total"], "message": "Outbox có message DEAD sau khi hết số lần retry; cần xử lý thủ công."})
        if queue.get("retry_exhaustion"):
            alerts.append({"code": "outbox_retry_exhaustion", "severity": "warning", "queue": queue["name"], "max_attempts": queue["max_attempts"], "attempt_limit": queue["attempt_limit"], "message": "Outbox message sắp hết số lần retry."})
        if queue.get("overdue_retries"):
            alerts.append({"code": "outbox_overdue_retry", "severity": "warning", "queue": queue["name"], "count": queue["overdue_retries"], "message": "Retry đã đến hạn nhưng publisher chưa xử lý."})
        if queue.get("stuck_claims"):
            alerts.append({"code": "outbox_stuck_claim", "severity": "warning", "queue": queue["name"], "count": queue["stuck_claims"], "message": "Message PROCESSING quá lease; publisher có thể đã chết giữa chừng."})
    if pool.get("exhausted"):
        alerts.append({"code": "db_pool_exhaustion", "severity": "critical", "message": "DB pool đã dùng hết connection."})
    if deploys:
        alerts.append({"code": "failed_deploy", "severity": "critical", "count": len(deploys), "message": "Có deploy operation thất bại trong 24 giờ gần nhất."})
    if int(collector.get("failure_total", 0)) > 0:
        alerts.append({"code": "collector_failure", "severity": "warning", "count": int(collector["failure_total"]), "message": "Collector có query thất bại; kiểm tra reconnect/reconcile."})
    return alerts[:100]


def collect_reliability() -> dict[str, object]:
    started = time.monotonic()
    services = {name: service_status(name, stale_after_seconds=60) for name in ("watcher", "worker", "remediation-watcher", "telegram-ai")}
    api = get_api_metrics()
    collector = get_collector_metrics()
    runner = get_ceph_runner_metrics()
    with db.SessionLocal() as session:
        freshness = _freshness(session)
        queues = [
            _outbox_row(
                session, IncidentOutbox, name="incident_outbox",
                attempt_limit=incident_outbox.MAX_ATTEMPTS, lease_seconds=incident_outbox.CLAIM_LEASE_SECONDS,
            ),
            _outbox_row(
                session, TelegramOutbox, name="telegram_outbox",
                attempt_limit=telegram_outbox.MAX_ATTEMPTS, lease_seconds=telegram_outbox.CLAIM_LEASE_SECONDS,
            ),
        ]
        actions = _queue_row(session, Action, statuses=("PENDING", "PENDING_APPROVAL", "APPROVED", "EXECUTING", "GRACE_PENDING"), name="action_queue")
        queues.append(actions)
        deploy_cutoff = utc_now() - timedelta(hours=24)
        failed_deploys = session.query(VitastorOperation).filter(
            VitastorOperation.status == "FAILED", VitastorOperation.finished_at >= deploy_cutoff,
        ).order_by(VitastorOperation.finished_at.desc()).limit(20).all()
        failed_deploy_view = [{"id": row.id, "operation": row.operation, "cluster_name": row.cluster_name, "finished_at": row.finished_at.isoformat() if row.finished_at else None, "error": str(row.error_message or "")[:240]} for row in failed_deploys]
        reconcile = {str(status): int(count) for status, count in session.query(RgwFederatedRoleMapping.status, func.count(RgwFederatedRoleMapping.id)).group_by(RgwFederatedRoleMapping.status).all()}
    pool = _db_pool()
    alerts = _alerts(freshness, services, queues, pool, failed_deploy_view, collector)
    elapsed = round((time.monotonic() - started) * 1000, 2)
    return {
        "schema": "ceph-ai.reliability.v1",
        "generated_at": utc_now().isoformat(),
        "collection_duration_ms": elapsed,
        "runtime_owner": _runtime_owner(),
        "restart_reconnect_reconcile": {
            "restart": {name: value.get("healthy", False) for name, value in services.items()},
            "reconnect": {"collector_failures_total": int(collector.get("failure_total", 0)), "ssh_connect_failures_total": int(runner.get("connect_failures_total", 0))},
            "reconcile": {"federated_role_mapping_status": reconcile, "action_queue_pending": actions["pending_total"]},
        },
        "slo": SLO_CONTRACT,
        "services": services,
        "snapshot_freshness": {"clusters": freshness, "stale_count": sum(bool(row.get("stale")) for row in freshness)},
        "queues": queues,
        "db_pool": pool,
        "api": api,
        "collector": collector,
        "ssh": runner,
        "resources": _proc_resources(),
        "failed_deploys_24h": failed_deploy_view,
        "alerts": alerts,
        "status": "critical" if any(item["severity"] == "critical" for item in alerts) else ("degraded" if alerts else "ok"),
    }
