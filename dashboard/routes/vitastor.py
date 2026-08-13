"""Independent Vitastor monitoring dashboard and connection inventory."""

import asyncio
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from shared import db
from shared.models import VitastorAnomalyEvent, VitastorCluster, VitastorDiagnosticRun, VitastorMetricSample, VitastorOsdMetricSample
from vitastor.client import LOG_SOURCES, VitastorConnectionError, normalize_etcd, normalize_status, query_dashboard, query_logs
from vitastor.diagnosis import diagnose

router = APIRouter(prefix="/vitastor", tags=["vitastor"])
templates = make_templates()


def _cluster_log_hosts(cluster: VitastorCluster) -> list[str]:
    """Return the configured management host plus known deployment nodes."""
    hosts = [cluster.management_host]
    try:
        cached = json.loads(cluster.last_status_json or "{}")
        deployment = cached.get("deployment") or {}
        hosts.extend(
            str(node["host"]).strip()
            for node in deployment.get("nodes", [])
            if isinstance(node, dict) and str(node.get("host", "")).strip()
        )
    except (TypeError, ValueError):
        pass
    return list(dict.fromkeys(host for host in hosts if host))


async def require_vitastor_login(request: Request, user: str = Depends(require_login)) -> str:
    if request.session.get("product") != "vitastor":
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return user


