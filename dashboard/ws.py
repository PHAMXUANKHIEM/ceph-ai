import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import func, or_

from shared import db
from shared.clusters import ensure_default_cluster, list_active_clusters
from shared.models import Incident

router = APIRouter()
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2
WS_POLICY_VIOLATION = 1008


def _snapshot(cluster_id: str | None = None, is_default_cluster: bool = True) -> tuple[int, object]:
    """A cheap fingerprint of incident state for the selected cluster.

    Watcher heartbeat is deliberately excluded: it changes on every poll
    and used to trigger a full browser reload every few seconds even when
    the cluster state had not changed.

    Polling the DB is a simple stand-in — there is no event bus wired from
    Watcher/Worker directly to the Dashboard.
    """
    with db.SessionLocal() as session:
        default_cluster = ensure_default_cluster(session)
        effective_id = cluster_id or default_cluster.id
        cluster_filter = (
            or_(Incident.cluster_id == effective_id, Incident.cluster_id.is_(None))
            if is_default_cluster
            else Incident.cluster_id == effective_id
        )
        count = session.query(func.count(Incident.id)).filter(cluster_filter).scalar()
        latest_updated = session.query(func.max(Incident.updated_at)).filter(cluster_filter).scalar()
    return count, latest_updated


@router.websocket("/ws/incidents")
async def incidents_ws(websocket: WebSocket) -> None:
    # SessionMiddleware populates websocket.session from the same signed
    # cookie used by the HTTP routes (Starlette applies session middleware
    # to the "websocket" scope too) — same require_login check as / , just
    # not expressible as a FastAPI Depends on a websocket route.
    if not websocket.session.get("user"):
        await websocket.close(code=WS_POLICY_VIOLATION)
        return

    await websocket.accept()
    with db.SessionLocal() as session:
        default_cluster = ensure_default_cluster(session)
        active = {cluster.id: cluster for cluster in list_active_clusters(session)}
        selected = active.get(websocket.session.get("selected_cluster_id"), default_cluster)
        selected_id = selected.id
        selected_is_default = selected.is_default
    last_seen = _snapshot(selected_id, selected_is_default)
    try:
        while True:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            try:
                current = _snapshot(selected_id, selected_is_default)
            except Exception:
                logger.exception("incidents_ws: failed to poll DB, closing connection")
                await websocket.close(code=1011)  # internal error
                return
            if current != last_seen:
                last_seen = current
                await websocket.send_json({"event": "incidents_changed"})
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("incidents_ws: unexpected error, closing connection")
