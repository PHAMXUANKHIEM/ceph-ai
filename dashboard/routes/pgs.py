import asyncio
import json
import logging
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
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()
POOL_CREATE_CEPH_CODE = "POOL_CREATE_REQUEST"


def _pool_names_by_id(payload: dict | list) -> dict[str, str]:
    raw_rows = payload if isinstance(payload, list) else payload.get("pools", []) if isinstance(payload, dict) else []
    return {
        str(row["pool"]): str(row.get("pool_name") or row.get("poolname") or row["pool"])
        for row in raw_rows
        if isinstance(row, dict) and "pool" in row
    }


def _normalize_pg_rows(payload: dict | list, pool_names: dict[str, str] | None = None) -> list[dict]:
    """Normalize the cluster-wide ``ceph pg dump pgs`` response."""
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
        pgid = str(pg.get("pgid", "—"))
        pool_id = pgid.split(".", 1)[0] if "." in pgid else ""
        acting = pg.get("acting") if isinstance(pg.get("acting"), list) else []
        up = pg.get("up") if isinstance(pg.get("up"), list) else []
        primary = pg.get("acting_primary")
        if primary is None:
            primary = pg.get("up_primary")
        if primary is None:
            primary = acting[0] if acting else "—"
        rows.append(
            {
                "pgid": pgid,
                "state": pg.get("state", "unknown"),
                "pool": (pool_names or {}).get(pool_id, pool_id or "—"),
                "up": up,
                "acting": acting,
                "primary": primary,
                "last_scrub": pg.get("last_scrub_stamp") or "—",
                "last_deep_scrub": pg.get("last_deep_scrub_stamp") or "—",
            }
        )
    return sorted(rows, key=lambda row: str(row["pgid"]))


@router.get("/pgs", response_class=HTMLResponse)
async def pgs_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    connection = cluster_connection(cluster)
    rows: list[dict] = []
    query_error: str | None = None
    try:
        query = ceph_client.run_ceph_json_command if cluster.is_default else None
        if query is not None:
            _host, pool_payload = await asyncio.to_thread(query, "ceph osd pool ls detail")
            _host, pg_payload = await asyncio.to_thread(query, "ceph pg dump pgs")
        else:
            _host, pool_payload = await asyncio.to_thread(
                run_ceph_json_command_with, *connection, "ceph osd pool ls detail"
            )
            _host, pg_payload = await asyncio.to_thread(
                run_ceph_json_command_with, *connection, "ceph pg dump pgs"
            )
        rows = _normalize_pg_rows(pg_payload, _pool_names_by_id(pool_payload))
    except CephQueryError as exc:
        logger.warning("pgs_page: failed to query all PGs for cluster %s: %s", cluster.id, exc)
        query_error = str(exc)

    state_counts = Counter(row["state"] for row in rows)
    return templates.TemplateResponse(
        request,
        "pgs.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "pgs": rows,
            "state_counts": sorted(state_counts.items()),
            "query_error": query_error,
            "clusters": clusters,
            "selected_cluster": cluster,
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
