import asyncio
import json
import logging
import re
from collections import Counter
from datetime import datetime
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.cluster_scope import cluster_connection, cluster_selection
from dashboard.templating import make_templates
from shared import audit, db
from shared.models import Action, ActionClassification, ActionStatus, Incident, IncidentStatus
from shared.object_storage_cache import get_or_load, invalidate as invalidate_cluster_cache
from watcher import ceph_client
from watcher.ceph_client import CephQueryError, run_ceph_json_command_with
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)
router = APIRouter()
templates = make_templates()
POOL_CREATE_CEPH_CODE = "POOL_CREATE_REQUEST"
POOL_ACTION_CEPH_CODE = "POOL_ACTION_REQUEST"
CEPH_POOL_FLAG_NODELETE = 1 << 4


def _pool_names_by_id(payload: dict | list) -> dict[str, str]:
    raw_rows = payload if isinstance(payload, list) else payload.get("pools", []) if isinstance(payload, dict) else []
    names: dict[str, str] = {}
    for row in raw_rows:
        if not isinstance(row, dict):
            continue
        pool_id = row.get("pool_id")
        if pool_id is None:
            pool_id = row.get("pool")
        if pool_id is None:
            pool_id = row.get("poolnum")
        pool_name = row.get("pool_name") or row.get("poolname") or row.get("name")
        if pool_id is not None and pool_name:
            names[str(pool_id)] = str(pool_name)
    return names


def _payload_rows(payload: dict | list, *keys: str) -> list[dict]:
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = []
        for key in keys:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                rows = candidate
                break
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _pool_is_protected(pool: dict) -> bool:
    """Accept the string/list names and legacy integer flag formats Ceph emits."""
    flag_names = pool.get("flags_names")
    if isinstance(flag_names, list):
        names = {str(item).strip().lower() for item in flag_names}
    else:
        names = {
            part.lower()
            for part in re.split(r"[,;\s]+", str(flag_names or ""))
            if part
        }
    if "nodelete" in names:
        return True
    flags = pool.get("flags")
    if isinstance(flags, int) and not isinstance(flags, bool):
        return bool(flags & CEPH_POOL_FLAG_NODELETE)
    if isinstance(flags, str) and not flags.strip().isdigit():
        return "nodelete" in {
            part.lower() for part in re.split(r"[,;\s]+", flags) if part
        }
    try:
        return bool(int(flags) & CEPH_POOL_FLAG_NODELETE)
    except (TypeError, ValueError):
        return False


def _normalize_pool_rows(
    detail_payload: dict | list,
    df_payload: dict | list,
    stats_payload: dict | list,
    rules_payload: dict | list,
) -> list[dict]:
    """Join Ceph's pool configuration, capacity and live I/O responses."""
    df_by_name = {
        str(row.get("name") or row.get("pool_name")): row.get("stats") or {}
        for row in _payload_rows(df_payload, "pools")
        if row.get("name") or row.get("pool_name")
    }
    io_by_name = {
        str(row.get("pool_name") or row.get("name")): row.get("client_io_rate") or row.get("stats") or {}
        for row in _payload_rows(stats_payload, "pool_stats", "pools")
        if row.get("pool_name") or row.get("name")
    }
    rule_names = {
        str(row.get("rule_id")): str(row.get("rule_name") or row.get("name"))
        for row in _payload_rows(rules_payload, "rules")
        if row.get("rule_id") is not None and (row.get("rule_name") or row.get("name"))
    }

    rows = []
    for pool in _payload_rows(detail_payload, "pools"):
        name = pool.get("pool_name") or pool.get("poolname") or pool.get("name")
        if not name:
            continue
        name = str(name)
        df_stats = df_by_name.get(name, {})
        io_stats = io_by_name.get(name, {})
        size = pool.get("size")
        protected = _pool_is_protected(pool)
        ec_profile = pool.get("erasure_code_profile")
        redundancy = f"EC · {ec_profile}" if ec_profile else (f"{size} replicas" if size is not None else "—")
        rule_id = pool.get("crush_rule")
        rows.append(
            {
                "name": name,
                "redundancy": redundancy,
                "size": size,
                "protected": protected,
                "pgs": pool.get("pg_num") if pool.get("pg_num") is not None else pool.get("pg_num_target", "—"),
                "crush_rule": rule_names.get(str(rule_id), str(rule_id) if rule_id is not None else "—"),
                "used_bytes": df_stats.get("stored", df_stats.get("bytes_used", 0)) or 0,
                "objects": df_stats.get("objects", 0) or 0,
                "read_iops": io_stats.get("read_op_per_sec", io_stats.get("read_iops", 0)) or 0,
                "write_iops": io_stats.get("write_op_per_sec", io_stats.get("write_iops", 0)) or 0,
            }
        )
    return sorted(rows, key=lambda row: row["name"])


