import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import to_utc_iso
from shared import db
from shared.cluster_nodes import configured_nodes as _configured_nodes
from shared.models import NodeDiagnosticRun
from watcher.ceph_client import DIAGNOSTIC_COMMANDS, CephQueryError, run_diagnostic_command
from watcher.node_metrics import NodeMetricsError, collect_node_metrics
from watcher.rgw_log import RgwLogError, fetch_rgw_log

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# How many past runs the Nodes-page CLI history panel shows — an audit
# trail (kept forever in the DB), not a live-tail; the page just doesn't
# need more than this to be useful.
DIAGNOSTIC_HISTORY_LIMIT = 20


@router.get("/nodes", response_class=HTMLResponse)
async def nodes_page(request: Request, user: str = Depends(require_login)):
    nodes = _configured_nodes()
    requested_host = request.query_params.get("host")
    known_hosts = {n["host"] for n in nodes}
    if requested_host and requested_host not in known_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    # No default node — landing on /nodes with no ?host= shows the empty
    # "chọn một node" state, not whichever node happened to be configured
    # first. A node is only selected when the operator actually picks one
    # (or deep-links with ?host=).
    selected_host = requested_host
    selected_node = next((n for n in nodes if n["host"] == selected_host), None)
    return templates.TemplateResponse(
        request,
        "nodes.html",
        {"user": user, "nodes": nodes, "selected_host": selected_host, "selected_node": selected_node},
    )


@router.get("/api/nodes/{host}/metrics")
async def node_metrics_api(host: str, user: str = Depends(require_login)):
    # `host` is attacker-reachable input feeding straight into an SSH
    # connect() — without this whitelist, any logged-in user could make the
    # Dashboard open an SSH session to an arbitrary address of their
    # choosing using the Watcher keypair (SSRF-via-SSH). Only nodes the
    # operator already configured for this cluster are queryable.
    allowed_hosts = {n["host"] for n in _configured_nodes()}
    if host not in allowed_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    try:
        metrics = collect_node_metrics(host)
    except NodeMetricsError as exc:
        logger.warning("node_metrics_api: %s", exc)
        raise HTTPException(status_code=502, detail=f"Không lấy được metrics từ node: {exc}")
    return {"host": host, **metrics}


def _diagnostic_run_to_dict(run: NodeDiagnosticRun) -> dict:
    return {
        "id": run.id,
        "host": run.host,
        "command_id": run.command_id,
        "command_label": run.command_label,
        "actor": run.actor,
        "success": run.success,
        "output_excerpt": run.output_excerpt,
        "created_at": to_utc_iso(run.created_at),
    }


@router.get("/api/nodes/{host}/diagnostics")
async def node_diagnostics_history(host: str, user: str = Depends(require_login)):
    """Recent runs of the read-only diagnostic CLI for this node — this list
    IS the audit trail for the feature (every run is persisted, see
    node_diagnostics_run below); there is no separate delete/edit route."""
    allowed_hosts = {n["host"] for n in _configured_nodes()}
    if host not in allowed_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    with db.SessionLocal() as session:
        runs = (
            session.query(NodeDiagnosticRun)
            .filter(NodeDiagnosticRun.host == host)
            .order_by(NodeDiagnosticRun.created_at.desc())
            .limit(DIAGNOSTIC_HISTORY_LIMIT)
            .all()
        )
    return {
        "commands": [{"command_id": cid, "label": label} for cid, label in DIAGNOSTIC_COMMANDS.items()],
        "runs": [_diagnostic_run_to_dict(run) for run in runs],
    }


@router.post("/api/nodes/{host}/diagnostics")
async def node_diagnostics_run(
    host: str,
    command_id: str = Form(...),
    user: str = Depends(require_login),
):
    # Same SSRF-via-SSH guard as node_metrics_api above — host must be one
    # of this cluster's configured nodes, never attacker-supplied.
    allowed_hosts = {n["host"] for n in _configured_nodes()}
    if host not in allowed_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách đã cấu hình")
    # command_id must be a whitelist key looked up server-side — this route
    # never runs a client-supplied command string (see
    # watcher/ceph_client.py::run_diagnostic_command / DIAGNOSTIC_COMMANDS).
    if command_id not in DIAGNOSTIC_COMMANDS:
        raise HTTPException(status_code=400, detail="Lệnh không hợp lệ")

    command_label = DIAGNOSTIC_COMMANDS[command_id]
    try:
        output = run_diagnostic_command(host, command_id)
        success = True
    except CephQueryError as exc:
        logger.warning("node_diagnostics_run: %s (%s) failed: %s", host, command_id, exc)
        output = str(exc)
        success = False
    except Exception as exc:
        logger.warning("node_diagnostics_run: %s (%s) unavailable: %s", host, command_id, exc)
        output = f"Không chạy được lệnh: {exc}"
        success = False

    run = NodeDiagnosticRun(
        host=host,
        command_id=command_id,
        command_label=command_label,
        actor=user,
        success=success,
        output_excerpt=output,
    )
    with db.SessionLocal() as session:
        session.add(run)
        session.commit()
        session.refresh(run)
        result = _diagnostic_run_to_dict(run)
    return result


@router.get("/api/nodes/{host}/rgw-log")
async def rgw_log_api(host: str, filter: str = "", user: str = Depends(require_login)):
    """Backs the Nodes page's "Log RGW" panel — tails this host's radosgw
    daemon log (watcher/rgw_log.py), optionally grepped server-side by
    `filter`. Live-only, like node_metrics_api above: nothing here is
    persisted (no audit trail the way node_diagnostics_run has one) — this
    is a monitoring view, not a discrete auditable action.
    """
    # Restricted to hosts actually carrying the RGW role, not just any
    # configured node — same SSRF-via-SSH whitelist posture as
    # node_metrics_api/node_diagnostics_run, narrowed further here since a
    # non-RGW host has no radosgw container/daemon to read from anyway.
    rgw_hosts = {n["host"] for n in _configured_nodes() if "RGW" in n["roles"]}
    if host not in rgw_hosts:
        raise HTTPException(status_code=404, detail="Node không nằm trong danh sách RGW đã cấu hình")
    try:
        output = fetch_rgw_log(host, filter)
    except RgwLogError as exc:
        logger.warning("rgw_log_api: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    lines = output.splitlines()
    return {"host": host, "filter": filter, "lines": lines}
