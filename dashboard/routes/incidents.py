import asyncio
import json
import logging
import math
import threading
from time import monotonic
from urllib.parse import quote, urlencode
from datetime import datetime, timedelta
from shared.time import utc_now

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.routes.chat import CHAT_REQUEST_CEPH_CODE
from dashboard.routes.delete_cluster import CLUSTER_DELETE_CEPH_CODE
from dashboard.routes.deploy_cluster import CLUSTER_DEPLOY_CEPH_CODE
from dashboard.routes.upgrade import CLUSTER_UPGRADE_CEPH_CODE, is_cluster_upgrade_pending_or_approved
from watcher.log_analysis import LOG_ANOMALY_PREFIX
from dashboard.telegram_approval_bot import channels_for_incident, has_configured_channel
from dashboard.templating import make_templates
from dashboard.vntime import format_vn
from shared import audit, change_risk, db, heartbeat
from shared import incident_postmortem, trust_engine
from shared.unified_event_timeline import merge_event_sources
from shared.root_cause_chain import build_root_cause_chain
from dashboard import alert_center
from shared.cluster_nodes import configured_nodes
from shared.models import (
    Action, ActionStatus, AuditEntry, BackupJob, Cluster, Incident, IncidentStatus,
    IncidentTimelineEvent, ObjectStorageAuditEntry, RgwAccessAuditEvent,
    RemediationCase, WatcherHeartbeat,
)
from shared.cluster_snapshot import (
    DEFAULT_MAX_STALE_SECONDS,
    claim_refresh,
    is_refreshing,
    mark_refreshing,
    read_section_snapshot,
    read_snapshot,
)
from watcher.ceph_client import (
    run_ceph_json_command_with,
)
from watcher import cluster_snapshot_collector, incident_grouping

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# AC #2: "quá lâu chưa có poll mới" threshold — a multiple of the poll
# interval rather than a fixed number of seconds, so it scales with
# whatever watcher_poll_interval_seconds is configured to.
HEARTBEAT_STALE_MULTIPLIER = 3
_DASHBOARD_HEALTH_STALE_SECONDS = 180
_DASHBOARD_HEALTH_MAX_STALE_SECONDS = DEFAULT_MAX_STALE_SECONDS
_DASHBOARD_HEALTH_REFRESH_LOCKS: dict[str, threading.Lock] = {}
_DASHBOARD_HEALTH_REFRESH_LOCKS_GUARD = threading.Lock()
CASE_VERDICTS = {
    "CORRECT": "Chẩn đoán/xử lý đúng",
    "FALSE_POSITIVE": "Cảnh báo sai",
    "UNSAFE": "Hành động không an toàn",
    "INEFFECTIVE": "Không khắc phục được",
    "INCONCLUSIVE": "Chưa đủ bằng chứng",
}
ALERT_MUTE_HOURS = (1, 6, 24)


def _rca_evidence(raw: str | None) -> dict | None:
    """Prepare the safe, compact RCA evidence shown above the raw timeline."""
    evidence = incident_postmortem._safe_json(raw)
    if not isinstance(evidence, dict) or evidence.get("source") != "performance_rca":
        return None
    hosts = []
    seen_hosts = set()
    for row in evidence.get("host_evidence", []):
        if not isinstance(row, dict):
            continue
        host = str(row.get("host") or row.get("node_name") or "").strip()
        if not host or host in seen_hosts:
            continue
        seen_hosts.add(host)
        hosts.append({
            "host": host,
            "cpu_percent": row.get("cpu_percent"),
            "mem_percent": row.get("mem_percent"),
            "disk_latency_ms": row.get("disk_latency_ms"),
            "bottleneck": bool(row.get("bottleneck")),
        })
    topology = evidence.get("topology") if isinstance(evidence.get("topology"), dict) else {}
    def number(value):
        if isinstance(value, bool) or value is None:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    confidence = number(evidence.get("confidence"))
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))
    return {
        "hypothesis": evidence.get("hypothesis") or "—",
        "explanation": evidence.get("explanation") or "—",
        "confidence": confidence,
        "current_latency_ms": number(evidence.get("current_latency_ms")),
        "baseline_latency_ms": number(evidence.get("baseline_latency_ms")),
        "pool": evidence.get("pool"),
        "image": evidence.get("image"),
        "hosts": hosts,
        "pgid": topology.get("pgid"),
        "primary_osd": topology.get("primary_osd"),
        "data_object_count": topology.get("data_object_count"),
    }


def _rgw_incident_target(incident: Incident | None) -> dict | None:
    """Return a safe deep link for an RGW alert's bucket target."""
    if incident is None or not str(incident.ceph_code or "").startswith("RGW_ALERT_"):
        return None
    try:
        evidence = json.loads(incident.signal_evidence_json or "{}")
    except (TypeError, ValueError):
        return None
    target = evidence.get("target") if isinstance(evidence, dict) else None
    if not isinstance(target, dict) or target.get("type") != "bucket":
        return None
    bucket = str(target.get("id") or "").strip()
    if not bucket or len(bucket) > 255:
        return None
    return {
        "type": "bucket",
        "id": bucket,
        "label": f"Bucket {bucket}",
        "url": f"/object-storage/buckets/{quote(bucket, safe='')}",
    }


def _alert_group_for_incident(session, incident_id: str, selected_cluster: Cluster) -> dict | None:
    cluster_filter = (
        or_(Incident.cluster_id == selected_cluster.id, Incident.cluster_id.is_(None))
        if selected_cluster.is_default
        else Incident.cluster_id == selected_cluster.id
    )
    incidents = session.query(Incident).filter(cluster_filter).order_by(
        Incident.detected_at.desc(), Incident.id.desc()
    ).all()
    for group in alert_center.build_alert_groups(incidents):
        if any(row.id == incident_id for row in group["incidents"]):
            return group
    return None


def _alert_redirect(request: Request) -> RedirectResponse:
    """Return to the centre after a lifecycle action, without trusting an external URL."""
    return RedirectResponse("/alerts", status_code=303)



def _cached_node_inventory(mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode):
    commands = ("ceph orch host ls", "ceph node ls") if exec_mode == "cephadm" else ("ceph node ls",)
    for command in commands:
        try:
            return _run_monitor_command(
                mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode, command,
            )
        except Exception as exc:
            logger.info("dashboard_health: server inventory unavailable via %s: %s", command, exc)
    return None


def _dashboard_health_refresh_in_progress(cluster_id: str) -> bool:
    with _DASHBOARD_HEALTH_REFRESH_LOCKS_GUARD:
        lock = _DASHBOARD_HEALTH_REFRESH_LOCKS.get(cluster_id)
    return bool(lock and lock.locked()) or is_refreshing(cluster_id)


def _schedule_dashboard_health_refresh(selected_cluster: Cluster) -> bool:
    with _DASHBOARD_HEALTH_REFRESH_LOCKS_GUARD:
        lock = _DASHBOARD_HEALTH_REFRESH_LOCKS.setdefault(selected_cluster.id, threading.Lock())
    if not lock.acquire(blocking=False):
        return True
    if not claim_refresh(selected_cluster.id):
        lock.release()
        return True

    async def refresh() -> None:
        started = monotonic()
        started_at = cluster_snapshot_collector.collection_timestamp()
        try:
            configured_mon_nodes = [
                node.strip() for node in selected_cluster.ceph_mon_nodes.split(",") if node.strip()
            ]
            await asyncio.to_thread(
                cluster_snapshot_collector.collect_and_publish_health,
                selected_cluster,
                mon_nodes=configured_mon_nodes,
                collection_started_monotonic=started,
                collection_started_at=started_at,
            )
        except Exception as exc:
            logger.info("dashboard_health(%s): snapshot refresh failed: %s", selected_cluster.name, exc)
        finally:
            mark_refreshing(selected_cluster.id, False)
            lock.release()

    asyncio.create_task(refresh())
    return True