@router.get("", response_class=HTMLResponse)
async def vitastor_home(request: Request, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        clusters = session.query(VitastorCluster).order_by(VitastorCluster.name.asc()).all()
    return templates.TemplateResponse(request, "vitastor/index.html", {
        "user": user, "is_admin": auth.is_vitastor_admin_user(user), "clusters": clusters,
    })


@router.get("/logs", response_class=HTMLResponse)
async def vitastor_logs_page(request: Request, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        clusters = session.query(VitastorCluster).filter(VitastorCluster.is_active.is_(True)).order_by(VitastorCluster.name).all()
        cluster_hosts = {cluster.id: _cluster_log_hosts(cluster) for cluster in clusters}
    return templates.TemplateResponse(request, "vitastor/logs.html", {"user": user, "is_admin": auth.is_vitastor_admin_user(user), "clusters": clusters, "cluster_hosts": cluster_hosts})


@router.get("/api/logs")
async def vitastor_logs_api(cluster_id: str | None = None, host: str = "", source: str = "all", keyword: str = "", preset: str = "all", lines: int = 300, user: str = Depends(require_vitastor_login)):
    if source not in LOG_SOURCES: raise HTTPException(400, "Nguồn log không hợp lệ")
    patterns = {"all": (), "errors": ("error", "failed", "fatal", "panic"), "warnings": ("warn", "slow", "degraded", "timeout"), "recovery": ("recover", "rebalance", "misplaced"), "network": ("connect", "disconnect", "socket", "network")}
    if preset not in patterns: raise HTTPException(400, "Bộ lọc chỉ định không hợp lệ")
    keyword = keyword.strip()
    if len(keyword) > 100 or any(ord(ch) < 32 for ch in keyword): raise HTTPException(400, "Từ khóa lọc không hợp lệ")
    with db.SessionLocal() as session:
        cluster = _cluster_or_404(session, cluster_id)
        allowed_hosts = _cluster_log_hosts(cluster)
        selected_host = host.strip() or cluster.management_host
        if selected_host not in allowed_hosts: raise HTTPException(400, "Host không thuộc cụm Vitastor")
        args = (selected_host, cluster.ssh_user, cluster.ssh_key_path); cluster_name = cluster.name; cluster_pk = cluster.id
    try: raw = await asyncio.to_thread(query_logs, *args, source=source, lines=lines)
    except VitastorConnectionError as exc: raise HTTPException(502, str(exc)) from exc
    rows = raw.splitlines(); selected = patterns[preset]
    if selected: rows = [line for line in rows if any(token in line.lower() for token in selected)]
    if keyword: rows = [line for line in rows if keyword.casefold() in line.casefold()]
    return {"cluster": {"id": cluster_pk, "name": cluster_name}, "host": selected_host, "source": source, "preset": preset, "keyword": keyword, "lines": rows, "count": len(rows), "fetched_at": datetime.utcnow().isoformat() + "Z"}


def _cluster_or_404(session, cluster_id: str | None) -> VitastorCluster:
    query = session.query(VitastorCluster).filter(VitastorCluster.is_active.is_(True))
    cluster = query.filter(VitastorCluster.id == cluster_id).first() if cluster_id else query.order_by(VitastorCluster.name.asc()).first()
    if cluster is None:
        raise HTTPException(status_code=404, detail="Chưa cấu hình cụm Vitastor")
    return cluster


def _query_cluster(cluster: VitastorCluster) -> dict:
    return query_dashboard(
        cluster.management_host, cluster.ssh_user, cluster.ssh_key_path,
        cluster.etcd_address, cluster.etcd_prefix, cluster.config_path,
        cluster.exec_mode, cluster.container_name,
    )


def _diagnostic_dict(row: VitastorDiagnosticRun) -> dict:
    result = None
    if row.result_json:
        try: result = json.loads(row.result_json)
        except json.JSONDecodeError: pass
    return {
        "id": row.id, "cluster_id": row.cluster_id, "status": row.status,
        "health": row.health, "diagnosis_text": row.diagnosis_text, "result": result,
        "error": row.error_message, "requested_by": row.requested_by,
        "created_at": row.created_at.isoformat() + "Z",
        "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
    }


@router.get("/api/diagnostics/latest")
async def latest_vitastor_diagnostic(cluster_id: str, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        _cluster_or_404(session, cluster_id)
        row = session.query(VitastorDiagnosticRun).filter_by(cluster_id=cluster_id).order_by(VitastorDiagnosticRun.created_at.desc()).first()
        return {"diagnostic": _diagnostic_dict(row) if row else None}


@router.get("/api/anomalies")
async def vitastor_anomalies(cluster_id: str, include_resolved: bool = False, user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        _cluster_or_404(session, cluster_id)
        query = session.query(VitastorAnomalyEvent).filter_by(cluster_id=cluster_id)
        if not include_resolved: query = query.filter_by(status="OPEN")
        rows = query.order_by(VitastorAnomalyEvent.detected_at.desc()).limit(100).all()
        return {"anomalies": [{"id": row.id, "entity_type": row.entity_type, "entity_name": row.entity_name, "metric": row.metric, "status": row.status, "severity": row.severity, "current": row.current_value, "baseline": row.baseline_value, "ratio": row.deviation_ratio, "samples": row.sample_count, "explanation": row.explanation, "detected_at": row.detected_at.isoformat()+"Z", "resolved_at": row.resolved_at.isoformat()+"Z" if row.resolved_at else None} for row in rows]}


@router.post("/api/diagnostics")
async def create_vitastor_diagnostic(cluster_id: str = Form(...), user: str = Depends(require_vitastor_login)):
    with db.SessionLocal() as session:
        cluster = _cluster_or_404(session, cluster_id)
        session.expunge(cluster)
    try:
        datasets = await asyncio.to_thread(_query_cluster, cluster)
    except VitastorConnectionError as exc:
        raise HTTPException(status_code=502, detail=f"Không thu thập được evidence: {exc}") from exc
    summary = normalize_status(datasets["status"])
    evidence = {
        "summary": summary, "status": datasets["status"],
        "pools": (datasets.get("pools") or [])[:100],
        "osds": (datasets.get("osds") or [])[:200],
        "images": (datasets.get("images") or [])[:100],
        "collection_errors": datasets.get("errors") or {},
    }
    with db.SessionLocal() as session:
        row = VitastorDiagnosticRun(cluster_id=cluster.id, status="RUNNING", health=summary["health"], evidence_json=json.dumps(evidence), requested_by=user)
        session.add(row); session.commit(); diagnostic_id = row.id
    try:
        diagnosis_text, result = await diagnose(cluster.name, evidence)
    except Exception as exc:
        with db.SessionLocal() as session:
            row = session.get(VitastorDiagnosticRun, diagnostic_id)
            row.status = "FAILED"; row.error_message = str(exc); row.finished_at = datetime.utcnow(); session.commit()
        raise HTTPException(status_code=502, detail=f"AI chẩn đoán thất bại: {exc}") from exc
    with db.SessionLocal() as session:
        row = session.get(VitastorDiagnosticRun, diagnostic_id)
        row.status = "COMPLETED"; row.diagnosis_text = diagnosis_text
        row.result_json = json.dumps(result, ensure_ascii=False); row.finished_at = datetime.utcnow()
        session.commit(); return {"diagnostic": _diagnostic_dict(row)}


@router.get("/api/overview")
async def vitastor_overview_api(
    cluster_id: str | None = None, user: str = Depends(require_vitastor_login)
):
    with db.SessionLocal() as session:
        cluster = _cluster_or_404(session, cluster_id)
        cluster_pk = cluster.id
        cluster_name = cluster.name
        try:
            datasets = await asyncio.to_thread(_query_cluster, cluster)
        except VitastorConnectionError as exc:
            cached = None
            if cluster.last_status_json:
                try:
                    cached = json.loads(cluster.last_status_json)
                except (TypeError, json.JSONDecodeError):
                    pass
            if cached:
                if "summary" not in cached:
                    cached = {
                        "summary": normalize_status(cached),
                        "pools": [], "osds": [], "images": [], "section_errors": {},
                    }
                return {
                    **cached,
                    "cluster": {"id": cluster_pk, "name": cluster_name},
                    "stale": True, "error": str(exc),
                }
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        response = {
            "cluster": {"id": cluster_pk, "name": cluster_name},
            "stale": False,
            "checked_at": datetime.utcnow().isoformat() + "Z",
            "summary": normalize_status(datasets["status"]),
            "etcd_detail": normalize_etcd(datasets.get("etcd_status"), datasets.get("etcd_health")),
            "pools": datasets.get("pools") if isinstance(datasets.get("pools"), list) else [],
            "osds": datasets.get("osds") if isinstance(datasets.get("osds"), list) else [],
            "images": datasets.get("images") if isinstance(datasets.get("images"), list) else [],
            "section_errors": datasets.get("errors") or {},
        }
        osd_rows = response["osds"]
        server_names = {
            str(row.get("parent")) for row in osd_rows
            if isinstance(row, dict) and row.get("type") == "osd" and row.get("parent")
        }
        online_names = {
            str(row.get("parent")) for row in osd_rows
            if isinstance(row, dict) and row.get("type") == "osd" and row.get("parent") and row.get("up")
        }
        response["summary"]["servers"] = {"online": len(online_names), "total": len(server_names)}
        cached_alert_state = {}
        if cluster.last_status_json:
            try:
                old_cache = json.loads(cluster.last_status_json)
                cached_alert_state = {k: v for k, v in old_cache.items() if str(k).startswith("_telegram_")}
                if isinstance(old_cache.get("hardware"), list): response["hardware"] = old_cache["hardware"]
                if isinstance(old_cache.get("network"), list): response["network"] = old_cache["network"]
            except (TypeError, json.JSONDecodeError, AttributeError):
                pass
        cached_response = {k: v for k, v in response.items() if k != "cluster"}
        cached_response.update(cached_alert_state)
        cluster.last_status_json = json.dumps(cached_response)
        cluster.last_checked_at = datetime.utcnow()
        session.commit()
        return response


@router.get("/api/metrics/history")
async def vitastor_metric_history(
    cluster_id: str | None = None, hours: int = 24,
    user: str = Depends(require_vitastor_login),
):
    hours = min(720, max(1, hours))
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    with db.SessionLocal() as session:
        cluster = _cluster_or_404(session, cluster_id)
        points = session.query(VitastorMetricSample).filter(
            VitastorMetricSample.cluster_id == cluster.id,
            VitastorMetricSample.collected_at >= cutoff,
        ).order_by(VitastorMetricSample.collected_at).all()
        osd_points = session.query(VitastorOsdMetricSample).filter(
            VitastorOsdMetricSample.cluster_id == cluster.id,
            VitastorOsdMetricSample.collected_at >= cutoff,
        ).order_by(VitastorOsdMetricSample.collected_at).all()
        return {
            "cluster_id": cluster.id, "hours": hours,
            "points": [{
                "at": row.collected_at.isoformat() + "Z", "health": row.health,
                "used_percent": row.used_percent, "read_iops": row.read_iops,
                "write_iops": row.write_iops, "read_bps": row.read_bps,
                "write_bps": row.write_bps, "read_latency_ms": row.read_latency_ms,
                "write_latency_ms": row.write_latency_ms, "recovery_bps": row.recovery_bps,
                "degraded_bytes": row.degraded_bytes, "etcd_latency_ms": row.etcd_latency_ms,
            } for row in points],
            "osds": [{
                "at": row.collected_at.isoformat() + "Z", "osd_id": row.osd_id,
                "host": row.host, "up": row.is_up, "used_percent": row.used_percent,
                "read_iops": row.read_iops, "write_iops": row.write_iops,
                "read_bps": row.read_bps, "write_bps": row.write_bps,
                "read_latency_ms": row.read_latency_ms, "write_latency_ms": row.write_latency_ms,
            } for row in osd_points],
        }


def _require_vitastor_admin(user: str) -> None:
    if not auth.is_vitastor_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ Vitastor admin được cấu hình cụm")


@router.post("/clusters")
async def create_vitastor_cluster(
    name: str = Form(...), management_host: str = Form(...),
    etcd_address: str = Form(""), etcd_prefix: str = Form("/vitastor"),
    config_path: str = Form(""), ssh_user: str = Form(...),
    ssh_key_path: str = Form(...), exec_mode: str = Form("none"),
    container_name: str = Form(""), user: str = Depends(require_vitastor_login),
):
    _require_vitastor_admin(user)
    values = {
        "name": name.strip(), "management_host": management_host.strip(),
        "etcd_address": etcd_address.strip(), "etcd_prefix": etcd_prefix.strip() or "/vitastor",
        "config_path": config_path.strip(), "ssh_user": ssh_user.strip(),
        "ssh_key_path": ssh_key_path.strip(), "exec_mode": exec_mode.strip(),
        "container_name": container_name.strip(),
    }
    if not values["name"] or not values["management_host"] or not values["ssh_user"] or not values["ssh_key_path"]:
        raise HTTPException(status_code=400, detail="Thiếu tên cụm, management host hoặc thông tin SSH")
    if not values["config_path"] and not values["etcd_address"]:
        raise HTTPException(status_code=400, detail="Cần etcd address hoặc config path")
    if values["exec_mode"] not in {"none", "docker", "podman"}:
        raise HTTPException(status_code=400, detail="Exec mode không hợp lệ")
    if values["exec_mode"] != "none" and not values["container_name"]:
        raise HTTPException(status_code=400, detail="Cần tên container cho Docker/Podman")

    probe = VitastorCluster(**values, created_by=user)
    try:
        datasets = await asyncio.to_thread(_query_cluster, probe)
    except VitastorConnectionError as exc:
        raise HTTPException(status_code=400, detail=f"Không xác minh được cụm: {exc}") from exc
    cached = {
        "stale": False, "checked_at": datetime.utcnow().isoformat() + "Z",
        "summary": normalize_status(datasets["status"]),
        "etcd_detail": normalize_etcd(datasets.get("etcd_status"), datasets.get("etcd_health")),
        "pools": datasets.get("pools") if isinstance(datasets.get("pools"), list) else [],
        "osds": datasets.get("osds") if isinstance(datasets.get("osds"), list) else [],
        "images": datasets.get("images") if isinstance(datasets.get("images"), list) else [],
        "section_errors": datasets.get("errors") or {},
    }
    probe.last_status_json = json.dumps(cached)
    probe.last_checked_at = datetime.utcnow()
    with db.SessionLocal() as session:
        if session.query(VitastorCluster).filter(VitastorCluster.name == values["name"]).first():
            raise HTTPException(status_code=409, detail="Tên cụm Vitastor đã tồn tại")
        session.add(probe)
        session.commit()
        cluster_id = probe.id
    return {"ok": True, "cluster_id": cluster_id}