def _format_bytes(value) -> str:
    try:
        amount = max(0.0, float(value))
    except (TypeError, ValueError):
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"


def _query_pool_rows(cluster) -> list[dict]:
    commands = ("ceph osd pool ls detail", "ceph df detail", "ceph osd pool stats", "ceph osd crush rule dump")
    connection = cluster_connection(cluster)

    def fetch(command: str):
        if cluster.is_default:
            return ceph_client.run_ceph_json_command(command)[1]
        return run_ceph_json_command_with(*connection, command)[1]

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        payloads = list(executor.map(fetch, commands))
    rows = _normalize_pool_rows(*payloads)
    for row in rows:
        row["used"] = _format_bytes(row.pop("used_bytes"))
    return rows


def _query_pg_rows(cluster) -> list[dict]:
    connection = cluster_connection(cluster)
    if cluster.is_default:
        pool_payload = ceph_client.run_ceph_json_command("ceph osd pool ls detail")[1]
        pg_payload = ceph_client.run_ceph_json_command("ceph pg dump pgs")[1]
    else:
        pool_payload = run_ceph_json_command_with(*connection, "ceph osd pool ls detail")[1]
        pg_payload = run_ceph_json_command_with(*connection, "ceph pg dump pgs")[1]
    return _normalize_pg_rows(pg_payload, _pool_names_by_id(pool_payload))


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
        pool_id = pg.get("pool_id")
        if pool_id is None:
            pool_id = pg.get("pool")
        if pool_id is None:
            pool_id = pgid.split(".", 1)[0] if "." in pgid else ""
        pool_id = str(pool_id)
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
    rows: list[dict] = []
    query_error: str | None = None
    try:
        rows = await asyncio.to_thread(get_or_load, "pgs", f"{cluster.id}:inventory", lambda: _query_pg_rows(cluster))
    except CephQueryError as exc:
        logger.warning("pgs_page: failed to query all PGs for cluster %s: %s", cluster.id, exc)
        query_error = str(exc)

    state_counts = Counter(row["state"] for row in rows)
    pool_names = sorted({str(row["pool"]) for row in rows if row.get("pool") not in (None, "—")})
    return templates.TemplateResponse(
        request,
        "pgs.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "pgs": rows,
            "pool_names": pool_names,
            "state_counts": sorted(state_counts.items()),
            "query_error": query_error,
            "clusters": clusters,
            "selected_cluster": cluster,
        },
    )