@router.get("/alerts", response_class=HTMLResponse)
async def alert_center_page(request: Request, user: str = Depends(require_login)):
    status_filter = request.query_params.get("status", "all").strip().lower()
    severity_filter = request.query_params.get("severity", "all").strip().upper()
    period_filter = request.query_params.get("period", "all").strip().lower()
    code_filter = request.query_params.get("code", "").strip()
    if status_filter not in {"all", "open", "closed", "acknowledged", "muted"}:
        status_filter = "all"
    if severity_filter not in {"ALL", "HEALTH_WARN", "HEALTH_ERR"}:
        severity_filter = "ALL"
    if period_filter not in {"all", "24h", "7d", "30d", "1y"}:
        period_filter = "all"
    try:
        clusters, selected_cluster = _resolve_selected_cluster(
            request.query_params.get("cluster", ""), request.session.get("selected_cluster_id", ""), request=request
        )
        request.session["selected_cluster_id"] = selected_cluster.id
        cluster_filter = (
            or_(Incident.cluster_id == selected_cluster.id, Incident.cluster_id.is_(None))
            if selected_cluster.is_default
            else Incident.cluster_id == selected_cluster.id
        )
        with db.SessionLocal() as session:
            query = session.query(Incident).filter(cluster_filter)
            if severity_filter != "ALL":
                query = query.filter(Incident.severity == severity_filter)
            if code_filter:
                query = query.filter(Incident.ceph_code.ilike(f"%{code_filter}%"))
            if period_filter != "all":
                hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30, "1y": 24 * 365}[period_filter]
                query = query.filter(Incident.detected_at >= utc_now() - timedelta(hours=hours))
            incidents = query.order_by(Incident.detected_at.desc(), Incident.id.desc()).all()
    except SQLAlchemyError:
        logger.exception("alert_center: failed to query incidents from DB")
        raise HTTPException(status_code=503, detail="Không kết nối được database")
    groups = alert_center.build_alert_groups(incidents)
    for group in groups:
        group["target"] = _rgw_incident_target(group.get("representative"))
    if status_filter == "open":
        groups = [group for group in groups if group["is_active"]]
    elif status_filter == "closed":
        groups = [group for group in groups if not group["is_active"]]
    elif status_filter == "acknowledged":
        groups = [group for group in groups if group["is_acknowledged"]]
    elif status_filter == "muted":
        groups = [group for group in groups if group["is_muted"]]
    try:
        page = int(request.query_params.get("page", "1"))
    except (TypeError, ValueError):
        page = 1
    page_size = 10
    page_data = alert_center.paginate_alert_groups(groups, page=page, page_size=page_size)
    filter_query = urlencode({
        "cluster": selected_cluster.id,
        "status": status_filter,
        "severity": severity_filter.lower(),
        "period": period_filter,
        "code": code_filter,
    })
    return templates.TemplateResponse(request, "alerts.html", {
        "user": user,
        "is_admin": auth.is_admin_user(user),
        "clusters": clusters,
        "selected_cluster": selected_cluster,
        "alert_groups": page_data["items"],
        "total_groups": page_data["total_groups"],
        "total_pages": page_data["total_pages"],
        "page": page_data["page"],
        "page_size": page_data["page_size"],
        "total_incidents": len(incidents),
        "active_groups": sum(1 for group in groups if group["is_active"]),
        "status_filter": status_filter,
        "severity_filter": severity_filter,
        "period_filter": period_filter,
        "code_filter": code_filter,
        "filter_query": filter_query,
        "mute_hours": ALERT_MUTE_HOURS,
    })


async def _update_alert_lifecycle(
    request: Request, incident_id: str, user: str, operation: str, mute_hours: int | None = None,
):
    _clusters, selected_cluster = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        group = _alert_group_for_incident(session, incident_id, selected_cluster)
        if group is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy nhóm cảnh báo trong cụm đang chọn")
        rows = group["incidents"]
        now = utc_now()
        if operation == "acknowledge":
            for row in rows:
                row.acknowledged_at, row.acknowledged_by = now, user
            event_type = audit.EVENT_ALERT_ACKNOWLEDGED
            evidence = {"occurrence_count": len(rows)}
        elif operation == "unacknowledge":
            for row in rows:
                row.acknowledged_at = row.acknowledged_by = None
            event_type = audit.EVENT_ALERT_UNACKNOWLEDGED
            evidence = {"occurrence_count": len(rows)}
        elif operation == "mute":
            if mute_hours not in ALERT_MUTE_HOURS:
                raise HTTPException(status_code=400, detail="Thời gian mute không hợp lệ")
            until = now + timedelta(hours=mute_hours)
            for row in rows:
                row.muted_until, row.muted_by = until, user
            event_type = audit.EVENT_ALERT_MUTED
            evidence = {"occurrence_count": len(rows), "hours": mute_hours}
        else:
            for row in rows:
                row.muted_until, row.muted_by = None, None
            event_type = audit.EVENT_ALERT_UNMUTED
            evidence = {"occurrence_count": len(rows)}
        audit.record(session, incident_id=group["representative"].id, action_id=None,
                     event_type=event_type, actor=user, evidence=evidence)
        session.commit()
    return _alert_redirect(request)


@router.post("/alerts/{incident_id}/acknowledge")
async def acknowledge_alert(request: Request, incident_id: str, user: str = Depends(require_login)):
    return await _update_alert_lifecycle(request, incident_id, user, "acknowledge")


@router.post("/alerts/{incident_id}/unacknowledge")
async def unacknowledge_alert(request: Request, incident_id: str, user: str = Depends(require_login)):
    return await _update_alert_lifecycle(request, incident_id, user, "unacknowledge")


@router.post("/alerts/{incident_id}/mute")
async def mute_alert(
    request: Request, incident_id: str, hours: int = Form(...), user: str = Depends(require_login)
):
    return await _update_alert_lifecycle(request, incident_id, user, "mute", hours)


@router.post("/alerts/{incident_id}/unmute")
async def unmute_alert(request: Request, incident_id: str, user: str = Depends(require_login)):
    return await _update_alert_lifecycle(request, incident_id, user, "unmute")


def _incident_in_selected_cluster(session, incident_id: str, selected_cluster: Cluster) -> Incident | None:
    query = session.query(Incident).filter(Incident.id == incident_id)
    query = query.filter(
        or_(Incident.cluster_id == selected_cluster.id, Incident.cluster_id.is_(None))
        if selected_cluster.is_default else Incident.cluster_id == selected_cluster.id
    )
    return query.one_or_none()


