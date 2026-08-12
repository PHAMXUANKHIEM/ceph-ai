import asyncio
import json
import logging
import shlex
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.cluster_scope import cluster_connection, cluster_selection
from dashboard.templating import make_templates
from shared import audit, db
from shared.models import Action, ActionClassification, ActionStatus, Incident, IncidentStatus
from watcher import ceph_client
from watcher.ceph_client import CephQueryError, run_ceph_json_command_with
from dashboard.routes.volumes import _pool_names_from_detail
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()
POOL_CREATE_CEPH_CODE = "POOL_CREATE_REQUEST"


def _normalize_pg_rows(payload: dict | list) -> list[dict]:
    """Normalize `ceph pg ls-by-pool --format json` across Ceph releases."""
    if isinstance(payload, list):
        raw_rows = payload
    elif isinstance(payload, dict):
        raw_rows = payload.get("pg_stats") or payload.get("pgs") or []
    else:
        raw_rows = []

    rows = []
    for pg in raw_rows:
        if not isinstance(pg, dict):
            continue
        stats = pg.get("stat_sum") if isinstance(pg.get("stat_sum"), dict) else {}
        rows.append(
            {
                "pgid": pg.get("pgid", "—"),
                "state": pg.get("state", "unknown"),
                "up": pg.get("up") if isinstance(pg.get("up"), list) else [],
                "acting": pg.get("acting") if isinstance(pg.get("acting"), list) else [],
                "objects": stats.get("num_objects", 0),
                "bytes": stats.get("num_bytes", 0),
                "degraded": stats.get("num_objects_degraded", 0),
                "misplaced": stats.get("num_objects_misplaced", 0),
                "unfound": stats.get("num_objects_unfound", 0),
                "last_scrub": pg.get("last_scrub_stamp") or "—",
            }
        )
    return sorted(rows, key=lambda row: str(row["pgid"]))


@router.get("/pgs", response_class=HTMLResponse)
async def pgs_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    connection = cluster_connection(cluster)
    if cluster.is_default:
        pools = ceph_client.configured_rbd_pools()
    else:
        try:
            _host, pool_payload = await asyncio.to_thread(
                run_ceph_json_command_with, *connection, "ceph osd pool ls detail"
            )
            pools = _pool_names_from_detail(pool_payload)
        except CephQueryError as exc:
            logger.warning("pgs_page: pool discovery failed for cluster %s: %s", cluster.id, exc)
            pools = []
    selected_pool = request.query_params.get("pool")
    if selected_pool and selected_pool not in pools:
        raise HTTPException(status_code=404, detail="Pool không nằm trong danh sách đã cấu hình")

    rows: list[dict] = []
    query_error: str | None = None
    if selected_pool:
        try:
            command = f"ceph pg ls-by-pool {shlex.quote(selected_pool)}"
            if cluster.is_default:
                _host, payload = await asyncio.to_thread(ceph_client.run_ceph_json_command, command)
            else:
                _host, payload = await asyncio.to_thread(run_ceph_json_command_with, *connection, command)
            rows = _normalize_pg_rows(payload)
        except CephQueryError as exc:
            logger.warning("pgs_page: failed to query pool %r: %s", selected_pool, exc)
            query_error = str(exc)

    state_counts = Counter(row["state"] for row in rows)
    return templates.TemplateResponse(
        request,
        "pgs.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "pools": pools,
            "selected_pool": selected_pool,
            "pgs": rows,
            "state_counts": sorted(state_counts.items()),
            "query_error": query_error,
            "clusters": clusters,
            "selected_cluster": cluster,
            "create_success": (
                "Yêu cầu tạo pool đã được gửi tới Worker."
                if request.query_params.get("create_success") == "1"
                else None
            ),
        },
    )


@router.post("/pgs/pools/create")
async def create_pool(
    request: Request,
    user: str = Depends(require_login),
    cluster_id: str = Form(""),
    pool_name: str = Form(""),
    pg_num: int = Form(32),
    app_name: str = Form("rbd"),
    return_to: str = Form("pgs"),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ tài khoản admin mới được tạo pool")

    clusters, selected = cluster_selection(request)
    by_id = {cluster.id: cluster for cluster in clusters}
    cluster = by_id.get(cluster_id.strip())
    if cluster is None or cluster.id != selected.id:
        raise HTTPException(status_code=400, detail="Cụm được gửi lên không khớp cụm đang chọn")

    mon_nodes, _container, _ssh_user, _ssh_key, _exec_mode = cluster_connection(cluster)
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Cụm đang chọn chưa cấu hình MON node")

    params = {"pool_name": pool_name.strip(), "pg_num": pg_num, "app_name": app_name.strip()}
    try:
        command = executor_commands.get_command("create_pool", mon_nodes[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Thông tin pool không hợp lệ: {exc}") from exc

    classification = gate.classify_action("create_pool")
    if classification != ActionClassification.SAFE:
        raise HTTPException(status_code=500, detail="Policy create_pool không còn ở chế độ SAFE")

    with db.SessionLocal() as session:
        incident = Incident(
            cluster_id=cluster.id,
            ceph_code=POOL_CREATE_CEPH_CODE,
            status=IncidentStatus.APPROVED.value,
            log_excerpt=f"{user} yêu cầu tạo pool {params['pool_name']} với {pg_num} PG",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id="create_pool",
            classification=classification.value,
            status=ActionStatus.APPROVED.value,
            rationale=f"Tạo pool {params['pool_name']} trên cụm {cluster.name}",
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(params),
            proposed_command=command,
        )
        session.add(action)
        session.flush()
        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_POOL_CREATE_REQUESTED,
            actor=user,
        )
        session.commit()

    return RedirectResponse(
        url=f"/{'volumes' if return_to == 'volumes' else 'pgs'}?cluster={cluster.id}&create_success=1",
        status_code=303,
    )