@router.get("/pools", response_class=HTMLResponse)
async def pools_page(request: Request, user: str = Depends(require_login)):
    clusters, cluster = cluster_selection(request)
    rows: list[dict] = []
    query_error: str | None = None
    try:
        action_pending = request.query_params.get("create_success") == "1" or bool(
            request.query_params.get("action_success", "").strip()
        )
        rows = await asyncio.to_thread(
            _query_pool_rows if action_pending else
            lambda selected: get_or_load("pools", f"{selected.id}:inventory", lambda: _query_pool_rows(selected)),
            cluster,
        )
    except CephQueryError as exc:
        logger.warning("pools_page: failed to query pools for cluster %s: %s", cluster.id, exc)
        query_error = str(exc)

    return templates.TemplateResponse(
        request,
        "pools.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "pools": rows,
            "query_error": query_error,
            "clusters": clusters,
            "selected_cluster": cluster,
            "create_success": request.query_params.get("create_success") == "1",
            "action_success": request.query_params.get("action_success", "").strip() or None,
            "selected_pool": request.query_params.get("pool", "").strip() or None,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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

    invalidate_cluster_cache(cluster.id, "pools")
    invalidate_cluster_cache(cluster.id, "pgs")

    return RedirectResponse(
        url=f"/pools?cluster={cluster.id}&create_success=1",
        status_code=303,
    )


@router.post("/pools/action")
async def pool_action(
    request: Request,
    user: str = Depends(require_login),
    cluster_id: str = Form(""),
    action_id: str = Form(""),
    pool_name: str = Form(""),
    size: int | None = Form(None),
    pg_num: int | None = Form(None),
    protected: str = Form(""),
):
    if not auth.is_admin_user(user):
        raise HTTPException(status_code=403, detail="Chỉ tài khoản admin mới được thay đổi pool")

    allowed = {"edit_pool", "scrub_pool", "delete_pool", "set_pool_protection"}
    action_id = action_id.strip()
    if action_id not in allowed:
        raise HTTPException(status_code=400, detail="Thao tác pool không hợp lệ")

    clusters, selected = cluster_selection(request)
    cluster = {item.id: item for item in clusters}.get(cluster_id.strip())
    if cluster is None or cluster.id != selected.id:
        raise HTTPException(status_code=400, detail="Cụm được gửi lên không khớp cụm đang chọn")
    mon_nodes, _container, _ssh_user, _ssh_key, _exec_mode = cluster_connection(cluster)
    if not mon_nodes:
        raise HTTPException(status_code=400, detail="Cụm đang chọn chưa cấu hình MON node")

    name = pool_name.strip()
    try:
        query = ceph_client.run_ceph_json_command if cluster.is_default else None
        if query is not None:
            _host, pool_payload = await asyncio.to_thread(query, "ceph osd pool ls detail")
        else:
            _host, pool_payload = await asyncio.to_thread(
                run_ceph_json_command_with, *cluster_connection(cluster), "ceph osd pool ls detail"
            )
    except CephQueryError as exc:
        raise HTTPException(status_code=502, detail=f"Không kiểm tra được pool trên cụm: {exc}") from exc
    existing_names = set(_pool_names_by_id(pool_payload).values())
    if name not in existing_names:
        raise HTTPException(status_code=404, detail="Pool không tồn tại trên cụm đang chọn")

    params: dict = {"pool_name": name}
    if action_id == "edit_pool":
        params.update({"size": size, "pg_num": pg_num})
    elif action_id == "set_pool_protection":
        if protected not in {"true", "false"}:
            raise HTTPException(status_code=400, detail="Trạng thái bảo vệ pool không hợp lệ")
        params["protected"] = protected == "true"
    try:
        command = executor_commands.get_command(action_id, mon_nodes[0], params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Thông tin pool không hợp lệ: {exc}") from exc

    classification = gate.classify_action(action_id)
    status = ActionStatus.APPROVED if classification == ActionClassification.SAFE else ActionStatus.PENDING_APPROVAL
    incident_status = IncidentStatus.APPROVED if classification == ActionClassification.SAFE else IncidentStatus.PENDING_APPROVAL
    labels = {
        "edit_pool": "cập nhật",
        "scrub_pool": "scrub",
        "delete_pool": "xóa",
        "set_pool_protection": "đổi trạng thái bảo vệ",
    }
    with db.SessionLocal() as session:
        incident = Incident(
            cluster_id=cluster.id,
            ceph_code=POOL_ACTION_CEPH_CODE,
            status=incident_status.value,
            log_excerpt=f"{user} yêu cầu {labels[action_id]} pool {name}",
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()
        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=classification.value,
            status=status.value,
            rationale=f"{labels[action_id].capitalize()} pool {name} trên cụm {cluster.name}",
            target_nodes=json.dumps([mon_nodes[0]]),
            action_params=json.dumps(params),
            proposed_command=command,
        )
        session.add(action)
        session.flush()
        audit.record(session, incident_id=incident.id, action_id=action.id, event_type=audit.EVENT_CHAT_ACTION_REQUESTED, actor=user)
        session.commit()

    invalidate_cluster_cache(cluster.id, "pools")
    invalidate_cluster_cache(cluster.id, "pgs")

    return RedirectResponse(
        url=f"/pools?{urlencode({'cluster': cluster.id, 'pool': name, 'action_success': action_id})}",
        status_code=303,
    )