@router.get("/api/events/timeline")
async def unified_event_timeline_api(
    request: Request,
    limit: int = Query(200, ge=1, le=500),
    user: str = Depends(require_login),
):
    """Merge cluster-scoped lifecycle, audit and RGW events read-only."""
    del user
    _clusters, cluster = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        incident_scope = (
            or_(Incident.cluster_id == cluster.id, Incident.cluster_id.is_(None))
            if cluster.is_default else Incident.cluster_id == cluster.id
        )
        incidents = (
            session.query(Incident)
            .filter(incident_scope)
            .order_by(Incident.detected_at.desc(), Incident.id.desc())
            .limit(limit)
            .all()
        )
        incident_ids = [incident.id for incident in incidents]
        actions = (
            session.query(Action).filter(Action.incident_id.in_(incident_ids))
            .order_by(Action.created_at.desc(), Action.id.desc()).limit(limit).all()
            if incident_ids else []
        )
        audits = (
            session.query(AuditEntry).filter(AuditEntry.incident_id.in_(incident_ids))
            .order_by(AuditEntry.created_at.desc(), AuditEntry.id.desc()).limit(limit).all()
            if incident_ids else []
        )
        lifecycle = (
            session.query(IncidentTimelineEvent)
            .filter(IncidentTimelineEvent.incident_id.in_(incident_ids))
            .order_by(IncidentTimelineEvent.created_at.desc(), IncidentTimelineEvent.id.desc())
            .limit(limit).all()
            if incident_ids else []
        )
        object_audits = (
            session.query(ObjectStorageAuditEntry)
            .filter(ObjectStorageAuditEntry.cluster_id == cluster.id)
            .order_by(ObjectStorageAuditEntry.created_at.desc(), ObjectStorageAuditEntry.id.desc())
            .limit(limit).all()
        )
        rgw_events = (
            session.query(RgwAccessAuditEvent)
            .filter(RgwAccessAuditEvent.cluster_id == cluster.id)
            .order_by(RgwAccessAuditEvent.event_at.desc(), RgwAccessAuditEvent.id.desc())
            .limit(limit).all()
        )
        sources = {
            "incident": [{
                "id": row.id, "at": row.detected_at, "kind": "incident_detected",
                "actor": "watcher", "summary": row.ceph_code, "incident_id": row.id,
                "cluster_id": row.cluster_id,
            } for row in incidents],
            "action": [{
                "id": row.id, "at": row.created_at, "kind": "action_proposed",
                "actor": "system", "summary": row.rationale or row.action_id,
                "incident_id": row.incident_id, "action_id": row.id,
                "status": row.status, "cluster_id": cluster.id,
            } for row in actions],
            "audit": [{
                "id": row.id, "at": row.created_at, "kind": row.event_type,
                "actor": row.actor, "summary": row.event_type,
                "incident_id": row.incident_id, "action_id": row.action_id,
                "cluster_id": cluster.id,
            } for row in audits],
            "lifecycle": [{
                "id": row.id, "at": row.created_at, "kind": row.event_type,
                "actor": row.actor, "summary": row.event_type,
                "incident_id": row.incident_id, "action_id": row.action_id,
                "cluster_id": cluster.id,
            } for row in lifecycle],
            "object_storage": [{
                "id": row.id, "at": row.created_at, "kind": row.action,
                "actor": row.actor, "summary": row.action,
                "status": row.result, "target": row.target_type,
                "cluster_id": row.cluster_id,
            } for row in object_audits],
            "rgw": [{
                "id": row.id, "at": row.event_at, "kind": row.action,
                "actor": row.requester or "anonymous", "summary": row.action,
                "status": row.http_status, "target": row.bucket,
                "cluster_id": row.cluster_id,
            } for row in rgw_events],
        }
    result = merge_event_sources(sources, limit=limit)
    result["cluster_id"] = str(cluster.id)
    result["cluster_name"] = str(cluster.name)
    return result


@router.get("/api/incidents/{incident_id}/root-cause-chain")
async def root_cause_chain_api(
    request: Request,
    incident_id: str,
    user: str = Depends(require_login),
):
    """Return citation-linked root-cause candidates without executing actions."""
    del user
    _clusters, selected = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        incident = _incident_in_selected_cluster(session, incident_id, selected)
        if incident is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Incident trong cụm đang chọn")
        timeline = incident_postmortem.build_timeline(session, incident_id)
    return build_root_cause_chain(timeline)


@router.get("/api/incidents/{incident_id}/postmortem/export")
async def export_incident_postmortem(
    request: Request,
    incident_id: str,
    user: str = Depends(require_login),
):
    _clusters, selected = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        incident = _incident_in_selected_cluster(session, incident_id, selected)
        if incident is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Incident trong cụm đang chọn")
        timeline = incident_postmortem.build_timeline(session, incident_id)
        try:
            postmortem = json.loads(incident.postmortem_json) if incident.postmortem_json else None
        except (TypeError, ValueError):
            postmortem = None
    content = incident_postmortem.render_postmortem_markdown(timeline, postmortem)
    return PlainTextResponse(
        content,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="incident-{incident_id}-postmortem.md"'},
    )


@router.post("/api/incidents/{incident_id}/postmortem/review")
async def review_incident_postmortem(
    request: Request,
    incident_id: str,
    user: str = Depends(require_login),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được review postmortem")
    body = await request.json()
    decision = str(body.get("decision") or "").strip().lower()
    note = str(body.get("note") or "").strip()
    if decision not in {"approved", "rejected"}:
        raise HTTPException(status_code=400, detail="Review decision không hợp lệ")
    if decision == "rejected" and len(note) < 5:
        raise HTTPException(status_code=400, detail="Review reject cần note ít nhất 5 ký tự")
    _clusters, selected = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        incident = _incident_in_selected_cluster(session, incident_id, selected)
        if incident is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Incident trong cụm đang chọn")
        if not incident.postmortem_json:
            raise HTTPException(status_code=409, detail="Incident chưa có postmortem để review")
        audit.record(
            session,
            incident_id=incident.id,
            action_id=None,
            event_type=f"postmortem_review_{decision}",
            actor=user,
            evidence={"note": note[:1000], "decision": decision},
        )
        session.commit()
    return {"incident_id": incident_id, "decision": decision, "reviewed_by": user, "read_only": False}


@router.get("/incidents/{incident_id}/timeline", response_class=HTMLResponse)
async def incident_timeline_page(request: Request, incident_id: str, user: str = Depends(require_login)):
    clusters, selected_cluster = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        incident = _incident_in_selected_cluster(session, incident_id, selected_cluster)
        if incident is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Incident trong cụm đang chọn")
        incident_group = incident_grouping.build_group_context(session, incident_id)
        for related in incident_group.get("related_incidents", []):
            try:
                related["detected_at_display"] = format_vn(
                    datetime.fromisoformat(related["detected_at"])
                )
            except (KeyError, TypeError, ValueError):
                related["detected_at_display"] = related.get("detected_at") or "—"
        timeline = incident_postmortem.build_timeline(session, incident_id)
        postmortem = json.loads(incident.postmortem_json) if incident.postmortem_json else None
        generated_at = incident.postmortem_generated_at
        remediation_cases = (
            session.query(RemediationCase)
            .filter_by(incident_id=incident_id)
            .order_by(RemediationCase.created_at, RemediationCase.id)
            .all()
        )
        grace_action_ids = {
            row.id for row in session.query(Action.id).filter(
                Action.incident_id == incident_id,
                Action.status == ActionStatus.GRACE_PENDING.value,
            ).all()
        }
        rca_evidence = _rca_evidence(incident.signal_evidence_json)
    return templates.TemplateResponse(request, "incident_timeline.html", {
        "user": user, "is_admin": auth.is_admin_user(user), "clusters": clusters,
        "selected_cluster": selected_cluster, "incident": incident, "timeline": timeline,
        "incident_group": incident_group,
        "postmortem": postmortem, "postmortem_generated_at": generated_at,
        "postmortem_error": request.query_params.get("error", ""),
        "remediation_cases": remediation_cases, "case_verdicts": CASE_VERDICTS,
        "shadow_comparison": trust_engine.shadow_comparison,
        "grace_action_ids": grace_action_ids,
        "rca_evidence": rca_evidence,
        "incident_target": _rgw_incident_target(incident),
    })


@router.post("/incidents/{incident_id}/cases/{case_id}/verdict")
async def update_remediation_case_verdict(
    request: Request,
    incident_id: str,
    case_id: str,
    verdict: str = Form(...),
    note: str = Form(""),
    user: str = Depends(require_login),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ admin được ghi verdict học máy")
    verdict = verdict.strip().upper()
    note = note.strip()
    if verdict not in CASE_VERDICTS:
        raise HTTPException(status_code=400, detail="Operator verdict không hợp lệ")
    if verdict not in {"CORRECT", "INCONCLUSIVE"} and len(note) < 5:
        raise HTTPException(status_code=400, detail="Verdict sai/không an toàn cần ghi chú ít nhất 5 ký tự")
    if len(note) > 2000:
        raise HTTPException(status_code=400, detail="Ghi chú tối đa 2000 ký tự")
    _clusters, selected_cluster = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        incident = _incident_in_selected_cluster(session, incident_id, selected_cluster)
        if incident is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Incident trong cụm đang chọn")
        case = session.query(RemediationCase).filter_by(id=case_id, incident_id=incident_id).one_or_none()
        if case is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Remediation Case")
        case.operator_verdict = verdict
        case.operator_note = note or None
        case.operator_verdict_by = user
        case.operator_verdict_at = utc_now()
        audit.record(
            session, incident_id=incident_id, action_id=case.action_id,
            event_type=audit.EVENT_REMEDIATION_CASE_VERDICT_UPDATED, actor=user,
        )
        session.commit()
    return RedirectResponse(f"/incidents/{incident_id}/timeline", status_code=303)


@router.post("/incidents/{incident_id}/postmortem")
async def generate_incident_postmortem(
    request: Request, incident_id: str, user: str = Depends(require_login)
):
    _clusters, selected_cluster = _resolve_selected_cluster(
        "", request.session.get("selected_cluster_id", ""), request=request
    )
    with db.SessionLocal() as session:
        if _incident_in_selected_cluster(session, incident_id, selected_cluster) is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy Incident trong cụm đang chọn")
    try:
        await incident_postmortem.generate(incident_id)
    except Exception as exc:
        logger.warning("generate_incident_postmortem: %s", exc)
        from urllib.parse import quote
        return RedirectResponse(
            f"/incidents/{incident_id}/timeline?error={quote(str(exc) or type(exc).__name__)}",
            status_code=303,
        )
    return RedirectResponse(f"/incidents/{incident_id}/timeline", status_code=303)


def _run_monitor_command(mon_nodes, container_name: str, ssh_user: str,
                         ssh_key_path: str, exec_mode: str, command: str):
    return run_ceph_json_command_with(
        mon_nodes, container_name, ssh_user, ssh_key_path, exec_mode, command
    )

# Incident statuses that mean "still needs attention" — anything else
# (RESOLVED / AUTO_FIXED / REJECTED) is considered closed for status purposes.
OPEN_STATUSES = {
    IncidentStatus.NEW.value,
    IncidentStatus.DIAGNOSING.value,
    IncidentStatus.PENDING_APPROVAL.value,
    IncidentStatus.APPROVED.value,
    IncidentStatus.EXECUTING.value,
    # 2026-08-20: lệnh khắc phục đã chạy nhưng chưa xác minh là hết lỗi.
    # Chưa được coi là đóng — cả mục đích hiển thị lẫn mục đích tính trạng
    # thái tổng của cụm: nói "OK" trong khi còn đang chờ kiểm chứng chính
    # là kiểu lạc quan sai mà cả tính năng này sinh ra để chấm dứt.
    IncidentStatus.VERIFYING.value,
    IncidentStatus.FAILED.value,
}


def compute_cluster_status(incidents: list[Incident], heartbeat_stale: bool) -> str:
    """Derive an aggregate cluster status from stored Incidents.

    The Dashboard itself never queries Ceph directly — Watcher (Story 1.3)
    does that over SSH and writes real health transitions here as Incident
    rows. This function only aggregates what's already recorded, so it's as
    fresh as Watcher's last poll (settings.watcher_poll_interval_seconds),
    not literally real-time.

    "No open incidents" alone does not mean the cluster is healthy — it
    also happens when Watcher has never successfully reached the cluster at
    all, so there is no real health data to aggregate yet (0 rows in
    `incidents` either way, indistinguishable without extra context). Only
    report "OK" when the heartbeat confirms Watcher has actually reached the
    cluster recently (`not heartbeat_stale`); otherwise report "UNKNOWN"
    rather than defaulting to a reassuring "OK" with no evidence behind it.
    Real recorded incidents (WARN/ERR below) are historical fact regardless
    of current heartbeat staleness, so they're unaffected by this check.

    2026-07-23 fix #1: `dashboard/routes/chat.py::confirm_chat_action` creates
    a synthetic Incident (ceph_code=CHAT_REQUEST_CEPH_CODE) for every
    chat-confirmed action, purely so it can reuse the existing Action/
    evidence of real Ceph cluster health. Before this fix, a chat action
    that failed for an unrelated reason (e.g. a bad parameter, or — as
    actually happened — the Worker process running stale code) flipped
    Incident.status to FAILED and this function reported the WHOLE cluster
    as "ERR", even though `ceph health` was fine the entire time. Excluded
    here so only Watcher-detected incidents ever drive this aggregate — a
    failed chat action is still fully visible via its own Action row/audit
    trail, just not conflated with cluster health.

    Same reasoning applies to `dashboard/routes/upgrade.py`'s synthetic
    Incident (ceph_code=CLUSTER_UPGRADE_CEPH_CODE) — an upgrade proposal
    that's rejected, or whose `ceph orch upgrade start` command itself
    fails to send, must not flip the cluster-wide health badge to "ERR"
    either; it's the same kind of "our own pipeline's outcome", not a real
    `ceph health` signal.

    2026-07-25: and to `dashboard/routes/deploy_cluster.py`'s synthetic
    Incident (ceph_code=CLUSTER_DEPLOY_CEPH_CODE) — building a BRAND-NEW
    cluster that isn't even monitored yet must never be conflated with the
    health of whatever cluster IS currently configured/monitored; a failed
    deploy attempt is visible via its own Action row/audit trail only.

    2026-07-26: and to `dashboard/routes/delete_cluster.py`'s synthetic
    Incident (ceph_code=CLUSTER_DELETE_CEPH_CODE) — a failed/rejected
    delete proposal must not itself flip the cluster-health badge; the
    ACTUAL health impact of a successful deletion shows up naturally once
    the cluster is gone (heartbeat_stale/no more incidents), not via this
    synthetic row.

    2026-07-23 fix #2: this used to derive ERR from
    `Incident.status == FAILED` — i.e. "did OUR remediation attempt fail",
    not "is the cluster actually in HEALTH_ERR". Those are different
    things: a plain HEALTH_WARN check (e.g. POOL_APP_NOT_ENABLED) whose
    recommended action had no automated fix (investigate_manually) or
    whose fix genuinely failed would still get reported as cluster-wide
    "ERR", contradicting `ceph health`'s own real HEALTH_WARN status — and
    the page's own copy (index.html) explicitly promises this badge
    reflects "tình trạng CỦA CLUSTER (vd HEALTH_WARN/HEALTH_ERR thật)", not
    remediation outcome. Watcher already records Ceph's own real per-check
    severity on every Incident it creates (`Incident.severity`, from
    `checks[code]["severity"]" — see watcher/main.py::
    build_and_publish_incident) — that is the correct, authoritative signal
    to use instead. A remediation's success/failure is still fully visible
    via the Action row/pending-approval section/audit trail; it no longer
    overrides the cluster-health badge.
    """
    real_incidents = [
        i
        for i in incidents
        if i.ceph_code
        not in (
            CHAT_REQUEST_CEPH_CODE,
            CLUSTER_UPGRADE_CEPH_CODE,
            CLUSTER_DEPLOY_CEPH_CODE,
            CLUSTER_DELETE_CEPH_CODE,
        )
        # 2026-08-19 (Log Intelligence L4): LOG_ANOMALY: là GIẢ THUYẾT của
        # AI đọc từ log, không phải một phép đo như OSD_LATENCY_HIGH:
        # (`ceph osd perf`) hay NODE_RESOURCE_HIGH: (CPU/RAM thật) — những
        # cái đó vẫn tính vào badge. Badge này hứa phản ánh "tình trạng CỦA
        # CLUSTER (vd HEALTH_WARN/HEALTH_ERR thật)"; để một suy luận của
        # model bôi đỏ nó sẽ làm mất đúng lời hứa đó và bào mòn niềm tin
        # vào badge. Phát hiện vẫn hiện đầy đủ trong danh sách Incident và
        # ở hàng chờ duyệt, chỉ không tự mình đổi màu badge.
        and not (i.ceph_code or "").startswith(LOG_ANOMALY_PREFIX)
    ]
    open_incidents = [i for i in real_incidents if i.status in OPEN_STATUSES]
    if not open_incidents:
        return "UNKNOWN" if heartbeat_stale else "OK"
    if any(i.severity == "HEALTH_ERR" for i in open_incidents):
        return "ERR"
    return "WARN"


def is_heartbeat_stale(latest_heartbeat: WatcherHeartbeat | None) -> bool:
    """AC #2/#3: "mất kết nối cụm" is a SEPARATE signal from
    compute_cluster_status() (which only reflects recorded Incident data —
    i.e. "is the cluster healthy"). This answers "can Watcher currently
    reach the cluster at all" — true (stale/lost) when Watcher has never
    completed a poll, the last poll failed, or the last poll is old enough
    that Watcher may have silently died."""
    if latest_heartbeat is None:
        return True
    if not latest_heartbeat.success:
        return True
    age = utc_now() - latest_heartbeat.polled_at
    return age > timedelta(seconds=HEARTBEAT_STALE_MULTIPLIER * settings.watcher_poll_interval_seconds)


# Epic 9, Story 9.4 (AC #2): how far back a FAILED BackupJob still counts
# as an active alert — an old failure that's since been superseded by a
# later success shouldn't keep the banner lit forever.
BACKUP_ALERT_LOOKBACK_HOURS = 24


def _recent_backup_failure(session) -> BackupJob | None:
    """Simple, self-contained Dashboard signal (AC #2) — deliberately does
    NOT read worker/policy/backup_policy.yaml's tracked_images (AD-3:
    dashboard/ must not import worker/backup/ execution code, and
    policy_config.py exists purely to serve that code); just asks "is
    there any BackupJob that failed recently". The fuller, policy-aware
    "never backed up"/"overdue past RPO" check that also fires the
    outbound webhook lives in worker/backup/alerting.py's periodic job —
    this is only the at-a-glance Dashboard banner, same scope as
    is_heartbeat_stale()'s single-condition check above."""
    cutoff = utc_now() - timedelta(hours=BACKUP_ALERT_LOOKBACK_HOURS)
    return (
        session.query(BackupJob)
        .filter(BackupJob.status == "FAILED", BackupJob.created_at >= cutoff)
        .order_by(BackupJob.created_at.desc())
        .first()
    )

def _recent_backup_failure_for_cluster(
    session, cluster_id: str, is_default_cluster: bool
) -> BackupJob | None:
    """Latest failure for exactly the cluster currently being viewed."""
    cutoff = utc_now() - timedelta(hours=BACKUP_ALERT_LOOKBACK_HOURS)
    cluster_filter = (
        or_(BackupJob.cluster_id == cluster_id, BackupJob.cluster_id.is_(None))
        if is_default_cluster
        else BackupJob.cluster_id == cluster_id
    )
    return (
        session.query(BackupJob)
        .filter(BackupJob.status == "FAILED", BackupJob.created_at >= cutoff, cluster_filter)
        .order_by(BackupJob.created_at.desc())
        .first()
    )


def _parse_datetime_filter(raw: str) -> datetime | None:
    """Accepts the value an HTML <input type="datetime-local"> submits
    (`YYYY-MM-DDTHH:MM`). Returns None for blank/unparseable input rather
    than raising — an invalid filter should be silently ignored (show
    everything), not 500 the whole page."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _query_audit_entries(
    session,
    incident_id: str,
    since_dt: datetime | None,
    until_dt: datetime | None,
    cluster_id: str,
    is_default_cluster: bool,
) -> list[AuditEntry]:
    cluster_filter = (
        or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
        if is_default_cluster
        else Incident.cluster_id == cluster_id
    )
    query = session.query(AuditEntry).join(Incident, AuditEntry.incident_id == Incident.id).filter(cluster_filter)
    if incident_id:
        query = query.filter(AuditEntry.incident_id == incident_id)
    if since_dt is not None:
        query = query.filter(AuditEntry.created_at >= since_dt)
    if until_dt is not None:
        query = query.filter(AuditEntry.created_at <= until_dt)
    # The dashboard only needs a bounded recent preview during first paint;
    # the full history remains available from the dedicated audit view.
    return query.order_by(AuditEntry.created_at.desc()).limit(100).all()


def _fetch_dashboard_data(
    incident_id: str,
    since_dt: datetime | None,
    until_dt: datetime | None,
    cluster_id: str,
    is_default_cluster: bool,
    cluster_names_by_id: dict[str, str],
) -> tuple[
    list[Incident],
    WatcherHeartbeat | None,
    list[tuple[Action, Incident | None, str, bool]],
    list[AuditEntry],
    bool,
    BackupJob | None,
    bool,
    bool,
]:
    # The visible realtime overview is bootstrapped from the persistent
    # snapshot. Keep the secondary server-rendered feeds bounded so an old
    # history cannot block first paint or exhaust the DB pool when several
    # tabs open together.
    initial_feed_limit = 100
    with db.SessionLocal() as session:
        # Every cluster-owned feed is scoped to the current selection.
        # Pre-migration Incident
        # rows have `cluster_id IS NULL`, which means "the default cluster"
        # (Incident's own docstring) — only match those when the SELECTED
        # cluster IS the default one.
        incident_cluster_filter = (
            or_(Incident.cluster_id == cluster_id, Incident.cluster_id.is_(None))
            if is_default_cluster
            else Incident.cluster_id == cluster_id
        )
        incidents = (
            session.query(Incident)
            .filter(incident_cluster_filter)
            .order_by(Incident.detected_at.desc())
            .limit(initial_feed_limit)
            .all()
        )
        latest_heartbeat = heartbeat.get_latest(session, cluster_id)
        # Cheap DB-only signal (no SSH) for disabling "Duyệt" on every OTHER
        # pending risky action while a cluster upgrade is proposed/approved —
        # see dashboard/routes/upgrade.py::is_cluster_upgrade_pending_or_approved
        # for what this does and does NOT cover (the window after the Worker
        # has already sent `ceph orch upgrade start` is deliberately not
        # checked here — that would need a live SSH call on every Dashboard
        # page load; the authoritative gate for THAT window lives in
        # dashboard/routes/actions.py::approve_action instead).
        upgrade_blocks_other_actions = (
            is_cluster_upgrade_pending_or_approved(session) if is_default_cluster else False
        )
        # 2026-07-23 restore: a RISKY Action — whether from the auto-diagnosis
        # pipeline OR a chat-confirmed proposal (dashboard/routes/chat.py::
        # confirm_chat_action, same Action/Incident state machine) — lands
        # here and STAYS here until POST /actions/{id}/approve|reject is hit.
        # Between 5a29d4e (removed this card, assuming Chat-with-AI's confirm
        # click was itself sufficient) and this fix, nothing in the UI ever
        # called those endpoints for a chat-originated RISKY action, so it
        # (e.g. restart_osd_daemon, still `risky:` in action_policy.yaml)
        # sat in PENDING_APPROVAL forever — visibly proposed, silently never
        # run, with no operator-facing indication anything further was
        # needed.
        pending_actions_rows = (
            session.query(Action)
            .join(Incident, Action.incident_id == Incident.id)
            .filter(incident_cluster_filter)
            .filter(Action.status == ActionStatus.PENDING_APPROVAL.value)
            .order_by(Action.created_at.desc())
            .all()
        )
        other_cluster_filter = (
            Incident.cluster_id.notin_([cluster_id])
            if is_default_cluster
            else or_(Incident.cluster_id != cluster_id, Incident.cluster_id.is_(None))
        )
        has_other_cluster_pending = (
            session.query(Action.id)
            .join(Incident, Action.incident_id == Incident.id)
            .filter(Action.status == ActionStatus.PENDING_APPROVAL.value, other_cluster_filter)
            .first()
            is not None
        )
        # Resolve only the selected cluster's pending action incidents.
        pending_incident_ids = {a.incident_id for a in pending_actions_rows}
        pending_incidents_by_id = (
            {
                row.id: row
                for row in session.query(Incident).filter(Incident.id.in_(pending_incident_ids)).all()
            }
            if pending_incident_ids
            else {}
        )
        pending_actions = []
        for action in pending_actions_rows:
            pending_incident = pending_incidents_by_id.get(action.incident_id)
            try:
                risk = change_risk.assess_and_record(
                    session, action=action, incident=pending_incident,
                )
                change_risk.attach_summary(action, risk)
            except Exception:
                logger.exception("Unable to refresh change-risk assessment for action %s", action.id)
            cluster_label = ""
            if pending_incident is not None and pending_incident.cluster_id is not None:
                cluster_label = cluster_names_by_id.get(pending_incident.cluster_id, "")
            # 2026-08-10 (multi-tenant remediation Phase 2): whether THIS
            # action actually has a reachable Telegram channel — a
            # non-default cluster with no channel of its own is NOT covered
            # by the 3 global channels anymore (channels_for_incident
            # narrows, doesn't add) — see index()'s own use of this to keep
            # the "Chờ duyệt" card from ever stranding such an action, the
            # exact bug class docs/telegram-alerts.md mục 6.7 already
            # documents having happened once before.
            telegram_covered = bool(channels_for_incident(pending_incident, session))
            pending_actions.append((action, pending_incident, cluster_label, telegram_covered))
        # Audit Trail (filters + full history) now lives directly on the
        # Dashboard — there is no separate /audit page anymore.
        audit_entries = _query_audit_entries(
            session, incident_id, since_dt, until_dt, cluster_id, is_default_cluster
        )
        # Epic 9, Story 9.4 (AC #2) — see _recent_backup_failure's docstring.
        backup_alert = _recent_backup_failure_for_cluster(session, cluster_id, is_default_cluster)
    # 2026-08-07: the "Chờ duyệt — Risky Action" card is only shown as a
    # FALLBACK now that dashboard/telegram_approval_bot.py broadcasts the
    # same proposal (with Duyệt/Từ chối buttons) to every configured
    # Telegram channel — see docs/telegram-alerts.md mục 6. Do NOT remove
    # this card outright: if no Telegram channel is configured (or it's
    # been cleared since), this is the ONLY remaining place to
    # approve/reject an auto-diagnosed or Chat-with-AI-confirmed RISKY
    # Action — the exact stranding bug test_dashboard_actions.py::
    # test_index_shows_pending_action_card's docstring already describes
    # from 2026-07-23, before Telegram approval existed.
    telegram_configured = has_configured_channel()
    return (
        incidents,
        latest_heartbeat,
        pending_actions,
        audit_entries,
        upgrade_blocks_other_actions,
        backup_alert,
        telegram_configured,
        has_other_cluster_pending,
    )


def _resolve_selected_cluster(
    requested_cluster_id: str, session_cluster_id: str = "", *, request: Request | None = None
) -> tuple[list[Cluster], Cluster]:
    """Multi-cluster observability Phase 1's cluster switcher — `?cluster=`
    on `/` (a plain query param, same pattern this file already uses for
    incident_id/since/until — bookmarkable, and every existing link/form on
    this page keeps working unchanged since it's additive).

    2026-08-11: `?cluster=` alone isn't enough to make a choice actually
    "stick" — nothing else in the app (nav links, the brand link back to
    `/`) ever forwards it, so leaving /volumes, /nodes etc. via the nav bar
    and coming back to Dashboard silently lost the selection and landed
    back on the default cluster even though the picker still LOOKED
    selected on cluster 2. `session_cluster_id` (backed by
    `request.session["selected_cluster_id"]`, set below in index()) is the
    fallback once the query param itself is blank — same signed-cookie
    session already used for login (dashboard/app.py's SessionMiddleware),
    not a new mechanism. The query param still wins when present, so the
    picker/bookmarked links behave exactly as before.

    Falls back to the default cluster when both are blank, unknown, or
    deactivated — a stale bookmarked link (or session pointing at a
    since-deactivated cluster) must not 404/500, it should just land back
    on the default cluster."""
    # Kept as a compatibility wrapper for existing imports/tests; the
    # dependency-free implementation belongs in dashboard.cluster_scope.
    from dashboard.cluster_scope import resolve_cluster_selection

    if request is not None:
        from dashboard.cluster_scope import cluster_selection
        return cluster_selection(request)
    return resolve_cluster_selection(requested_cluster_id, session_cluster_id)


def _dashboard_health_payload(
    status: dict,
    cluster: Cluster,
    osd_perf: dict | None = None,
    cluster_nodes: dict | list | None = None,
    osd_dump: dict | None = None,
) -> dict:
    """Convert one authoritative ceph status response into card values."""
    health = status.get("health") if isinstance(status.get("health"), dict) else {}
    osdmap = status.get("osdmap") if isinstance(status.get("osdmap"), dict) else {}
    monmap = status.get("monmap") if isinstance(status.get("monmap"), dict) else {}
    pgmap = status.get("pgmap") if isinstance(status.get("pgmap"), dict) else {}

    mons = monmap.get("mons") if isinstance(monmap.get("mons"), list) else []
    mon_total = monmap.get("num_mons") if isinstance(monmap.get("num_mons"), int) else len(mons)
    quorum = status.get("quorum_names")
    if not isinstance(quorum, list):
        quorum = status.get("quorum") if isinstance(status.get("quorum"), list) else []

    bytes_used = pgmap.get("bytes_used")
    bytes_total = pgmap.get("bytes_total")
    utilization = None
    if isinstance(bytes_used, (int, float)) and isinstance(bytes_total, (int, float)) and bytes_total > 0:
        utilization = round(bytes_used * 100 / bytes_total)

    health_value = str(health.get("status") or "UNKNOWN").removeprefix("HEALTH_")
    pools = osdmap.get("num_pools")
    if not isinstance(pools, int):
        pools = pgmap.get("num_pools") if isinstance(pgmap.get("num_pools"), int) else None

    pg_states = pgmap.get("pgs_by_state") if isinstance(pgmap.get("pgs_by_state"), list) else []
    pg_okay = bool(pg_states) and all(
        isinstance(row, dict) and set(str(row.get("state_name") or "").split("+")) <= {"active", "clean"}
        for row in pg_states
    )

    perf_rows = []
    if isinstance(osd_perf, dict):
        candidate = osd_perf.get("osd_perf_infos")
        if isinstance(candidate, list):
            perf_rows = candidate
    latency_values = []
    for row in perf_rows:
        if not isinstance(row, dict):
            continue
        perf = row.get("perf_stats") if isinstance(row.get("perf_stats"), dict) else row
        for key in ("apply_latency_ms", "commit_latency_ms"):
            value = perf.get(key)
            if isinstance(value, (int, float)):
                latency_values.append(float(value))

    online_hosts: set[str] = set()
    server_total = len(configured_nodes(cluster))
    if isinstance(cluster_nodes, list):
        # `ceph orch host ls`: an empty status means online; offline/error
        # hosts carry an explicit status string.
        for row in cluster_nodes:
            if not isinstance(row, dict):
                continue
            hostname = str(row.get("hostname") or row.get("host") or "").strip()
            status_text = str(row.get("status") or "").strip().lower()
            if hostname and status_text not in {"offline", "maintenance", "error"}:
                online_hosts.add(hostname)
        # Cephadm returns every registered host here, including offline ones.
        # Its inventory is authoritative and avoids counting separate MON/OSD
        # network addresses for one physical server as multiple servers.
        server_total = len(cluster_nodes)
    elif isinstance(cluster_nodes, dict):
        # `ceph node ls` works for both legacy and cephadm clusters. Its
        # shape is role -> hostname -> daemon-id list.
        for values in cluster_nodes.values():
            if isinstance(values, dict):
                online_hosts.update(str(host) for host in values if host)
            elif isinstance(values, list):
                online_hosts.update(str(value) for value in values if value)

    read_bps = pgmap.get("read_bytes_sec")
    write_bps = pgmap.get("write_bytes_sec")
    read_ops = pgmap.get("read_op_per_sec")
    write_ops = pgmap.get("write_op_per_sec")
    bandwidth_bps = sum(value for value in (read_bps, write_bps) if isinstance(value, (int, float)))
    iops = sum(value for value in (read_ops, write_ops) if isinstance(value, (int, float)))

    osd_total = osdmap.get("num_osds")
    osd_up = osdmap.get("num_up_osds")
    dump_rows = osd_dump.get("osds") if isinstance(osd_dump, dict) else None
    if isinstance(dump_rows, list):
        valid_rows = [row for row in dump_rows if isinstance(row, dict) and "osd" in row]
        if valid_rows:
            osd_total = len(valid_rows)
            osd_up = sum(1 for row in valid_rows if row.get("up") in (1, True, "1"))

    return {
        "health": health_value,
        "osds": {"up": osd_up, "total": osd_total},
        "mons": {"up": len(quorum), "total": mon_total},
        "servers": {"online": len(online_hosts) if cluster_nodes is not None else None, "total": server_total},
        "utilization": {
            "percent": utilization,
            "bytes_used": bytes_used if isinstance(bytes_used, (int, float)) else None,
            "pools": pools,
        },
        "metrics": {
            "latency_ms": round(sum(latency_values) / len(latency_values), 2) if latency_values else None,
            "bandwidth_bps": bandwidth_bps,
            "iops": iops,
        },
        "placement_groups": "OKAY" if pg_okay else "WARN",
    }


def _dashboard_health_snapshot_response(
    snapshot: dict | None,
    selected_cluster: Cluster,
) -> dict:
    """Map one shared snapshot to the stable dashboard card response."""
    raw_health = snapshot.get("health") if isinstance(snapshot, dict) else None
    if isinstance(raw_health, dict):
        # Watcher snapshots normally store the health result itself
        # (``{"status": "HEALTH_WARN"}``), while older snapshots may contain
        # the complete Ceph status object under ``health``. Accept both
        # shapes so the dashboard remains compatible across generations.
        status = raw_health if isinstance(raw_health.get("health"), dict) else {"health": raw_health}
    else:
        status = {}
    status_section = read_section_snapshot(
        selected_cluster.id,
        "status",
        stale_after_seconds=_DASHBOARD_HEALTH_STALE_SECONDS,
        max_stale_seconds=_DASHBOARD_HEALTH_MAX_STALE_SECONDS,
    )
    nodes_section = read_section_snapshot(
        selected_cluster.id,
        "nodes",
        stale_after_seconds=_DASHBOARD_HEALTH_STALE_SECONDS,
        max_stale_seconds=_DASHBOARD_HEALTH_MAX_STALE_SECONDS,
    )
    node_data = nodes_section.get("nodes") if isinstance(nodes_section, dict) else None
    cluster_nodes = node_data.get("nodes") if isinstance(node_data, dict) else node_data
    if not isinstance(cluster_nodes, (dict, list)):
        cluster_nodes = None
    full_status = status_section.get("status") if isinstance(status_section, dict) else None
    if isinstance(full_status, dict) and isinstance(full_status.get("health"), dict):
        # Use ceph -s for capacity/OSD/PG cards. The critical health collector
        # runs more often, so its newest health result takes precedence.
        status = dict(full_status)
        critical_health = raw_health.get("health") if isinstance(raw_health, dict) and isinstance(raw_health.get("health"), dict) else raw_health
        if isinstance(critical_health, dict):
            full_health = status.get("health")
            status["health"] = {**(full_health if isinstance(full_health, dict) else {}), **critical_health}
        payload = _dashboard_health_payload(
            status, selected_cluster, cluster_nodes=cluster_nodes,
        )
    else:
        payload = _dashboard_health_payload(
            status, selected_cluster, cluster_nodes=cluster_nodes,
        )
    status_age = status_section.get("age_seconds") if isinstance(status_section, dict) else None
    status_available = bool(status_section and status_section.get("section_available", True))
    partial_errors = {}
    if isinstance(snapshot, dict) and isinstance(snapshot.get("partial_errors"), dict):
        partial_errors.update(snapshot["partial_errors"])
    if isinstance(status_section, dict) and isinstance(status_section.get("partial_errors"), dict):
        partial_errors.update(status_section["partial_errors"])
    payload.update(
        {
            "cluster_id": selected_cluster.id,
            "cached": snapshot is not None,
            "generation": snapshot.get("generation", 0) if snapshot else 0,
            "collected_at": snapshot.get("collected_at") if snapshot else None,
            "published_at": snapshot.get("published_at") if snapshot else None,
            "age_seconds": snapshot.get("age_seconds") if snapshot else None,
            "cache_age_seconds": snapshot.get("age_seconds") if snapshot else None,
            "collector_lag_seconds": snapshot.get("collector_lag_seconds") if snapshot else None,
            "health_available": bool(snapshot.get("health_available", True)) if snapshot else False,
            "stale": (
                bool(snapshot.get("stale", True)) or not snapshot.get("health_available", True)
            ) if snapshot else True,
            "refreshing": _dashboard_health_refresh_in_progress(selected_cluster.id),
            "last_error": snapshot.get("last_error") if snapshot else None,
            "partial_errors": partial_errors,
            "last_attempted_at": snapshot.get("last_attempted_at") if snapshot else None,
            "status_available": status_available,
            "status_stale": bool(status_section.get("stale", True)) if status_section else True,
            "status_collected_at": status_section.get("collected_at") if status_section else None,
            "status_age_seconds": status_age,
        }
    )
    return payload


@router.get("/api/dashboard/health")
def dashboard_health(request: Request, _user: str = Depends(require_login)):
    """Return the latest shared snapshot without running a Ceph command."""
    _clusters, selected_cluster = _resolve_selected_cluster(
        request.query_params.get("cluster", "").strip(),
        request.session.get("selected_cluster_id", ""), request=request,
    )
    snapshot = read_snapshot(
        selected_cluster.id,
        stale_after_seconds=_DASHBOARD_HEALTH_STALE_SECONDS,
        max_stale_seconds=_DASHBOARD_HEALTH_MAX_STALE_SECONDS,
    )
    return _dashboard_health_snapshot_response(snapshot, selected_cluster)


@router.post("/api/dashboard/health/refresh", status_code=202)
async def refresh_dashboard_health(request: Request, _user: str = Depends(require_login)):
    """Queue an explicit operator refresh; never wait for Ceph in HTTP."""
    _clusters, selected_cluster = _resolve_selected_cluster(
        request.query_params.get("cluster", "").strip(),
        request.session.get("selected_cluster_id", ""), request=request,
    )
    _schedule_dashboard_health_refresh(selected_cluster)
    return {
        "accepted": True,
        "cluster_id": selected_cluster.id,
        "refreshing": True,
    }


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    user: str = Depends(require_login),
    incident_id: str = "",
    since: str = "",
    until: str = "",
    cluster: str = "",
):
    incident_id = incident_id.strip()
    since_dt = _parse_datetime_filter(since.strip())
    until_dt = _parse_datetime_filter(until.strip())
    try:
        # Inside the try (not resolved before it) — this hits the DB same
        # as everything else _fetch_dashboard_data does below, and must
        # fail the same clean 503 way if the DB is unreachable, not an
        # unhandled 500 from before the try block even started.
        clusters, selected_cluster = _resolve_selected_cluster(
            cluster.strip(), request.session.get("selected_cluster_id", ""), request=request
        )
        # Persist whatever we landed on (explicit ?cluster=, prior session
        # value, or the default-cluster fallback) so the NEXT visit to `/`
        # with no query param — e.g. clicking "Dashboard" in the nav bar
        # after navigating to /volumes — resumes on this cluster instead of
        # silently resetting to the default one (see _resolve_selected_
        # cluster's docstring above).
        request.session["selected_cluster_id"] = selected_cluster.id
        cluster_names_by_id = {c.id: c.name for c in clusters}
        # Bootstrap the React overview from the persisted snapshot. This is a
        # read-only local/cache operation, so the first paint does not need to
        # wait for a second HTTP round-trip before showing health cards. The
        # background Watcher remains the only owner of Ceph collection.
        initial_snapshot = read_snapshot(
            selected_cluster.id,
            stale_after_seconds=_DASHBOARD_HEALTH_STALE_SECONDS,
            max_stale_seconds=_DASHBOARD_HEALTH_MAX_STALE_SECONDS,
        )
        initial_health = _dashboard_health_snapshot_response(initial_snapshot, selected_cluster)
        if settings.dashboard_legacy_feed_enabled:
            (
                incidents,
                latest_heartbeat,
                pending_actions_with_incident,
                audit_entries,
                upgrade_blocks_other_actions,
                backup_alert,
                telegram_configured,
                has_other_cluster_pending,
            ) = _fetch_dashboard_data(
                incident_id, since_dt, until_dt, selected_cluster.id, selected_cluster.is_default, cluster_names_by_id
            )
        else:
            # The React overview is entirely snapshot-backed. Avoid querying
            # the heartbeat table during first paint: the snapshot already
            # carries freshness and the Watcher remains the only collector.
            # This keeps simultaneous browser tabs off the database hot path.
            latest_heartbeat = None
            incidents = []
            pending_actions_with_incident = []
            audit_entries = []
            upgrade_blocks_other_actions = False
            backup_alert = None
            telegram_configured = True
            has_other_cluster_pending = False
        # Kept inside the same try as the fetch (Review Story 5.2) — these
        # derive directly from just-fetched DB data, so any failure here
        # (e.g. a malformed row) should surface the same friendly error,
        # not an unhandled 500 that a caller-facing except SQLAlchemyError
        # alone wouldn't catch.
        stale = (
            is_heartbeat_stale(latest_heartbeat)
            if settings.dashboard_legacy_feed_enabled
            else bool(initial_health.get("stale", True))
        )
        status = compute_cluster_status(incidents, stale)
        # 2026-08-10 (multi-tenant remediation Phase 2): the "Chờ duyệt" card
        # used to hide the instant the 3 GLOBAL channels were configured —
        # now that a non-default cluster's own channel can NARROW coverage
        # instead of the global ones always covering everything, that alone
        # would strand an uncovered action (the "Chat-with-AI confirm was
        # assumed sufficient" bug docs/telegram-alerts.md mục 6.7 already
        # describes fixing once). Force the card visible whenever at least
        # one pending action isn't actually covered by any reachable
        # Telegram channel, even if the global 3 are configured — default
        # single-cluster behavior (every action always default-cluster-
        # covered) is unchanged.
        show_pending_card = not telegram_configured or any(
            not covered for _, _, _, covered in pending_actions_with_incident
        )
    except SQLAlchemyError:
        logger.exception("index: failed to query incidents from DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )
    except Exception:
        # Any other failure while preparing the page (e.g. a bug in
        # compute_cluster_status/is_heartbeat_stale) must not leak a raw
        # 500/stack trace to the browser either.
        logger.exception("index: failed to prepare dashboard page")
        raise HTTPException(status_code=500, detail="Lỗi khi tải trang — xem log server để biết chi tiết")
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "status": status,
            "incidents": incidents,
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "heartbeat": latest_heartbeat,
            "heartbeat_stale": stale,
            "cluster_mon_nodes": selected_cluster.ceph_mon_nodes,
            "cluster_container_name": selected_cluster.ceph_container_name,
            "cluster_exec_mode": selected_cluster.ceph_exec_mode,
            "clusters": clusters,
            "selected_cluster": selected_cluster,
            "initial_health": initial_health,
            "dashboard_legacy_feed_enabled": settings.dashboard_legacy_feed_enabled,
            "pending_actions": pending_actions_with_incident,
            "audit_entries": audit_entries,
            "filter_incident_id": incident_id,
            "filter_since": since,
            "filter_until": until,
            "upgrade_blocks_other_actions": upgrade_blocks_other_actions,
            "backup_alert": backup_alert,
            # 2026-08-07: the "Chờ duyệt" card only renders when Telegram
            # approval doesn't already cover every pending action (see
            # show_pending_card's own comment above, and
            # _fetch_dashboard_data's docstring) — Duyệt/Từ chối reaches a
            # covered RISKY Action's Telegram channel regardless of where it
            # originated; the card stays as the fallback for anything that
            # isn't reachable that way.
            "telegram_approval_configured": not show_pending_card,
            "has_other_cluster_pending": has_other_cluster_pending,
            # Sidebar tab (2026-07-24) — lands on Audit Trail if the operator
            # just used its filter form (a GET with query params, unlike
            # Settings' POST-result sections), otherwise defaults to Chờ
            # duyệt (the most actionable tab) when that card exists, else
            # Incident Feed.
            "active_tab": (
                "audit" if (incident_id or since or until)
                else "pending" if show_pending_card
                else "incidents"
            ),
        },
    )
